//! MCP server core — the three query tools over JSON-RPC.
//!
//! The dispatch logic lives in `handle_request(db_path, request_json)`, a pure
//! string-in / string-out function so it can be unit-tested directly from
//! Python without spawning a process. The actual stdio serve loop is a thin
//! wrapper in `python/sylva/__main__.py` (`sylva serve --db ...`).
//!
//! Protocol: JSON-RPC 2.0. Every call returns a valid JSON-RPC response
//! string — protocol problems (bad JSON, unknown method, bad params, missing
//! database) are reported as JSON-RPC `error` objects, never as panics or
//! Python exceptions.
//!
//! Two framings are supported over the same dispatch (Feature 7.4 / issue #31):
//! - **Plain JSON-RPC** — one method per tool (`{"method":"search_symbol",
//!   "params":{"name":...}}`). The original Feature 1.6 surface, kept working.
//! - **MCP framing** — real MCP clients (Claude Code, Cursor) speak
//!   `initialize` (handshake), `tools/list` (discovery with input schemas), and
//!   `tools/call` (`{"name":..., "arguments":{...}}`). `notifications/initialized`
//!   is accepted as a no-op (a JSON-RPC notification → no response).
//!
//! Tools:
//! - `search_symbol` (params: name)  — symbols matching a name
//! - `get_callers`   (params: symbol) — symbols that call the given symbol
//! - `get_dependencies` (params: symbol) — symbols the given symbol calls/imports
//! - `get_coverage` (params: name) — a symbol's coverage %, or null (Feature 3.5)
//! - `get_uncovered_paths` (no params) — symbols with 0% coverage (Feature 3.5)
//! - `get_test_coverage` (params: name) — tests that exercise a symbol (Feature 3.5)
//!
//! `get_callers` / `get_dependencies` read the `edges` table, which is not yet
//! populated by the indexer (see issue #27). Their query logic is exercised
//! against edges seeded directly in tests; on a freshly-indexed repo they
//! return an empty list until edge population lands.

use pyo3::prelude::*;
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};
use std::path::Path;

// JSON-RPC error codes (standard + one app-specific server error).
const PARSE_ERROR: i64 = -32700;
const INVALID_REQUEST: i64 = -32600;
const METHOD_NOT_FOUND: i64 = -32601;
const INVALID_PARAMS: i64 = -32602;
const SERVER_ERROR: i64 = -32001;

fn ok_response(id: Value, result: Value) -> String {
    json!({ "jsonrpc": "2.0", "id": id, "result": result }).to_string()
}

fn err_response(id: Value, code: i64, message: &str) -> String {
    json!({
        "jsonrpc": "2.0",
        "id": id,
        "error": { "code": code, "message": message }
    })
    .to_string()
}

/// SQL for each tool. All three project the same symbol shape so the client
/// gets a uniform result structure.
fn sql_for(method: &str) -> Option<&'static str> {
    match method {
        "search_symbol" => Some(
            "SELECT s.name, s.kind, s.line_start, s.docstring, f.path \
             FROM symbols s JOIN files f ON f.id = s.file_id \
             WHERE s.name = ?1 \
             ORDER BY f.path, s.line_start",
        ),
        // Callers: inbound `calls` edges — src symbols pointing at the target.
        "get_callers" => Some(
            "SELECT s.name, s.kind, s.line_start, s.docstring, f.path \
             FROM edges e \
             JOIN symbols s ON s.id = e.src_id \
             JOIN symbols t ON t.id = e.dst_id \
             JOIN files f ON f.id = s.file_id \
             WHERE t.name = ?1 AND e.kind = 'calls' \
             ORDER BY f.path, s.line_start",
        ),
        // Dependencies: outbound `calls`/`imports` edges — dst symbols.
        "get_dependencies" => Some(
            "SELECT s.name, s.kind, s.line_start, s.docstring, f.path \
             FROM edges e \
             JOIN symbols s ON s.id = e.dst_id \
             JOIN symbols src ON src.id = e.src_id \
             JOIN files f ON f.id = s.file_id \
             WHERE src.name = ?1 AND e.kind IN ('calls', 'imports') \
             ORDER BY f.path, s.line_start",
        ),
        _ => None,
    }
}

/// Open the graph database for a query, mapping a missing file or open failure
/// to a human-readable error (wrapped as a JSON-RPC error by the caller).
fn open_db(db_path: &str) -> Result<Connection, String> {
    if !Path::new(db_path).exists() {
        return Err(format!("database not found: '{}'", db_path));
    }
    Connection::open(db_path).map_err(|e| format!("cannot open database '{}': {}", db_path, e))
}

/// Run a symbol-shaped tool query and return its results as a JSON array.
fn run_query(db_path: &str, sql: &str, name: &str) -> Result<Value, String> {
    let conn = open_db(db_path)?;

    let mut stmt = conn
        .prepare(sql)
        .map_err(|e| format!("failed to prepare query: {}", e))?;

    let rows = stmt
        .query_map(params![name], |row| {
            Ok(json!({
                "name": row.get::<_, String>(0)?,
                "kind": row.get::<_, String>(1)?,
                "line": row.get::<_, Option<i64>>(2)?,
                "docstring": row.get::<_, Option<String>>(3)?,
                "file": row.get::<_, String>(4)?,
            }))
        })
        .map_err(|e| format!("query failed: {}", e))?;

    let mut out = Vec::new();
    for row in rows {
        out.push(row.map_err(|e| format!("failed to read row: {}", e))?);
    }
    Ok(Value::Array(out))
}

/// `get_coverage` — a symbol's coverage percentage, or JSON `null` when the
/// symbol is unknown or has no coverage data (both map to null, never an error).
fn coverage_query(db_path: &str, name: &str) -> Result<Value, String> {
    let conn = open_db(db_path)?;
    let pct: Option<Option<f64>> = conn
        .query_row(
            "SELECT coverage_pct FROM symbols WHERE name = ?1 LIMIT 1",
            params![name],
            |r| r.get(0),
        )
        .optional()
        .map_err(|e| format!("query failed: {}", e))?;
    Ok(match pct {
        Some(Some(value)) => json!(value),
        _ => Value::Null,
    })
}

/// `get_uncovered_paths` — symbols known to be untested (`coverage_pct == 0.0`),
/// as `{name, file, line}` objects. Symbols with no data (NULL) are excluded:
/// "uncovered" means known-untested, not unmeasured. Empty list when none.
fn uncovered_paths_query(db_path: &str) -> Result<Value, String> {
    let conn = open_db(db_path)?;
    let mut stmt = conn
        .prepare(
            "SELECT s.name, f.path, s.line_start \
             FROM symbols s JOIN files f ON f.id = s.file_id \
             WHERE s.coverage_pct = 0.0 \
             ORDER BY f.path, s.line_start",
        )
        .map_err(|e| format!("failed to prepare query: {}", e))?;
    let rows = stmt
        .query_map([], |row| {
            Ok(json!({
                "name": row.get::<_, String>(0)?,
                "file": row.get::<_, String>(1)?,
                "line": row.get::<_, Option<i64>>(2)?,
            }))
        })
        .map_err(|e| format!("query failed: {}", e))?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row.map_err(|e| format!("failed to read row: {}", e))?);
    }
    Ok(Value::Array(out))
}

/// `get_test_coverage` — the names of test functions that exercise the given
/// symbol (inbound `test_covers` edges). Empty list for an unknown/untested
/// symbol.
fn test_coverage_query(db_path: &str, name: &str) -> Result<Value, String> {
    let conn = open_db(db_path)?;
    let mut stmt = conn
        .prepare(
            "SELECT DISTINCT src.name \
             FROM edges e \
             JOIN symbols dst ON dst.id = e.dst_id \
             JOIN symbols src ON src.id = e.src_id \
             WHERE dst.name = ?1 AND e.kind = 'test_covers' \
             ORDER BY src.name",
        )
        .map_err(|e| format!("failed to prepare query: {}", e))?;
    let rows = stmt
        .query_map(params![name], |row| Ok(json!(row.get::<_, String>(0)?)))
        .map_err(|e| format!("query failed: {}", e))?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row.map_err(|e| format!("failed to read row: {}", e))?);
    }
    Ok(Value::Array(out))
}

/// The MCP protocol version this server advertises in `initialize`.
const MCP_PROTOCOL_VERSION: &str = "2024-11-05";

/// The advertised tool catalogue for `tools/list`: name, description, and a JSON
/// Schema for each tool's arguments. Every tool here is dispatchable by both the
/// plain method framing and `tools/call`.
fn tools_list() -> Value {
    // A one-string-argument schema, reused by the symbol tools.
    let str_arg = |field: &str, desc: &str| {
        json!({
            "type": "object",
            "properties": { field: { "type": "string", "description": desc } },
            "required": [field]
        })
    };
    json!([
        { "name": "search_symbol", "description": "Find symbols matching a name.",
          "inputSchema": str_arg("name", "Symbol name to search for") },
        { "name": "get_callers", "description": "Symbols that call the given symbol.",
          "inputSchema": str_arg("symbol", "Symbol whose callers to list") },
        { "name": "get_dependencies", "description": "Symbols the given symbol calls or imports.",
          "inputSchema": str_arg("symbol", "Symbol whose dependencies to list") },
        { "name": "get_coverage", "description": "A symbol's coverage percentage, or null.",
          "inputSchema": str_arg("name", "Symbol name") },
        { "name": "get_test_coverage", "description": "Tests that exercise the given symbol.",
          "inputSchema": str_arg("name", "Symbol name") },
        { "name": "get_uncovered_paths", "description": "Symbols known to be untested (0% coverage).",
          "inputSchema": json!({ "type": "object", "properties": {} }) }
    ])
}

/// Dispatch a tool by name to its query. `name_arg` is the single string
/// argument (`name`/`symbol`) most tools need. Returns the raw tool result, or
/// `(json_rpc_code, message)` on failure — shared by the plain-method framing
/// and `tools/call` so both behave identically.
fn dispatch_tool(db_path: &str, tool: &str, name_arg: Option<&str>) -> Result<Value, (i64, String)> {
    let need_name = |f: fn(&str, &str) -> Result<Value, String>| match name_arg {
        Some(name) => f(db_path, name).map_err(|m| (SERVER_ERROR, m)),
        None => Err((
            INVALID_PARAMS,
            "Invalid params: expected a 'name' or 'symbol' string".to_string(),
        )),
    };
    match tool {
        "search_symbol" | "get_callers" | "get_dependencies" => {
            let sql = sql_for(tool).expect("matched a known tool");
            match name_arg {
                Some(name) => run_query(db_path, sql, name).map_err(|m| (SERVER_ERROR, m)),
                None => Err((
                    INVALID_PARAMS,
                    "Invalid params: expected a 'name' or 'symbol' string".to_string(),
                )),
            }
        }
        "get_coverage" => need_name(coverage_query),
        "get_test_coverage" => need_name(test_coverage_query),
        "get_uncovered_paths" => uncovered_paths_query(db_path).map_err(|m| (SERVER_ERROR, m)),
        other => Err((METHOD_NOT_FOUND, format!("Unknown tool: {}", other))),
    }
}

/// Extract the `name`/`symbol` string argument from a params object.
fn name_from(params: &Value) -> Option<&str> {
    params
        .get("name")
        .or_else(|| params.get("symbol"))
        .and_then(Value::as_str)
}

/// Handle a single JSON-RPC request against the graph database and return the
/// JSON-RPC response as a string. Never raises — all failures become JSON-RPC
/// error responses. A JSON-RPC *notification* (e.g. `notifications/initialized`)
/// returns an empty string, signalling the serve loop to write no response.
#[pyfunction]
pub fn handle_request(db_path: &str, request: &str) -> String {
    let req: Value = match serde_json::from_str(request) {
        Ok(v) => v,
        Err(_) => return err_response(Value::Null, PARSE_ERROR, "Parse error: invalid JSON"),
    };

    let id = req.get("id").cloned().unwrap_or(Value::Null);

    let method = match req.get("method").and_then(Value::as_str) {
        Some(m) => m,
        None => return err_response(id, INVALID_REQUEST, "Invalid Request: missing 'method'"),
    };

    let params_val = req.get("params").cloned().unwrap_or(Value::Null);

    match method {
        // --- MCP framing (Feature 7.4) --------------------------------------
        "initialize" => {
            return ok_response(
                id,
                json!({
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": { "tools": {} },
                    "serverInfo": { "name": "sylva", "version": env!("CARGO_PKG_VERSION") }
                }),
            );
        }
        // Notifications carry no id and expect no response.
        "notifications/initialized" | "initialized" => return String::new(),
        "tools/list" => return ok_response(id, json!({ "tools": tools_list() })),
        "tools/call" => {
            let tool = match params_val.get("name").and_then(Value::as_str) {
                Some(t) => t,
                None => {
                    return err_response(
                        id,
                        INVALID_PARAMS,
                        "Invalid params: 'tools/call' requires a tool 'name'",
                    )
                }
            };
            let arguments = params_val.get("arguments").cloned().unwrap_or(Value::Null);
            return match dispatch_tool(db_path, tool, name_from(&arguments)) {
                // MCP wraps a tool result as text content.
                Ok(result) => ok_response(
                    id,
                    json!({
                        "content": [{ "type": "text", "text": result.to_string() }],
                        "isError": false
                    }),
                ),
                Err((code, msg)) => err_response(id, code, &msg),
            };
        }

        // --- Plain JSON-RPC framing (Feature 1.6, kept working) -------------
        _ => match dispatch_tool(db_path, method, name_from(&params_val)) {
            Ok(result) => ok_response(id, result),
            // Preserve the original "Method not found" wording for plain calls.
            Err((METHOD_NOT_FOUND, _)) => {
                err_response(id, METHOD_NOT_FOUND, &format!("Method not found: {}", method))
            }
            Err((code, msg)) => err_response(id, code, &msg),
        },
    }
}
