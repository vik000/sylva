//! Edge population — build `calls` and `imports` relationships across the graph.
//!
//! Resolving a reference (`b()` in one file → the symbol `b` defined in another)
//! needs the *whole* graph, so this is a standalone pass run **after** all files
//! are indexed, not a per-file step. It:
//!   - clears existing `calls`/`imports` edges (leaving `test_covers` intact),
//!   - for every `import` symbol, links it to the unique definition of the same
//!     name (`imports` edge),
//!   - re-parses each indexed file, finds call sites, attributes each to the
//!     innermost enclosing symbol (the caller), and links it to the unique
//!     definition of the callee name (`calls` edge).
//!
//! Resolution is by name with ambiguous-skip: a unique matching definition wins;
//! several same-named definitions are skipped and logged; unresolved names
//! (builtins / external / not indexed) are skipped. This never invents a false
//! edge, and self-calls (recursion) produce a self-edge. Edges use
//! `INSERT OR IGNORE` against the unique index (migration v2), so the pass is
//! idempotent. Returns the number of edges written.
//!
//! Precision limits of name-only resolution (tracked in issue #36): method
//! calls resolve by method name ignoring receiver type; there is no scope /
//! shadowing model; and genuinely-colliding names are skipped rather than
//! disambiguated. Type-aware resolution is future work.

use pyo3::exceptions::{PyOSError, PyRuntimeError};
use pyo3::prelude::*;
use rusqlite::{params, Connection};
use std::collections::HashMap;
use std::path::Path;
use tree_sitter::{Node, Parser};

struct SymRow {
    id: i64,
    name: String,
    kind: String,
    file_id: i64,
    line_start: Option<i64>,
    line_end: Option<i64>,
}

fn node_text(node: Node, src: &[u8]) -> String {
    node.utf8_text(src).unwrap_or("").to_string()
}

/// Resolve a referenced name to a unique definition (function/class) symbol id.
/// Ambiguous (several same-named defs) or unresolved names yield None.
fn resolve_def(defs_by_name: &HashMap<String, Vec<i64>>, name: &str) -> Option<i64> {
    match defs_by_name.get(name).map(Vec::as_slice) {
        Some([only]) => Some(*only),
        Some(many) if many.len() > 1 => {
            eprintln!(
                "sylva: reference '{}' matches {} definitions; skipping (ambiguous)",
                name,
                many.len()
            );
            None
        }
        _ => None,
    }
}

/// Innermost symbol (narrowest span) among `candidates` whose span contains
/// `line`. Used to find the caller enclosing a call site.
fn innermost<'a>(syms: &'a [SymRow], candidates: &[usize], line: i64) -> Option<&'a SymRow> {
    candidates
        .iter()
        .map(|&i| &syms[i])
        .filter(|s| match s.line_start {
            Some(start) => start <= line && line <= s.line_end.unwrap_or(start),
            None => false,
        })
        .min_by_key(|s| {
            let start = s.line_start.expect("filtered to Some");
            s.line_end.unwrap_or(start) - start
        })
}

/// Collect `(line, callee_name)` for every call site in the tree. The callee is
/// the function identifier (`b()`) or the attribute name (`obj.method()`).
fn collect_calls(node: Node, src: &[u8], out: &mut Vec<(i64, String)>) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "call" {
            if let Some(func) = child.child_by_field_name("function") {
                let name = match func.kind() {
                    "identifier" => Some(node_text(func, src)),
                    "attribute" => func.child_by_field_name("attribute").map(|a| node_text(a, src)),
                    _ => None,
                };
                if let Some(name) = name {
                    out.push((child.start_position().row as i64 + 1, name));
                }
            }
        }
        collect_calls(child, src, out); // recurse: calls nest inside args/bodies
    }
}

/// Build `calls` and `imports` edges for the indexed graph. Returns the number
/// of edges written.
#[pyfunction]
pub fn build_edges(db_path: &str) -> PyResult<usize> {
    if !Path::new(db_path).exists() {
        return Err(PyOSError::new_err(format!("database not found: '{}'", db_path)));
    }
    let mut conn = Connection::open(db_path)
        .map_err(|e| PyOSError::new_err(format!("cannot open database '{}': {}", db_path, e)))?;

    // Load all symbols and file records once.
    let syms: Vec<SymRow> = {
        let mut stmt = conn
            .prepare("SELECT id, name, kind, file_id, line_start, line_end FROM symbols")
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        let rows = stmt
            .query_map([], |r| {
                Ok(SymRow {
                    id: r.get(0)?,
                    name: r.get(1)?,
                    kind: r.get(2)?,
                    file_id: r.get(3)?,
                    line_start: r.get(4)?,
                    line_end: r.get(5)?,
                })
            })
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        rows.collect::<Result<_, _>>()
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?
    };

    let files: Vec<(i64, String)> = {
        let mut stmt = conn
            .prepare("SELECT id, path FROM files")
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read files: {}", e)))?;
        let rows = stmt
            .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read files: {}", e)))?;
        rows.collect::<Result<_, _>>()
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read files: {}", e)))?
    };

    // Definitions by name (resolution targets) and non-import symbols by file
    // (caller-span lookup).
    let mut defs_by_name: HashMap<String, Vec<i64>> = HashMap::new();
    let mut by_file: HashMap<i64, Vec<usize>> = HashMap::new();
    for (i, s) in syms.iter().enumerate() {
        if s.kind == "function" || s.kind == "class" {
            defs_by_name.entry(s.name.clone()).or_default().push(s.id);
        }
        if s.kind != "import" {
            by_file.entry(s.file_id).or_default().push(i);
        }
    }

    // Parse each file up front (before the transaction borrows the connection).
    let mut parser = Parser::new();
    parser
        .set_language(&tree_sitter_python::LANGUAGE.into())
        .map_err(|e| PyRuntimeError::new_err(format!("failed to load Python grammar: {}", e)))?;

    // (src_id, dst_id) call edges, resolved.
    let mut call_edges: Vec<(i64, i64)> = Vec::new();
    for (file_id, path) in &files {
        let source = match std::fs::read_to_string(path) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("sylva: skipping unreadable file '{}' during edge build: {}", path, e);
                continue;
            }
        };
        let tree = match parser.parse(source.as_bytes(), None) {
            Some(t) => t,
            None => {
                eprintln!("sylva: failed to parse '{}' during edge build; skipping", path);
                continue;
            }
        };

        let mut calls = Vec::new();
        collect_calls(tree.root_node(), source.as_bytes(), &mut calls);

        let empty = Vec::new();
        let candidates = by_file.get(file_id).unwrap_or(&empty);
        for (line, callee) in calls {
            let caller = match innermost(&syms, candidates, line) {
                Some(s) => s,
                None => continue, // call outside any symbol (module-level) — no src
            };
            if let Some(dst) = resolve_def(&defs_by_name, &callee) {
                call_edges.push((caller.id, dst));
            }
        }
    }

    // Now write: clear old calls/imports, then insert imports + calls.
    let tx = conn
        .transaction()
        .map_err(|e| PyRuntimeError::new_err(format!("failed to begin transaction: {}", e)))?;

    tx.execute("DELETE FROM edges WHERE kind IN ('calls', 'imports')", [])
        .map_err(|e| PyRuntimeError::new_err(format!("failed to clear edges: {}", e)))?;

    let mut written = 0usize;
    {
        let mut insert = tx
            .prepare("INSERT OR IGNORE INTO edges (src_id, dst_id, kind) VALUES (?1, ?2, ?3)")
            .map_err(|e| PyRuntimeError::new_err(format!("failed to prepare insert: {}", e)))?;

        // Import edges: each import binding → the definition it names.
        for s in syms.iter().filter(|s| s.kind == "import") {
            if let Some(dst) = resolve_def(&defs_by_name, &s.name) {
                if dst != s.id {
                    written += insert
                        .execute(params![s.id, dst, "imports"])
                        .map_err(|e| PyRuntimeError::new_err(format!("failed to write edge: {}", e)))?;
                }
            }
        }

        // Call edges (self-calls allowed → recursion self-edge).
        for (src_id, dst_id) in &call_edges {
            written += insert
                .execute(params![src_id, dst_id, "calls"])
                .map_err(|e| PyRuntimeError::new_err(format!("failed to write edge: {}", e)))?;
        }
    }

    tx.commit()
        .map_err(|e| PyRuntimeError::new_err(format!("failed to commit edges: {}", e)))?;

    Ok(written)
}
