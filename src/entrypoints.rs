//! Feature 9.1 — global entrypoint inference (deterministic, no LLM).
//!
//! The Feature 4.3 "entry points" heuristic (any function with no inbound call)
//! yields dozens of leaves — not *the* entrypoint of the system. This infers the
//! real start(s) by combining two signals and ranking:
//!
//! - **Convention markers (cheap subset, option b):** a top-level function named
//!   `main`, and functions invoked inside an `if __name__ == "__main__":` guard.
//!   Richer framework markers (console_scripts / routes / CLI) are Feature 9.1.1
//!   (issue #65).
//! - **Topological dominance:** SCC-condense the call graph (Tarjan), take the
//!   root strongly-connected components (zero in-degree in the condensed DAG),
//!   and rank every candidate by the size of its reachable set (cycle-safe BFS
//!   over outbound `calls`). The 'main' entrypoint reaches ~everything.
//!
//! Candidates are the root-SCC members *plus* any marked symbol (a marker
//! overrides topology — `main` is usually called by its guard, so it is not a
//! graph root). Results are ranked markers-first, then by reach; rank 1 is the
//! designated **primary** entrypoint.

use pyo3::exceptions::{PyFileNotFoundError, PyOSError, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rusqlite::Connection;
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use tree_sitter::{Node, Parser};

struct Sym {
    id: i64,
    name: String,
    kind: String,
    path: String,
    line: Option<i64>,
    line_end: Option<i64>,
}

fn node_text(node: Node, src: &[u8]) -> String {
    node.utf8_text(src).unwrap_or("").to_string()
}

use crate::graph::tarjan_scc;

/// Callee names for every call site under `node` (identifier or attribute name),
/// used to resolve what an `if __name__ == "__main__"` guard invokes.
fn collect_call_names(node: Node, src: &[u8], out: &mut Vec<String>) {
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
                    out.push(name);
                }
            }
        }
        collect_call_names(child, src, out);
    }
}

/// Recursively find `if __name__ == "__main__":` guards and record the symbols
/// invoked inside them (resolved by unique name) as `main_guard` markers.
fn find_main_guards(
    node: Node,
    src: &[u8],
    defs_by_name: &HashMap<String, Vec<i64>>,
    markers: &mut HashMap<i64, &'static str>,
) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "if_statement" {
            if let Some(cond) = child.child_by_field_name("condition") {
                let text = node_text(cond, src);
                if text.contains("__name__") && text.contains("__main__") {
                    if let Some(body) = child.child_by_field_name("consequence") {
                        let mut names = Vec::new();
                        collect_call_names(body, src, &mut names);
                        for name in names {
                            if let Some([only]) = defs_by_name.get(&name).map(Vec::as_slice) {
                                markers.entry(*only).or_insert("main_guard");
                            }
                        }
                    }
                }
            }
        }
        find_main_guards(child, src, defs_by_name, markers);
    }
}

// --------------------------------------------------------------------------- //
// Feature 9.1.1 — framework markers (decorators + declarative console_scripts)
// --------------------------------------------------------------------------- //

/// Map a decorator to an entrypoint marker kind by its method name (framework-
/// agnostic): `route`/`get`/`post`/… → `web_route` (Flask/FastAPI); `command`/
/// `group`/`callback` → `cli` (click/typer). Unknown decorators → None (ignored).
fn decorator_marker_kind(decorator: Node, src: &[u8]) -> Option<&'static str> {
    let mut c = decorator.walk();
    let expr = decorator.named_children(&mut c).next()?; // the expression after '@'
    let method = match expr.kind() {
        "call" => {
            let f = expr.child_by_field_name("function")?;
            match f.kind() {
                "attribute" => f.child_by_field_name("attribute").map(|a| node_text(a, src)),
                "identifier" => Some(node_text(f, src)),
                _ => None,
            }
        }
        "attribute" => expr.child_by_field_name("attribute").map(|a| node_text(a, src)),
        "identifier" => Some(node_text(expr, src)),
        _ => None,
    }?;
    match method.as_str() {
        "route" | "get" | "post" | "put" | "delete" | "patch" | "head" | "options"
        | "websocket" => Some("web_route"),
        "command" | "group" | "callback" => Some("cli"),
        _ => None,
    }
}

/// Record decorated functions whose decorator marks a framework entrypoint.
fn scan_decorators(
    node: Node,
    src: &[u8],
    path: &str,
    sym_by_pos: &HashMap<(String, i64), i64>,
    markers: &mut HashMap<i64, &'static str>,
) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "decorated_definition" {
            if let Some(def) = child.child_by_field_name("definition") {
                if def.kind() == "function_definition" {
                    let line = def.start_position().row as i64 + 1;
                    if let Some(&id) = sym_by_pos.get(&(path.to_string(), line)) {
                        let mut dc = child.walk();
                        for deco in child.children(&mut dc) {
                            if deco.kind() == "decorator" {
                                if let Some(kind) = decorator_marker_kind(deco, src) {
                                    markers.entry(id).or_insert(kind);
                                }
                            }
                        }
                    }
                }
            }
        }
        scan_decorators(child, src, path, sym_by_pos, markers);
    }
}

/// Does `file_path` correspond to the dotted `module`? (`pkg.mod` matches
/// `.../pkg/mod.py`; `pkg` matches `.../pkg/__init__.py`.) For disambiguating a
/// `console_scripts` target among same-named functions.
fn file_matches_module(file_path: &str, module: &str) -> bool {
    let stripped = file_path.strip_suffix(".py").unwrap_or(file_path);
    let mut path_comps: Vec<&str> =
        stripped.split(['/', '\\']).filter(|c| !c.is_empty() && *c != ".").collect();
    if path_comps.last() == Some(&"__init__") {
        path_comps.pop();
    }
    let mod_comps: Vec<&str> = module.split('.').filter(|c| !c.is_empty()).collect();
    !mod_comps.is_empty() && path_comps.ends_with(&mod_comps)
}

/// Longest shared leading path of two directories.
fn common_prefix(a: &Path, b: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for (x, y) in a.components().zip(b.components()) {
        if x == y {
            out.push(x.as_os_str());
        } else {
            break;
        }
    }
    out
}

/// The nearest ancestor of the indexed files that holds a `pyproject.toml` or
/// `setup.cfg` — the project root where `console_scripts` are declared.
fn find_project_root(files: &[String]) -> Option<PathBuf> {
    let mut common: Option<PathBuf> = None;
    for f in files {
        let dir = Path::new(f).parent().unwrap_or_else(|| Path::new("")).to_path_buf();
        common = Some(match common {
            None => dir,
            Some(c) => common_prefix(&c, &dir),
        });
    }
    let mut dir = common;
    let mut hops = 0;
    while let Some(d) = dir {
        if d.join("pyproject.toml").is_file() || d.join("setup.cfg").is_file() {
            return Some(d);
        }
        hops += 1;
        if hops > 40 {
            return None;
        }
        dir = d.parent().map(Path::to_path_buf);
    }
    None
}

/// Parse a TOML script table (`[project.scripts]` / `[tool.poetry.scripts]`):
/// each `name = "module:function"` yields `(Some(module), function)`. A small
/// hand parser (no toml dependency); malformed lines are skipped.
fn parse_toml_scripts(text: &str, section: &str, out: &mut Vec<(Option<String>, String)>) {
    let mut in_section = false;
    for line in text.lines() {
        let t = line.trim();
        if t.starts_with('[') {
            in_section = t == section;
            continue;
        }
        if !in_section || t.is_empty() || t.starts_with('#') {
            continue;
        }
        if let Some(eq) = t.find('=') {
            let value = t[eq + 1..].trim().trim_matches(['"', '\'']);
            if let Some((module, func)) = value.split_once(':') {
                let func = func.split_whitespace().next().unwrap_or(func);
                if !func.is_empty() {
                    out.push((Some(module.to_string()), func.to_string()));
                }
            }
        }
    }
}

/// Parse `console_scripts` under `[options.entry_points]` in setup.cfg (indented
/// `name = module:function` continuation lines).
fn parse_setup_cfg_console_scripts(text: &str, out: &mut Vec<(Option<String>, String)>) {
    let mut in_entry_points = false;
    let mut in_console = false;
    for line in text.lines() {
        let t = line.trim();
        if t.starts_with('[') {
            in_entry_points = t == "[options.entry_points]";
            in_console = false;
            continue;
        }
        if !in_entry_points {
            continue;
        }
        let indented = line.starts_with([' ', '\t']);
        if !indented {
            // A key at section level (e.g. `console_scripts =`).
            in_console = t.split_once('=').map_or(false, |(k, _)| k.trim() == "console_scripts");
            continue;
        }
        if in_console && !t.is_empty() && !t.starts_with('#') {
            if let Some((_name, value)) = t.split_once('=') {
                if let Some((module, func)) = value.trim().split_once(':') {
                    let func = func.split_whitespace().next().unwrap_or(func);
                    if !func.is_empty() {
                        out.push((Some(module.to_string()), func.to_string()));
                    }
                }
            }
        }
    }
}

/// Infer and rank the program's entrypoints. Returns a ranked list of dicts:
/// `{symbol, file, reachable, is_marker, marker_kind, rank, primary}`.
#[pyfunction]
pub fn infer_entrypoints(py: Python<'_>, db_path: &str) -> PyResult<Py<PyList>> {
    if !Path::new(db_path).exists() {
        return Err(PyFileNotFoundError::new_err(format!(
            "database not found: '{}'",
            db_path
        )));
    }
    let conn = Connection::open(db_path)
        .map_err(|e| PyOSError::new_err(format!("cannot open database '{}': {}", db_path, e)))?;

    // Symbols.
    let syms: Vec<Sym> = {
        let mut stmt = conn
            .prepare(
                "SELECT s.id, s.name, s.kind, f.path, s.line_start, s.line_end \
                 FROM symbols s JOIN files f ON f.id = s.file_id",
            )
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        let rows = stmt
            .query_map([], |r| {
                Ok(Sym {
                    id: r.get(0)?,
                    name: r.get(1)?,
                    kind: r.get(2)?,
                    path: r.get(3)?,
                    line: r.get(4)?,
                    line_end: r.get(5)?,
                })
            })
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        rows.collect::<Result<_, _>>()
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?
    };

    // Call edges.
    let call_edges: Vec<(i64, i64)> = {
        let mut stmt = conn
            .prepare("SELECT src_id, dst_id FROM edges WHERE kind = 'calls'")
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
        let rows = stmt
            .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?)))
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
        rows.collect::<Result<_, _>>()
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?
    };

    // Total degree over calls + imports + inherits (both directions) — the
    // centrality signal used to rank a *library*'s public API, so the central
    // class (many inbound uses/subclasses) is designated primary, not an
    // arbitrary deep-reaching function.
    let mut degree: HashMap<i64, i64> = HashMap::new();
    {
        let mut stmt = conn
            .prepare("SELECT src_id, dst_id FROM edges WHERE kind IN ('calls', 'imports', 'inherits')")
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
        let rows = stmt
            .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?)))
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
        for row in rows {
            let (s, d) = row.map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
            *degree.entry(s).or_insert(0) += 1;
            *degree.entry(d).or_insert(0) += 1;
        }
    }

    let list = PyList::empty(py);
    if syms.is_empty() {
        return Ok(list.unbind()); // empty graph -> empty
    }

    // Lookups.
    let detail: HashMap<i64, &Sym> = syms.iter().map(|s| (s.id, s)).collect();
    let mut defs_by_name: HashMap<String, Vec<i64>> = HashMap::new();
    let mut sym_by_pos: HashMap<(String, i64), i64> = HashMap::new(); // (path,line)->id
    for s in &syms {
        if s.kind == "function" || s.kind == "class" {
            defs_by_name.entry(s.name.clone()).or_default().push(s.id);
            if let Some(line) = s.line {
                sym_by_pos.insert((s.path.clone(), line), s.id);
            }
        }
    }

    // Outbound call adjacency (by id) for reachability.
    let mut adj_ids: HashMap<i64, Vec<i64>> = HashMap::new();
    for &(src, dst) in &call_edges {
        adj_ids.entry(src).or_default().push(dst);
    }

    // --- Markers (cheap subset): top-level `main` + `__main__` guards ---------
    let mut markers: HashMap<i64, &'static str> = HashMap::new();
    let mut parser = Parser::new();
    parser
        .set_language(&tree_sitter_python::LANGUAGE.into())
        .map_err(|e| PyRuntimeError::new_err(format!("failed to load Python grammar: {}", e)))?;
    // One reparse per distinct file.
    let mut files: Vec<String> = syms.iter().map(|s| s.path.clone()).collect();
    files.sort();
    files.dedup();
    for path in &files {
        let source = match std::fs::read_to_string(path) {
            Ok(s) => s,
            Err(_) => continue,
        };
        let tree = match parser.parse(source.as_bytes(), None) {
            Some(t) => t,
            None => continue,
        };
        let src = source.as_bytes();
        let root = tree.root_node();
        // Top-level `main` function.
        let mut mc = root.walk();
        for child in root.named_children(&mut mc) {
            if child.kind() == "function_definition" {
                if let Some(nm) = child.child_by_field_name("name") {
                    if node_text(nm, src) == "main" {
                        let line = child.start_position().row as i64 + 1;
                        if let Some(&id) = sym_by_pos.get(&(path.clone(), line)) {
                            markers.entry(id).or_insert("main");
                        }
                    }
                }
            }
        }
        // `if __name__ == "__main__":` guards.
        find_main_guards(root, src, &defs_by_name, &mut markers);
        // Feature 9.1.1 — framework decorators (routes / CLI commands).
        scan_decorators(root, src, path, &sym_by_pos, &mut markers);
    }

    // Feature 9.1.1 — declarative `console_scripts` (pyproject / setup.cfg).
    if let Some(root_dir) = find_project_root(&files) {
        let mut targets: Vec<(Option<String>, String)> = Vec::new();
        if let Ok(text) = std::fs::read_to_string(root_dir.join("pyproject.toml")) {
            parse_toml_scripts(&text, "[project.scripts]", &mut targets);
            parse_toml_scripts(&text, "[tool.poetry.scripts]", &mut targets);
        }
        if let Ok(text) = std::fs::read_to_string(root_dir.join("setup.cfg")) {
            parse_setup_cfg_console_scripts(&text, &mut targets);
        }
        for (module, func) in targets {
            match defs_by_name.get(&func).map(Vec::as_slice) {
                Some([only]) => {
                    markers.entry(*only).or_insert("console_script");
                }
                Some(many) if many.len() > 1 => {
                    if let Some(module) = &module {
                        let matches: Vec<i64> = many
                            .iter()
                            .copied()
                            .filter(|id| {
                                detail.get(id).map_or(false, |s| file_matches_module(&s.path, module))
                            })
                            .collect();
                        if matches.len() == 1 {
                            markers.entry(matches[0]).or_insert("console_script");
                        }
                    }
                }
                _ => {}
            }
        }
    }

    // --- Feature 9.7: library archetype → mark the public API surface --------
    // A library has no run/web entrypoint marker; its "entrypoints" are its
    // public (non-`_`) top-level functions/classes (methods excluded).
    let has_run = markers
        .values()
        .any(|&k| matches!(k, "main" | "main_guard" | "cli" | "console_script"));
    let has_web = markers.values().any(|&k| k == "web_route");
    let is_library = !has_run && !has_web;
    if is_library {
        let class_spans: Vec<(&str, i64, i64)> = syms
            .iter()
            .filter(|s| s.kind == "class")
            .filter_map(|s| Some((s.path.as_str(), s.line?, s.line_end?)))
            .collect();
        let is_method = |s: &Sym| -> bool {
            match s.line {
                Some(ls) => class_spans
                    .iter()
                    .any(|&(cp, cstart, cend)| cp == s.path && cstart < ls && ls <= cend),
                None => false,
            }
        };
        for s in &syms {
            let is_def = s.kind == "function" || s.kind == "class";
            let public_toplevel =
                !s.name.starts_with('_') && !(s.kind == "function" && is_method(s));
            if is_def && public_toplevel {
                markers.entry(s.id).or_insert("public_api");
            }
        }
    }

    // --- Root SCCs (zero in-degree in the condensed DAG) ----------------------
    // Node set = definition symbols (call graph endpoints).
    let def_ids: Vec<i64> = syms
        .iter()
        .filter(|s| s.kind == "function" || s.kind == "class")
        .map(|s| s.id)
        .collect();
    let idx_of: HashMap<i64, usize> = def_ids.iter().enumerate().map(|(i, &id)| (id, i)).collect();
    let n = def_ids.len();
    let mut adj: Vec<Vec<usize>> = vec![Vec::new(); n];
    for &(src, dst) in &call_edges {
        if let (Some(&u), Some(&v)) = (idx_of.get(&src), idx_of.get(&dst)) {
            adj[u].push(v);
        }
    }
    let comp = tarjan_scc(n, &adj);
    let n_comp = comp.iter().copied().max().map_or(0, |m| m + 1);
    // Condensed in-degree: an SCC has an inbound edge from a *different* SCC.
    let mut comp_has_inbound = vec![false; n_comp];
    for u in 0..n {
        for &v in &adj[u] {
            if comp[u] != comp[v] {
                comp_has_inbound[comp[v]] = true;
            }
        }
    }
    let root_ids: HashSet<i64> = (0..n)
        .filter(|&i| !comp_has_inbound[comp[i]])
        .map(|i| def_ids[i])
        .collect();

    // --- Candidates = root-SCC members ∪ markers (fallback: all defs) ---------
    let mut candidates: HashSet<i64> = root_ids;
    candidates.extend(markers.keys().copied());
    if candidates.is_empty() {
        candidates.extend(def_ids.iter().copied());
    }

    // Reachable-set size (cycle-safe BFS over outbound calls, excluding self).
    let reachable = |start: i64| -> usize {
        let mut visited: HashSet<i64> = HashSet::new();
        visited.insert(start);
        let mut frontier = vec![start];
        while let Some(u) = frontier.pop() {
            if let Some(neigh) = adj_ids.get(&u) {
                for &w in neigh {
                    if visited.insert(w) {
                        frontier.push(w);
                    }
                }
            }
        }
        visited.len() - 1 // exclude the start itself
    };

    // Rank: markers first, then by reach desc, then name asc. Candidates are
    // already definition symbols (roots / markers / def fallback).
    let deg = |id: i64| -> i64 { *degree.get(&id).unwrap_or(&0) };
    let mut ranked: Vec<(i64, usize, bool)> = candidates
        .iter()
        .filter(|&&id| detail.contains_key(&id))
        .map(|&id| (id, reachable(id), markers.contains_key(&id)))
        .collect();
    ranked.sort_by(|a, b| {
        let name_a = detail.get(&a.0).map(|s| s.name.as_str()).unwrap_or("");
        let name_b = detail.get(&b.0).map(|s| s.name.as_str()).unwrap_or("");
        let base = b.2.cmp(&a.2); // is_marker desc
        // A library has no single "start": rank its public API by centrality
        // (degree), so the central class wins. An application ranks by reach
        // from its markers (the real program start reaches the most).
        if is_library {
            base.then(deg(b.0).cmp(&deg(a.0))) // degree desc (centrality)
                .then(b.1.cmp(&a.1)) // reachable desc
                .then(name_a.cmp(name_b))
        } else {
            base.then(b.1.cmp(&a.1)) // reachable desc
                .then(deg(b.0).cmp(&deg(a.0)))
                .then(name_a.cmp(name_b))
        }
    });

    for (rank, (id, reach, is_marker)) in ranked.iter().enumerate() {
        let s = detail.get(id).expect("candidate came from detail");
        let d = PyDict::new(py);
        d.set_item("symbol", &s.name)?;
        d.set_item("file", &s.path)?;
        d.set_item("line", s.line)?;
        d.set_item("reachable", *reach)?;
        d.set_item("degree", deg(*id))?; // centrality (used to rank libraries)
        d.set_item("is_marker", *is_marker)?;
        d.set_item("marker_kind", markers.get(id).copied())?;
        d.set_item("rank", rank + 1)?;
        d.set_item("primary", rank == 0)?;
        d.set_item("is_library", is_library)?; // Feature 9.7 — repo archetype
        list.append(d)?;
    }
    Ok(list.unbind())
}
