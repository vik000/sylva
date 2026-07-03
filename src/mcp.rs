//! MCP server core — the three query tools over JSON-RPC.
//!
//! The dispatch logic lives in `handle_request(db_path, request_json)`, a pure
//! string-in / string-out function so it can be unit-tested directly from
//! Python without spawning a process. The actual stdio serve loop is a thin
//! wrapper in `python/sylva/__main__.py` (`sylva serve --db ...`).
//!
//! Protocol: plain JSON-RPC 2.0. Every call returns a valid JSON-RPC response
//! string — protocol problems (bad JSON, unknown method, bad params, missing
//! database) are reported as JSON-RPC `error` objects, never as panics or
//! Python exceptions.
//!
//! Tools:
//! - `search_symbol` (params: name)  — symbols matching a name
//! - `get_callers`   (params: symbol) — symbols that call the given symbol
//! - `get_dependencies` (params: symbol) — symbols the given symbol calls/imports
//!
//! `get_callers` / `get_dependencies` read the `edges` table, which is not yet
//! populated by the indexer (see issue #27). Their query logic is exercised
//! against edges seeded directly in tests; on a freshly-indexed repo they
//! return an empty list until edge population lands.

use pyo3::prelude::*;
use rusqlite::{params, Connection};
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

/// Run a tool query and return its results as a JSON array. Errors are returned
/// as human-readable strings for the caller to wrap in a JSON-RPC error.
fn run_query(db_path: &str, sql: &str, name: &str) -> Result<Value, String> {
    if !Path::new(db_path).exists() {
        return Err(format!("database not found: '{}'", db_path));
    }

    let conn = Connection::open(db_path)
        .map_err(|e| format!("cannot open database '{}': {}", db_path, e))?;

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

/// Handle a single JSON-RPC request against the graph database and return the
/// JSON-RPC response as a string. Never raises — all failures become JSON-RPC
/// error responses.
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

    let sql = match sql_for(method) {
        Some(sql) => sql,
        None => {
            return err_response(id, METHOD_NOT_FOUND, &format!("Method not found: {}", method))
        }
    };

    // All three tools take a single string argument; accept either `name` or
    // `symbol` so callers can use whichever reads naturally.
    let params_val = req.get("params").cloned().unwrap_or(Value::Null);
    let arg = params_val
        .get("name")
        .or_else(|| params_val.get("symbol"))
        .and_then(Value::as_str);
    let name = match arg {
        Some(s) => s,
        None => {
            return err_response(
                id,
                INVALID_PARAMS,
                "Invalid params: expected a 'name' or 'symbol' string",
            )
        }
    };

    match run_query(db_path, sql, name) {
        Ok(results) => ok_response(id, results),
        Err(msg) => err_response(id, SERVER_ERROR, &msg),
    }
}
