//! Feature 9.2 — the "main spine": the longest execution path from an entrypoint.
//!
//! The call graph is cyclic (recursion), so it is first **SCC-condensed** (shared
//! Tarjan, `crate::graph`) into a DAG where cycles collapse to one node — which
//! also makes longest-path terminating. The longest path from the entry's
//! component is then found by topological-order DP, and each component on it is
//! expanded back to its member symbols (a collapsed cycle flags `in_cycle`).
//! Nodes carry a `layer` (depth along the spine) so the result renders top-down
//! like the Feature 4.10 layered view, and is reused by 9.4's global flow view.

use pyo3::exceptions::{PyFileNotFoundError, PyOSError, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rusqlite::Connection;
use std::collections::{HashMap, HashSet, VecDeque};
use std::path::Path;

use crate::graph::tarjan_scc;

struct Sym {
    id: i64,
    name: String,
    kind: String,
    path: String,
    line: Option<i64>,
}

/// Resolve the entry symbol name: the explicit `entry` argument, else the
/// primary entrypoint inferred by Feature 9.1.
fn resolve_entry(py: Python<'_>, db_path: &str, entry: Option<&str>) -> PyResult<Option<String>> {
    if let Some(e) = entry {
        return Ok(Some(e.to_string()));
    }
    let eps = crate::entrypoints::infer_entrypoints(py, db_path)?;
    for item in eps.bind(py).iter() {
        let d = item.downcast::<PyDict>().map_err(PyErr::from)?;
        let is_primary = d
            .get_item("primary")?
            .map_or(false, |v| v.extract::<bool>().unwrap_or(false));
        if is_primary {
            if let Some(sym) = d.get_item("symbol")? {
                return Ok(Some(sym.extract::<String>()?));
            }
        }
    }
    Ok(None)
}

/// Build the "main spine" from `entry` (or Feature 9.1's primary). Returns
/// `{entry, nodes: [{symbol, file, line, layer, in_cycle}], edges}`.
#[pyfunction]
#[pyo3(signature = (db_path, entry=None))]
pub fn main_spine(py: Python<'_>, db_path: &str, entry: Option<&str>) -> PyResult<Py<PyDict>> {
    if !Path::new(db_path).exists() {
        return Err(PyFileNotFoundError::new_err(format!(
            "database not found: '{}'",
            db_path
        )));
    }
    let conn = Connection::open(db_path)
        .map_err(|e| PyOSError::new_err(format!("cannot open database '{}': {}", db_path, e)))?;

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

    // Resolve the entry name (may be None on an empty graph / no primary).
    let entry_name = resolve_entry(py, db_path, entry)?;

    let empty = |name: Option<&str>| -> PyResult<Py<PyDict>> {
        let d = PyDict::new(py);
        d.set_item("entry", name)?;
        d.set_item("nodes", PyList::empty(py))?;
        d.set_item("edges", PyList::empty(py))?;
        Ok(d.unbind())
    };

    let entry_name = match entry_name {
        Some(e) => e,
        None => return empty(None),
    };

    // Definition symbols form the call-graph node set.
    let detail: HashMap<i64, &Sym> = syms.iter().map(|s| (s.id, s)).collect();
    let def_ids: Vec<i64> = syms
        .iter()
        .filter(|s| s.kind == "function" || s.kind == "class")
        .map(|s| s.id)
        .collect();
    let idx_of: HashMap<i64, usize> = def_ids.iter().enumerate().map(|(i, &id)| (id, i)).collect();
    let n = def_ids.len();

    let entry_ids: Vec<i64> = syms
        .iter()
        .filter(|s| s.name == entry_name && (s.kind == "function" || s.kind == "class"))
        .map(|s| s.id)
        .collect();
    if n == 0 || entry_ids.is_empty() {
        return empty(Some(&entry_name)); // unknown/empty entry -> empty spine
    }

    // Compact adjacency (also detect self-loops for `in_cycle`).
    let mut adj: Vec<Vec<usize>> = vec![Vec::new(); n];
    let mut self_loop = vec![false; n];
    for &(src, dst) in &call_edges {
        if let (Some(&u), Some(&v)) = (idx_of.get(&src), idx_of.get(&dst)) {
            adj[u].push(v);
            if u == v {
                self_loop[u] = true;
            }
        }
    }

    // Condense to the SCC-DAG.
    let comp = tarjan_scc(n, &adj);
    let n_comp = comp.iter().copied().max().map_or(0, |m| m + 1);
    let mut comp_adj: Vec<HashSet<usize>> = vec![HashSet::new(); n_comp];
    for u in 0..n {
        for &v in &adj[u] {
            if comp[u] != comp[v] {
                comp_adj[comp[u]].insert(comp[v]);
            }
        }
    }

    // Entry components.
    let entry_comps: HashSet<usize> =
        entry_ids.iter().filter_map(|id| idx_of.get(id)).map(|&i| comp[i]).collect();

    // Reachable components from the entry (downward closure over the DAG).
    let mut reachable: HashSet<usize> = entry_comps.clone();
    let mut queue: VecDeque<usize> = entry_comps.iter().copied().collect();
    while let Some(c) = queue.pop_front() {
        for &d in &comp_adj[c] {
            if reachable.insert(d) {
                queue.push_back(d);
            }
        }
    }

    // Topological order of the reachable sub-DAG (Kahn): entry comps have
    // in-degree 0 within the reachable set.
    let mut indeg: HashMap<usize, usize> = reachable.iter().map(|&c| (c, 0usize)).collect();
    for &c in &reachable {
        for &d in &comp_adj[c] {
            if reachable.contains(&d) {
                *indeg.get_mut(&d).unwrap() += 1;
            }
        }
    }
    let mut topo: Vec<usize> = Vec::new();
    let mut ready: VecDeque<usize> =
        reachable.iter().copied().filter(|c| indeg[c] == 0).collect();
    while let Some(c) = ready.pop_front() {
        topo.push(c);
        for &d in &comp_adj[c] {
            if let Some(id) = indeg.get_mut(&d) {
                *id -= 1;
                if *id == 0 {
                    ready.push_back(d);
                }
            }
        }
    }

    // Longest-path DP from the entry (by hop count). `depth` = longest distance
    // from any entry component; `pred` reconstructs the path.
    let mut depth: HashMap<usize, i64> = entry_comps.iter().map(|&c| (c, 0i64)).collect();
    let mut pred: HashMap<usize, usize> = HashMap::new();
    for &c in &topo {
        let dc = match depth.get(&c) {
            Some(&d) => d,
            None => continue, // not on any entry-rooted path
        };
        // Deterministic order of successors.
        let mut succ: Vec<usize> = comp_adj[c].iter().copied().filter(|d| reachable.contains(d)).collect();
        succ.sort_unstable();
        for d in succ {
            if dc + 1 > *depth.get(&d).unwrap_or(&i64::MIN) {
                depth.insert(d, dc + 1);
                pred.insert(d, c);
            }
        }
    }

    // Deepest component (ties → smallest comp id for determinism).
    let target = depth
        .iter()
        .max_by(|a, b| a.1.cmp(b.1).then(b.0.cmp(a.0)))
        .map(|(&c, _)| c);
    let target = match target {
        Some(t) => t,
        None => return empty(Some(&entry_name)),
    };

    // Reconstruct the spine component path (entry -> target).
    let mut path_comps: Vec<usize> = Vec::new();
    let mut cur = target;
    loop {
        path_comps.push(cur);
        match pred.get(&cur) {
            Some(&p) => cur = p,
            None => break, // reached an entry component
        }
    }
    path_comps.reverse();
    let layer_of: HashMap<usize, i64> =
        path_comps.iter().map(|&c| (c, depth[&c])).collect();

    // Expand each spine component to its member symbols.
    let mut members_by_comp: HashMap<usize, Vec<i64>> = HashMap::new();
    for (i, &id) in def_ids.iter().enumerate() {
        members_by_comp.entry(comp[i]).or_default().push(id);
    }
    let spine_ids: HashSet<i64> = path_comps
        .iter()
        .flat_map(|c| members_by_comp.get(c).cloned().unwrap_or_default())
        .collect();

    let nodes = PyList::empty(py);
    // Emit ordered by layer, then name, for stable output.
    let mut ordered: Vec<(i64, i64, bool)> = Vec::new(); // (id, layer, in_cycle)
    for &c in &path_comps {
        let members = members_by_comp.get(&c).cloned().unwrap_or_default();
        let in_cycle = members.len() > 1 || members.iter().any(|id| {
            idx_of.get(id).map_or(false, |&i| self_loop[i])
        });
        for id in members {
            ordered.push((id, layer_of[&c], in_cycle));
        }
    }
    ordered.sort_by(|a, b| {
        let na = detail.get(&a.0).map(|s| s.name.as_str()).unwrap_or("");
        let nb = detail.get(&b.0).map(|s| s.name.as_str()).unwrap_or("");
        a.1.cmp(&b.1).then(na.cmp(nb))
    });
    for (id, layer, in_cycle) in &ordered {
        let s = detail.get(id).expect("spine id came from detail");
        let d = PyDict::new(py);
        d.set_item("symbol", &s.name)?;
        d.set_item("file", &s.path)?;
        d.set_item("line", s.line)?;
        d.set_item("layer", *layer)?;
        d.set_item("in_cycle", *in_cycle)?;
        nodes.append(d)?;
    }

    // Call edges among the spine's member symbols.
    let edges = PyList::empty(py);
    for &(src, dst) in &call_edges {
        if spine_ids.contains(&src) && spine_ids.contains(&dst) {
            let e = PyDict::new(py);
            e.set_item("source", src)?;
            e.set_item("target", dst)?;
            edges.append(e)?;
        }
    }

    let out = PyDict::new(py);
    out.set_item("entry", &entry_name)?;
    out.set_item("nodes", nodes)?;
    out.set_item("edges", edges)?;
    Ok(out.unbind())
}
