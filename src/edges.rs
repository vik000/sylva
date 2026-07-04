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
//! several same-named definitions are skipped; unresolved names (builtins /
//! external / not indexed) are skipped. Skips are counted and reported as a
//! single bounded summary (issue #39) — set `SYLVA_LOG` for per-reference
//! detail. This never invents a false edge, and self-calls (recursion) produce
//! a self-edge. Edges use
//! `INSERT OR IGNORE` against the unique index (migration v2), so the pass is
//! idempotent. Returns the number of edges written.
//!
//! Precision limits of name-only resolution (tracked in issue #36): method
//! calls resolve by method name ignoring receiver type; there is no scope /
//! shadowing model; and genuinely-colliding names are skipped rather than
//! disambiguated. Type-aware resolution is future work.

use pyo3::exceptions::{PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rusqlite::{params, Connection};
use std::collections::{HashMap, HashSet};
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

/// Outcome of resolving a referenced name to a definition. Kept silent here so
/// the caller can aggregate counts and emit one bounded summary (issue #39).
enum Resolution {
    Resolved(i64),
    Ambiguous,
    Unresolved,
}

/// Resolve a referenced name to a unique definition, as `(symbol_id, file_id)`
/// pairs. When the name is globally ambiguous, prefer a definition in
/// `caller_file` (issue #36): a call to `get()` inside a module that defines
/// `get` resolves to *that* `get`, matching Python's module-scope resolution.
/// This recovers local calls without ever inventing a false edge.
fn resolve_def(
    defs_by_name: &HashMap<String, Vec<(i64, i64)>>,
    name: &str,
    caller_file: Option<i64>,
) -> Resolution {
    match defs_by_name.get(name).map(Vec::as_slice) {
        Some([(only, _)]) => Resolution::Resolved(*only),
        Some(many) if many.len() > 1 => {
            if let Some(file) = caller_file {
                let same_file: Vec<i64> =
                    many.iter().filter(|(_, f)| *f == file).map(|(id, _)| *id).collect();
                if same_file.len() == 1 {
                    return Resolution::Resolved(same_file[0]);
                }
            }
            Resolution::Ambiguous
        }
        _ => Resolution::Unresolved,
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
    let mut defs_by_name: HashMap<String, Vec<(i64, i64)>> = HashMap::new();
    let mut by_file: HashMap<i64, Vec<usize>> = HashMap::new();
    for (i, s) in syms.iter().enumerate() {
        if s.kind == "function" || s.kind == "class" {
            defs_by_name.entry(s.name.clone()).or_default().push((s.id, s.file_id));
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

    let verbose = crate::verbose();
    let mut skipped_ambiguous = 0usize;
    let mut skipped_unresolved = 0usize;

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
            match resolve_def(&defs_by_name, &callee, Some(*file_id)) {
                Resolution::Resolved(dst) => call_edges.push((caller.id, dst)),
                Resolution::Ambiguous => {
                    skipped_ambiguous += 1;
                    if verbose {
                        eprintln!("sylva: ambiguous call reference '{}'; skipping", callee);
                    }
                }
                Resolution::Unresolved => {
                    skipped_unresolved += 1;
                    if verbose {
                        eprintln!("sylva: unresolved call reference '{}'; skipping", callee);
                    }
                }
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
            // Imports point cross-file by nature, so same-file preference does
            // not apply — resolve globally (None).
            match resolve_def(&defs_by_name, &s.name, None) {
                Resolution::Resolved(dst) if dst != s.id => {
                    written += insert
                        .execute(params![s.id, dst, "imports"])
                        .map_err(|e| PyRuntimeError::new_err(format!("failed to write edge: {}", e)))?;
                }
                Resolution::Resolved(_) => {} // self-reference, skip
                Resolution::Ambiguous => skipped_ambiguous += 1,
                Resolution::Unresolved => skipped_unresolved += 1,
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

    // One bounded summary instead of a line per skipped reference (issue #39).
    if skipped_ambiguous + skipped_unresolved > 0 {
        eprintln!(
            "sylva: build_edges: {} edges; skipped {} ambiguous and {} unresolved reference(s){}",
            written,
            skipped_ambiguous,
            skipped_unresolved,
            if verbose { "" } else { " (set SYLVA_LOG=1 for per-reference detail)" }
        );
    }

    Ok(written)
}

// --------------------------------------------------------------------------- //
// Feature 4.1 — call chain tracing
// --------------------------------------------------------------------------- //

/// Breadth-first traversal from `start` over an adjacency map, up to `max_depth`
/// hops. Nodes are visited once (cycle-safe), and each newly-reached node is
/// pushed to `out` as `(id, depth, direction)`.
fn bfs(
    start: &[i64],
    adjacency: &HashMap<i64, Vec<i64>>,
    max_depth: i64,
    direction: &'static str,
    out: &mut Vec<(i64, i64, &'static str)>,
) {
    let mut visited: HashSet<i64> = start.iter().copied().collect();
    let mut frontier: Vec<i64> = start.to_vec();
    let mut depth = 1;
    while depth <= max_depth && !frontier.is_empty() {
        let mut next = Vec::new();
        for node in &frontier {
            if let Some(neighbors) = adjacency.get(node) {
                for &nb in neighbors {
                    if visited.insert(nb) {
                        out.push((nb, depth, direction));
                        next.push(nb);
                    }
                }
            }
        }
        frontier = next;
        depth += 1;
    }
}

/// Trace call chains from `symbol` over `calls` edges.
///
/// `direction`: `outbound` (callees), `inbound` (callers), or `both`. `depth` is
/// a strict cap on the number of hops. Returns a flat list of reached symbols as
/// `{name, kind, file, line, depth, direction}`, where `depth` 0 is the start
/// symbol itself (direction `self`). An unknown symbol yields an empty list; an
/// invalid direction or negative depth raises `ValueError`.
#[pyfunction]
pub fn trace_calls(
    py: Python<'_>,
    db_path: &str,
    symbol: &str,
    direction: &str,
    depth: i64,
) -> PyResult<Py<PyList>> {
    // Validate arguments first, before any DB work.
    if !matches!(direction, "inbound" | "outbound" | "both") {
        return Err(PyValueError::new_err(format!(
            "invalid direction '{}': expected 'inbound', 'outbound', or 'both'",
            direction
        )));
    }
    if depth < 0 {
        return Err(PyValueError::new_err("depth must be >= 0"));
    }
    if !Path::new(db_path).exists() {
        return Err(PyOSError::new_err(format!("database not found: '{}'", db_path)));
    }

    let conn = Connection::open(db_path)
        .map_err(|e| PyOSError::new_err(format!("cannot open database '{}': {}", db_path, e)))?;

    // Symbol details for the output, and name → ids for the start set.
    let mut detail: HashMap<i64, (String, String, Option<i64>, String)> = HashMap::new();
    let mut name_to_ids: HashMap<String, Vec<i64>> = HashMap::new();
    {
        let mut stmt = conn
            .prepare(
                "SELECT s.id, s.name, s.kind, s.line_start, f.path \
                 FROM symbols s JOIN files f ON f.id = s.file_id",
            )
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        let rows = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, Option<i64>>(3)?,
                    r.get::<_, String>(4)?,
                ))
            })
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        for row in rows {
            let (id, name, kind, line, path) =
                row.map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
            name_to_ids.entry(name.clone()).or_default().push(id);
            detail.insert(id, (name, kind, line, path));
        }
    }

    let start_ids: Vec<i64> = name_to_ids.get(symbol).cloned().unwrap_or_default();
    if start_ids.is_empty() {
        return Ok(PyList::empty(py).unbind()); // unknown symbol -> empty, not error
    }

    // Build call adjacency in both directions.
    let mut outgoing: HashMap<i64, Vec<i64>> = HashMap::new();
    let mut incoming: HashMap<i64, Vec<i64>> = HashMap::new();
    {
        let mut stmt = conn
            .prepare("SELECT src_id, dst_id FROM edges WHERE kind = 'calls'")
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
        let rows = stmt
            .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?)))
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
        for row in rows {
            let (src, dst) =
                row.map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
            outgoing.entry(src).or_default().push(dst);
            incoming.entry(dst).or_default().push(src);
        }
    }

    let mut reached: Vec<(i64, i64, &'static str)> = Vec::new();
    for &id in &start_ids {
        reached.push((id, 0, "self"));
    }
    if direction == "outbound" || direction == "both" {
        bfs(&start_ids, &outgoing, depth, "outbound", &mut reached);
    }
    if direction == "inbound" || direction == "both" {
        bfs(&start_ids, &incoming, depth, "inbound", &mut reached);
    }

    // Deterministic order: by depth, then name, then direction.
    reached.sort_by(|a, b| {
        let name_a = detail.get(&a.0).map(|d| d.0.as_str()).unwrap_or("");
        let name_b = detail.get(&b.0).map(|d| d.0.as_str()).unwrap_or("");
        a.1.cmp(&b.1).then(name_a.cmp(name_b)).then(a.2.cmp(b.2))
    });

    let list = PyList::empty(py);
    for (id, node_depth, node_dir) in reached {
        let (name, kind, line, path) = detail.get(&id).expect("id came from detail");
        let d = PyDict::new(py);
        d.set_item("name", name)?;
        d.set_item("kind", kind)?;
        d.set_item("file", path)?;
        d.set_item("line", *line)?;
        d.set_item("depth", node_depth)?;
        d.set_item("direction", node_dir)?;
        list.append(d)?;
    }
    Ok(list.unbind())
}

// --------------------------------------------------------------------------- //
// Feature 4.2 — blast radius analysis
// --------------------------------------------------------------------------- //

/// Everything that would break if `symbol` changed: the full transitive set of
/// symbols that depend on it, via inbound `calls` **and** `imports` edges.
///
/// Returns each affected symbol as `{name, kind, file, line, distance, via}`,
/// where `distance` is the number of hops from the target (1 = a direct
/// dependent) and `via` is the edge kind (`calls`/`imports`) it was first
/// reached by. The target itself is not included, so a leaf symbol (nothing
/// depends on it) returns an empty list. An unknown symbol also returns empty.
/// A `visited` set makes the traversal finite despite cycles.
///
/// Note (issue #37): `imports` edges from Feature 4.0 are name-conflated with
/// their target (and lost for aliased imports), so import relationships rarely
/// surface here yet. The `imports` traversal is correct and will benefit once
/// import-edge modeling is fixed.
#[pyfunction]
pub fn blast_radius(py: Python<'_>, db_path: &str, symbol: &str) -> PyResult<Py<PyList>> {
    if !Path::new(db_path).exists() {
        return Err(PyOSError::new_err(format!("database not found: '{}'", db_path)));
    }
    let conn = Connection::open(db_path)
        .map_err(|e| PyOSError::new_err(format!("cannot open database '{}': {}", db_path, e)))?;

    let mut detail: HashMap<i64, (String, String, Option<i64>, String)> = HashMap::new();
    let mut name_to_ids: HashMap<String, Vec<i64>> = HashMap::new();
    {
        let mut stmt = conn
            .prepare(
                "SELECT s.id, s.name, s.kind, s.line_start, f.path \
                 FROM symbols s JOIN files f ON f.id = s.file_id",
            )
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        let rows = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, Option<i64>>(3)?,
                    r.get::<_, String>(4)?,
                ))
            })
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        for row in rows {
            let (id, name, kind, line, path) =
                row.map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
            name_to_ids.entry(name.clone()).or_default().push(id);
            detail.insert(id, (name, kind, line, path));
        }
    }

    let start_ids: Vec<i64> = name_to_ids.get(symbol).cloned().unwrap_or_default();
    if start_ids.is_empty() {
        return Ok(PyList::empty(py).unbind());
    }

    // Inbound adjacency over calls + imports: for edge (src -> dst), `src`
    // depends on `dst`, so record `dst -> (src, kind)`.
    let mut inbound: HashMap<i64, Vec<(i64, &'static str)>> = HashMap::new();
    {
        let mut stmt = conn
            .prepare("SELECT src_id, dst_id, kind FROM edges WHERE kind IN ('calls', 'imports')")
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
        let rows = stmt
            .query_map([], |r| {
                Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?, r.get::<_, String>(2)?))
            })
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
        for row in rows {
            let (src, dst, kind) =
                row.map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
            let via: &'static str = if kind == "imports" { "imports" } else { "calls" };
            inbound.entry(dst).or_default().push((src, via));
        }
    }

    // BFS outward from the target over dependents. Unbounded depth; `visited`
    // guarantees termination. The target(s) are excluded from the output.
    let mut visited: HashSet<i64> = start_ids.iter().copied().collect();
    let mut frontier: Vec<i64> = start_ids.clone();
    let mut affected: Vec<(i64, i64, &'static str)> = Vec::new();
    let mut distance = 1;
    while !frontier.is_empty() {
        let mut next = Vec::new();
        for node in &frontier {
            if let Some(dependents) = inbound.get(node) {
                for &(dep, via) in dependents {
                    if visited.insert(dep) {
                        affected.push((dep, distance, via));
                        next.push(dep);
                    }
                }
            }
        }
        frontier = next;
        distance += 1;
    }

    affected.sort_by(|a, b| {
        let name_a = detail.get(&a.0).map(|d| d.0.as_str()).unwrap_or("");
        let name_b = detail.get(&b.0).map(|d| d.0.as_str()).unwrap_or("");
        a.1.cmp(&b.1).then(name_a.cmp(name_b))
    });

    let list = PyList::empty(py);
    for (id, dist, via) in affected {
        let (name, kind, line, path) = detail.get(&id).expect("id came from detail");
        let d = PyDict::new(py);
        d.set_item("name", name)?;
        d.set_item("kind", kind)?;
        d.set_item("file", path)?;
        d.set_item("line", *line)?;
        d.set_item("distance", dist)?;
        d.set_item("via", via)?;
        list.append(d)?;
    }
    Ok(list.unbind())
}
