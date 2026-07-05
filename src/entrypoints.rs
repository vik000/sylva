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
use std::path::Path;
use tree_sitter::{Node, Parser};

struct Sym {
    id: i64,
    name: String,
    kind: String,
    path: String,
    line: Option<i64>,
}

fn node_text(node: Node, src: &[u8]) -> String {
    node.utf8_text(src).unwrap_or("").to_string()
}

/// Iterative Tarjan SCC — returns an SCC id per node (0-based, dense). Iterative
/// so a deep call graph can't overflow the stack.
fn tarjan_scc(n: usize, adj: &[Vec<usize>]) -> Vec<usize> {
    const UNSET: usize = usize::MAX;
    let mut index = vec![UNSET; n];
    let mut low = vec![0usize; n];
    let mut on_stack = vec![false; n];
    let mut comp = vec![UNSET; n];
    let mut stack: Vec<usize> = Vec::new();
    let mut next_index = 0usize;
    let mut next_comp = 0usize;

    for start in 0..n {
        if index[start] != UNSET {
            continue;
        }
        let mut work: Vec<(usize, usize)> = vec![(start, 0)]; // (node, next child)
        while let Some(&(v, ci)) = work.last() {
            if ci == 0 {
                index[v] = next_index;
                low[v] = next_index;
                next_index += 1;
                stack.push(v);
                on_stack[v] = true;
            }
            if ci < adj[v].len() {
                let w = adj[v][ci];
                work.last_mut().unwrap().1 += 1;
                if index[w] == UNSET {
                    work.push((w, 0));
                } else if on_stack[w] {
                    low[v] = low[v].min(index[w]);
                }
            } else {
                if low[v] == index[v] {
                    loop {
                        let w = stack.pop().unwrap();
                        on_stack[w] = false;
                        comp[w] = next_comp;
                        if w == v {
                            break;
                        }
                    }
                    next_comp += 1;
                }
                work.pop();
                if let Some(&(parent, _)) = work.last() {
                    low[parent] = low[parent].min(low[v]);
                }
            }
        }
    }
    comp
}

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
                "SELECT s.id, s.name, s.kind, f.path, s.line_start \
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
    let mut ranked: Vec<(i64, usize, bool)> = candidates
        .iter()
        .filter(|&&id| detail.contains_key(&id))
        .map(|&id| (id, reachable(id), markers.contains_key(&id)))
        .collect();
    ranked.sort_by(|a, b| {
        let name_a = detail.get(&a.0).map(|s| s.name.as_str()).unwrap_or("");
        let name_b = detail.get(&b.0).map(|s| s.name.as_str()).unwrap_or("");
        b.2.cmp(&a.2) // is_marker desc
            .then(b.1.cmp(&a.1)) // reachable desc
            .then(name_a.cmp(name_b)) // name asc
    });

    for (rank, (id, reach, is_marker)) in ranked.iter().enumerate() {
        let s = detail.get(id).expect("candidate came from detail");
        let d = PyDict::new(py);
        d.set_item("symbol", &s.name)?;
        d.set_item("file", &s.path)?;
        d.set_item("line", s.line)?;
        d.set_item("reachable", *reach)?;
        d.set_item("is_marker", *is_marker)?;
        d.set_item("marker_kind", markers.get(id).copied())?;
        d.set_item("rank", rank + 1)?;
        d.set_item("primary", rank == 0)?;
        list.append(d)?;
    }
    Ok(list.unbind())
}
