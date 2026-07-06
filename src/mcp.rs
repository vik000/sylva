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
//! - `trace_calls` / `blast_radius` / `get_architecture` — graph traversal (Feature 8.2)
//! - `infer_entrypoints` (no params) — ranked global entrypoints (Feature 9.1)
//! - `main_spine` (params: entry?) — longest execution path from an entrypoint (Feature 9.2)
//! - `infer_layers` (no params) — archetype + per-symbol architectural layer (Feature 9.8)
//! - `centrality` (no params) — betweenness + dominator ranking (Feature 9.3)
//! - `suggest_test_targets` (no params) — untested logic worth e2e-testing (Feature 10.2)
//! - `get_source` (params: name, or file+start+end) — current source of a symbol (Feature 8.4)
//!
//! `get_callers` / `get_dependencies` read the `edges` table, which is not yet
//! populated by the indexer (see issue #27). Their query logic is exercised
//! against edges seeded directly in tests; on a freshly-indexed repo they
//! return an empty list until edge population lands.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

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

/// Resolve a (possibly relative) graph file path to something readable. Graph
/// paths are relative to the analysis root; the db lives at `<root>/.codemcp/
/// sylva.db`, so a relative path is retried against `<root>` (two levels up from
/// the db) when it doesn't resolve from the server's cwd.
fn resolve_source_path(db_path: &str, path: &str) -> String {
    if Path::new(path).exists() {
        return path.to_string();
    }
    if let Some(root) = Path::new(db_path).parent().and_then(|p| p.parent()) {
        let joined = root.join(path);
        if joined.exists() {
            return joined.to_string_lossy().into_owned();
        }
    }
    path.to_string()
}

/// Read a 1-based inclusive line span from a file, returning the joined source.
/// A span past EOF yields the empty string; an end beyond the file is clamped.
/// Feature 8.4: source is read on demand, so it always reflects the current file.
fn read_span(file: &str, start: i64, end: i64) -> Result<String, String> {
    if start < 1 {
        return Err(format!("invalid start line: {}", start));
    }
    let content =
        std::fs::read_to_string(file).map_err(|e| format!("cannot read file '{}': {}", file, e))?;
    let lines: Vec<&str> = content.lines().collect();
    let s = (start as usize) - 1;
    if s >= lines.len() {
        return Ok(String::new()); // span begins past EOF
    }
    let e = (end.max(start) as usize).min(lines.len());
    Ok(lines[s..e].join("\n"))
}

/// `get_source` (name form) — the current source of each symbol matching `name`,
/// read from its `line_start`..`line_end` span. Symbols with a NULL span are
/// skipped; an unknown name yields an empty array (not an error). A missing or
/// unreadable file surfaces as an error (wrapped as JSON-RPC by the caller).
fn source_by_name(db_path: &str, name: &str) -> Result<Value, String> {
    let conn = open_db(db_path)?;
    let mut stmt = conn
        .prepare(
            "SELECT s.name, s.kind, f.path, s.line_start, s.line_end \
             FROM symbols s JOIN files f ON f.id = s.file_id \
             WHERE s.name = ?1 AND s.line_start IS NOT NULL \
             ORDER BY f.path, s.line_start",
        )
        .map_err(|e| format!("failed to prepare query: {}", e))?;
    let rows = stmt
        .query_map(params![name], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, i64>(3)?,
                row.get::<_, Option<i64>>(4)?,
            ))
        })
        .map_err(|e| format!("query failed: {}", e))?;

    let mut out = Vec::new();
    for row in rows {
        let (sname, kind, file, start, end) =
            row.map_err(|e| format!("failed to read row: {}", e))?;
        let end = end.unwrap_or(start); // NULL line_end → single-line span
        let code = read_span(&resolve_source_path(db_path, &file), start, end)?;
        out.push(json!({
            "name": sname, "kind": kind, "file": file,
            "line_start": start, "line_end": end, "code": code,
        }));
    }
    Ok(Value::Array(out))
}

/// `get_source` (range form) — an arbitrary `start`..`end` slice of `file`,
/// returned as a single-element array for a uniform result shape.
fn source_range(file: &str, start: i64, end: i64) -> Result<Value, String> {
    let code = read_span(file, start, end)?;
    Ok(json!([{ "file": file, "line_start": start, "line_end": end, "code": code }]))
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
          "inputSchema": json!({ "type": "object", "properties": {} }) },
        // Feature 8.2 — graph-traversal tools now on the MCP surface.
        { "name": "trace_calls", "description": "Trace call chains from a symbol (inbound/outbound/both) to a depth.",
          "inputSchema": json!({
              "type": "object",
              "properties": {
                  "symbol": { "type": "string", "description": "Symbol to trace from" },
                  "direction": { "type": "string", "enum": ["inbound", "outbound", "both"],
                                 "description": "Traversal direction (default 'both')" },
                  "depth": { "type": "integer", "description": "Max hops (default 3)" }
              },
              "required": ["symbol"]
          }) },
        { "name": "blast_radius", "description": "Everything that would break if the symbol's signature changed.",
          "inputSchema": str_arg("symbol", "Symbol whose dependents to compute") },
        { "name": "get_architecture", "description": "Top-level view: modules, hubs, and entry points.",
          "inputSchema": json!({
              "type": "object",
              "properties": { "hub_limit": { "type": "integer", "description": "Max hubs to return (default 10)" } }
          }) },
        // Feature 9.1 — inferred, ranked global entrypoints (markers + dominance).
        { "name": "infer_entrypoints", "description": "Inferred, ranked program entrypoints (primary first).",
          "inputSchema": json!({ "type": "object", "properties": {} }) },
        // Feature 9.2 — the main execution spine (longest path from an entrypoint).
        { "name": "main_spine", "description": "The main execution spine: the longest call path from an entrypoint (default: the primary).",
          "inputSchema": json!({
              "type": "object",
              "properties": { "entry": { "type": "string", "description": "Entry symbol (default: inferred primary)" } }
          }) },
        // Feature 9.8 — project archetype + per-symbol architectural layers.
        { "name": "infer_layers", "description": "Project archetype (library/application/service) + per-symbol architectural layer (interface/transport/data/business).",
          "inputSchema": json!({ "type": "object", "properties": {} }) },
        // Feature 9.3 — centrality: betweenness (chokepoints) + dominators (gateways).
        { "name": "centrality", "description": "Rank symbols by structural importance: betweenness (chokepoints) and dominators (gateways).",
          "inputSchema": json!({ "type": "object", "properties": {} }) },
        // Feature 10.2 — which untested logic is most worth e2e-testing.
        { "name": "suggest_test_targets", "description": "Rank untested symbols most worth e2e-testing (importance-weighted), with reasons.",
          "inputSchema": json!({ "type": "object", "properties": {} }) },
        // Feature 8.4 — fetch a symbol's (or a range's) current source code.
        { "name": "get_source", "description": "Fetch the current source code of a symbol, or an explicit file range.",
          "inputSchema": json!({
              "type": "object",
              "properties": {
                  "name": { "type": "string", "description": "Symbol name (name form)" },
                  "file": { "type": "string", "description": "File path (range form)" },
                  "start": { "type": "integer", "description": "1-based start line (range form)" },
                  "end": { "type": "integer", "description": "1-based end line, inclusive (range form)" }
              }
          }) },
        { "name": "get_outline", "description": "A file's skeleton: every function/class with its signature, docstring, and line span, but no bodies — read a file's shape without its full source.",
          "inputSchema": json!({
              "type": "object",
              "properties": {
                  "file": { "type": "string", "description": "File path or filename fragment (best match wins)" }
              },
              "required": ["file"]
          }) },
        { "name": "get_overview", "description": "One-call orientation: archetype, primary + ranked entrypoints, per-layer counts, top modules, hubs, and the main spine.",
          "inputSchema": json!({ "type": "object", "properties": {} }) }
    ])
}

/// Convert a Python value (as produced by the graph-traversal tools) into a
/// `serde_json::Value`, so their existing tested logic is reused rather than
/// re-implemented. Handles the flat scalars / lists / dicts these tools return.
fn py_to_json(obj: &Bound<'_, PyAny>) -> Value {
    if obj.is_none() {
        return Value::Null;
    }
    if let Ok(b) = obj.downcast::<PyBool>() {
        return json!(b.is_true());
    }
    if let Ok(i) = obj.extract::<i64>() {
        return json!(i);
    }
    if let Ok(f) = obj.extract::<f64>() {
        return json!(f);
    }
    if let Ok(s) = obj.extract::<String>() {
        return json!(s);
    }
    if let Ok(list) = obj.downcast::<PyList>() {
        return Value::Array(list.iter().map(|it| py_to_json(&it)).collect());
    }
    if let Ok(dict) = obj.downcast::<PyDict>() {
        let mut map = serde_json::Map::new();
        for (k, v) in dict.iter() {
            let key = k.extract::<String>().unwrap_or_else(|_| k.to_string());
            map.insert(key, py_to_json(&v));
        }
        return Value::Object(map);
    }
    json!(obj.to_string())
}

/// Map a `PyErr` from a called tool to a JSON-RPC `(code, message)`: a
/// `ValueError` (bad argument) → invalid params; anything else → server error.
fn pyerr_to_rpc(py: Python<'_>, e: PyErr) -> (i64, String) {
    let code = if e.is_instance_of::<PyValueError>(py) {
        INVALID_PARAMS
    } else {
        SERVER_ERROR
    };
    (code, e.value(py).to_string())
}

/// Extract the `name`/`symbol` string argument from a params object.
fn name_from(params: &Value) -> Option<&str> {
    params
        .get("name")
        .or_else(|| params.get("symbol"))
        .and_then(Value::as_str)
}

/// Dispatch a tool by name against `args` (the tool's argument object). Returns
/// the raw tool result, or `(json_rpc_code, message)` on failure — shared by the
/// plain-method framing and `tools/call` so both behave identically. The
/// graph-traversal tools (Feature 8.2) reuse the existing PyO3 functions and
/// convert their results, rather than re-implementing traversal here.
/// `get_outline` — the skeleton of a file: every function/class with its
/// signature line, docstring, and span, but **no bodies**. Lets an agent read a
/// file's shape without pulling its full source (the big token saver). `target`
/// is a path or filename fragment; the best (shortest) matching file wins.
fn outline_query(db_path: &str, target: &str) -> Result<Value, String> {
    let conn = open_db(db_path)?;
    let (file_id, path) = {
        let mut stmt = conn
            .prepare("SELECT id, path FROM files WHERE path = ?1 OR path LIKE '%' || ?1 ORDER BY length(path) LIMIT 1")
            .map_err(|e| format!("failed to prepare query: {}", e))?;
        let mut rows = stmt
            .query_map(params![target], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))
            .map_err(|e| format!("query failed: {}", e))?;
        match rows.next() {
            Some(r) => r.map_err(|e| format!("failed to read row: {}", e))?,
            None => return Err(format!("no indexed file matching '{}'", target)),
        }
    };

    let mut stmt = conn
        .prepare(
            "SELECT name, kind, line_start, line_end, docstring FROM symbols \
             WHERE file_id = ?1 AND kind IN ('function', 'class') ORDER BY line_start",
        )
        .map_err(|e| format!("failed to prepare query: {}", e))?;
    let rows = stmt
        .query_map(params![file_id], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, Option<i64>>(2)?,
                r.get::<_, Option<i64>>(3)?,
                r.get::<_, Option<String>>(4)?,
            ))
        })
        .map_err(|e| format!("query failed: {}", e))?;

    // Read the source once for signature lines (best-effort: skip if unreadable).
    let content = std::fs::read_to_string(resolve_source_path(db_path, &path)).unwrap_or_default();
    let lines: Vec<&str> = content.lines().collect();

    let mut syms = Vec::new();
    for row in rows {
        let (name, kind, ls, le, doc) = row.map_err(|e| format!("failed to read row: {}", e))?;
        let signature = ls
            .and_then(|l| lines.get((l as usize).saturating_sub(1)))
            .map(|s| s.trim().to_string());
        syms.push(json!({
            "name": name, "kind": kind,
            "line_start": ls, "line_end": le,
            "signature": signature, "docstring": doc,
        }));
    }
    Ok(json!({ "file": path, "symbols": syms }))
}

/// `get_overview` — a one-call orientation map: archetype, the primary + ranked
/// entrypoints, per-layer symbol counts, top modules, hubs, and the main spine.
/// Composes the existing analyses so an agent orients in a single round-trip.
fn overview(py: Python<'_>, db_path: &str) -> Result<Value, (i64, String)> {
    let arch = crate::architecture::get_architecture(py, db_path, 8)
        .map(|r| py_to_json(r.bind(py))).map_err(|e| pyerr_to_rpc(py, e))?;
    let eps = crate::entrypoints::infer_entrypoints(py, db_path)
        .map(|r| py_to_json(r.bind(py))).map_err(|e| pyerr_to_rpc(py, e))?;
    let layers = crate::layers::infer_layers(py, db_path)
        .map(|r| py_to_json(r.bind(py))).map_err(|e| pyerr_to_rpc(py, e))?;
    let spine = crate::spine::main_spine(py, db_path, None)
        .map(|r| py_to_json(r.bind(py))).map_err(|e| pyerr_to_rpc(py, e))?;

    let primary = eps
        .as_array()
        .and_then(|a| a.iter().find(|e| e.get("primary").and_then(Value::as_bool) == Some(true)))
        .cloned()
        .unwrap_or(Value::Null);
    let top_eps: Vec<Value> = eps
        .as_array()
        .map(|a| a.iter().take(8).cloned().collect())
        .unwrap_or_default();
    let mut layer_counts = serde_json::Map::new();
    if let Some(items) = layers.get("layers").and_then(Value::as_array) {
        for it in items {
            if let Some(l) = it.get("layer").and_then(Value::as_str) {
                let n = layer_counts.get(l).and_then(Value::as_i64).unwrap_or(0) + 1;
                layer_counts.insert(l.to_string(), json!(n));
            }
        }
    }
    let spine_syms: Vec<Value> = spine
        .get("nodes")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(|n| n.get("symbol").cloned()).collect())
        .unwrap_or_default();

    Ok(json!({
        "archetype": layers.get("archetype").cloned().unwrap_or(Value::Null),
        "primary_entrypoint": primary,
        "entrypoints": top_eps,
        "layers": layer_counts,
        "modules": arch.get("modules").cloned().unwrap_or(Value::Null),
        "hubs": arch.get("hubs").cloned().unwrap_or(Value::Null),
        "spine": spine_syms,
    }))
}

fn dispatch_tool(
    py: Python<'_>,
    db_path: &str,
    tool: &str,
    args: &Value,
) -> Result<Value, (i64, String)> {
    let invalid_name = || {
        (
            INVALID_PARAMS,
            "Invalid params: expected a 'name' or 'symbol' string".to_string(),
        )
    };
    let need_name = |f: fn(&str, &str) -> Result<Value, String>| match name_from(args) {
        Some(name) => f(db_path, name).map_err(|m| (SERVER_ERROR, m)),
        None => Err(invalid_name()),
    };
    match tool {
        "search_symbol" | "get_callers" | "get_dependencies" => {
            let sql = sql_for(tool).expect("matched a known tool");
            match name_from(args) {
                Some(name) => run_query(db_path, sql, name).map_err(|m| (SERVER_ERROR, m)),
                None => Err(invalid_name()),
            }
        }
        "get_coverage" => need_name(coverage_query),
        "get_test_coverage" => need_name(test_coverage_query),
        "get_uncovered_paths" => uncovered_paths_query(db_path).map_err(|m| (SERVER_ERROR, m)),

        // Feature 8.4 — `get_source`: range form when `file` is given, else name form.
        "get_source" => {
            if let Some(file) = args.get("file").and_then(Value::as_str) {
                match (
                    args.get("start").and_then(Value::as_i64),
                    args.get("end").and_then(Value::as_i64),
                ) {
                    (Some(s), Some(e)) => source_range(file, s, e).map_err(|m| (SERVER_ERROR, m)),
                    _ => Err((
                        INVALID_PARAMS,
                        "Invalid params: range form requires integer 'start' and 'end'".to_string(),
                    )),
                }
            } else {
                match name_from(args) {
                    Some(name) => source_by_name(db_path, name).map_err(|m| (SERVER_ERROR, m)),
                    None => Err(invalid_name()),
                }
            }
        }

        // --- Feature 8.2: graph-traversal tools (reuse existing functions) ---
        "trace_calls" => {
            let symbol = name_from(args).ok_or_else(invalid_name)?;
            let direction = args.get("direction").and_then(Value::as_str).unwrap_or("both");
            let depth = args.get("depth").and_then(Value::as_i64).unwrap_or(3);
            crate::edges::trace_calls(py, db_path, symbol, direction, depth)
                .map(|r| py_to_json(r.bind(py)))
                .map_err(|e| pyerr_to_rpc(py, e))
        }
        "blast_radius" => {
            let symbol = name_from(args).ok_or_else(invalid_name)?;
            crate::edges::blast_radius(py, db_path, symbol)
                .map(|r| py_to_json(r.bind(py)))
                .map_err(|e| pyerr_to_rpc(py, e))
        }
        "get_architecture" => {
            let hub_limit = args.get("hub_limit").and_then(Value::as_u64).unwrap_or(10) as usize;
            crate::architecture::get_architecture(py, db_path, hub_limit)
                .map(|r| py_to_json(r.bind(py)))
                .map_err(|e| pyerr_to_rpc(py, e))
        }
        "infer_entrypoints" => crate::entrypoints::infer_entrypoints(py, db_path)
            .map(|r| py_to_json(r.bind(py)))
            .map_err(|e| pyerr_to_rpc(py, e)),
        "main_spine" => {
            let entry = args.get("entry").and_then(Value::as_str);
            crate::spine::main_spine(py, db_path, entry)
                .map(|r| py_to_json(r.bind(py)))
                .map_err(|e| pyerr_to_rpc(py, e))
        }
        "infer_layers" => crate::layers::infer_layers(py, db_path)
            .map(|r| py_to_json(r.bind(py)))
            .map_err(|e| pyerr_to_rpc(py, e)),
        "centrality" => crate::centrality::centrality(py, db_path)
            .map(|r| py_to_json(r.bind(py)))
            .map_err(|e| pyerr_to_rpc(py, e)),
        "suggest_test_targets" => crate::testtargets::suggest_test_targets(py, db_path)
            .map(|r| py_to_json(r.bind(py)))
            .map_err(|e| pyerr_to_rpc(py, e)),

        // --- Epic 11: token-efficient traversal ---------------------------
        "get_outline" => {
            let target = args
                .get("file")
                .or_else(|| args.get("name"))
                .and_then(Value::as_str)
                .ok_or_else(|| (INVALID_PARAMS, "Invalid params: expected a 'file' string".to_string()))?;
            outline_query(db_path, target).map_err(|m| (SERVER_ERROR, m))
        }
        "get_overview" => overview(py, db_path),

        other => Err((METHOD_NOT_FOUND, format!("Unknown tool: {}", other))),
    }
}

/// Handle a single JSON-RPC request against the graph database and return the
/// JSON-RPC response as a string. Never raises — all failures become JSON-RPC
/// error responses. A JSON-RPC *notification* (e.g. `notifications/initialized`)
/// returns an empty string, signalling the serve loop to write no response.
#[pyfunction]
pub fn handle_request(py: Python<'_>, db_path: &str, request: &str) -> String {
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
            return match dispatch_tool(py, db_path, tool, &arguments) {
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
        _ => match dispatch_tool(py, db_path, method, &params_val) {
            Ok(result) => ok_response(id, result),
            // Preserve the original "Method not found" wording for plain calls.
            Err((METHOD_NOT_FOUND, _)) => {
                err_response(id, METHOD_NOT_FOUND, &format!("Method not found: {}", method))
            }
            Err((code, msg)) => err_response(id, code, &msg),
        },
    }
}

/// Resolve `path` to an absolute path without requiring it to exist (unlike
/// `canonicalize`): an already-absolute path is returned as-is; a relative one
/// is joined onto the current working directory.
fn absolutise(path: &str) -> PathBuf {
    let p = Path::new(path);
    if p.is_absolute() {
        p.to_path_buf()
    } else {
        std::env::current_dir().map(|cwd| cwd.join(p)).unwrap_or_else(|_| p.to_path_buf())
    }
}

/// Feature 8.2 — write a per-project MCP scaffold into `out_dir`.
///
/// Writes `<out_dir>/mcp.json` (a Claude Code / MCP-client server config that
/// launches `sylva serve` against this repo's db, by **absolute** path so it
/// works from any client cwd) and `<out_dir>/tools.json` (the advertised tool
/// manifest, consumed by Feature 8.3). Idempotent: re-running overwrites with
/// identical content. Returns the path to the written `mcp.json`.
///
/// Interpretation (A) from issue #41: this wires Sylva's existing *query* tools
/// to the graph — it does not turn the analysed repo's own functions into
/// executable tools.
#[pyfunction]
#[pyo3(signature = (db_path, out_dir=".codemcp", command=None))]
pub fn init_mcp(db_path: &str, out_dir: &str, command: Option<&str>) -> PyResult<String> {
    use pyo3::exceptions::PyOSError;

    std::fs::create_dir_all(out_dir)
        .map_err(|e| PyOSError::new_err(format!("cannot create '{}': {}", out_dir, e)))?;

    let abs_db = absolutise(db_path);
    let abs_db_str = abs_db.to_string_lossy().to_string();

    // The launcher command: the caller passes the absolute path to the `sylva`
    // executable (so a venv install works without `sylva` being on the client's
    // PATH); falls back to the bare name.
    let cmd = command.unwrap_or("sylva");
    let config = json!({
        "mcpServers": {
            "sylva": {
                "command": cmd,
                "args": ["serve", "--db", abs_db_str]
            }
        }
    });
    let cfg_path = Path::new(out_dir).join("mcp.json");
    std::fs::write(&cfg_path, serde_json::to_string_pretty(&config).unwrap())
        .map_err(|e| PyOSError::new_err(format!("cannot write '{}': {}", cfg_path.display(), e)))?;

    // Tool manifest (Feature 8.3 consumes this to represent accessible tools).
    let manifest = json!({ "tools": tools_list() });
    let manifest_path = Path::new(out_dir).join("tools.json");
    std::fs::write(&manifest_path, serde_json::to_string_pretty(&manifest).unwrap()).map_err(
        |e| PyOSError::new_err(format!("cannot write '{}': {}", manifest_path.display(), e)),
    )?;

    Ok(cfg_path.to_string_lossy().to_string())
}
