//! Feature 9.3 — centrality: dominators + betweenness.
//!
//! Ranks symbols by structural importance *on paths* over the `calls` graph,
//! beyond the raw degree the viz already uses:
//!   - **betweenness** (Brandes' algorithm) — how many shortest paths run
//!     through a node; high = a chokepoint/bottleneck.
//!   - **dominators** — from a virtual super-source over the inferred
//!     entrypoints (Feature 9.1), each node's dominated-subtree size (`dominates`);
//!     high = a gateway that gates access to a large part of the code.
//!
//! Both are standard, deterministic graph algorithms. Cycles terminate (BFS
//! visited set; dominator fixpoint over reverse-postorder). Completes Epic 9.

use pyo3::exceptions::{PyFileNotFoundError, PyOSError, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rusqlite::Connection;
use std::collections::{HashMap, VecDeque};
use std::path::Path;

/// Brandes' betweenness centrality over a directed unweighted graph.
fn betweenness(n: usize, adj: &[Vec<usize>]) -> Vec<f64> {
    let mut bc = vec![0f64; n];
    for s in 0..n {
        let mut stack: Vec<usize> = Vec::new();
        let mut preds: Vec<Vec<usize>> = vec![Vec::new(); n];
        let mut sigma = vec![0f64; n];
        sigma[s] = 1.0;
        let mut dist = vec![-1i64; n];
        dist[s] = 0;
        let mut q = VecDeque::new();
        q.push_back(s);
        while let Some(v) = q.pop_front() {
            stack.push(v);
            for &w in &adj[v] {
                if dist[w] < 0 {
                    dist[w] = dist[v] + 1;
                    q.push_back(w);
                }
                if dist[w] == dist[v] + 1 {
                    sigma[w] += sigma[v];
                    preds[w].push(v);
                }
            }
        }
        let mut delta = vec![0f64; n];
        while let Some(w) = stack.pop() {
            for &v in &preds[w] {
                delta[v] += (sigma[v] / sigma[w]) * (1.0 + delta[w]);
            }
            if w != s {
                bc[w] += delta[w];
            }
        }
    }
    bc
}

/// Iterative reverse-postorder from `root` over `succ`.
fn reverse_postorder(root: usize, total: usize, succ: &[Vec<usize>]) -> Vec<usize> {
    let mut visited = vec![false; total];
    let mut order: Vec<usize> = Vec::new();
    // (node, next child index) work stack for iterative postorder.
    let mut work: Vec<(usize, usize)> = vec![(root, 0)];
    visited[root] = true;
    while let Some(&(u, ci)) = work.last() {
        if ci < succ[u].len() {
            work.last_mut().unwrap().1 += 1;
            let v = succ[u][ci];
            if !visited[v] {
                visited[v] = true;
                work.push((v, 0));
            }
        } else {
            order.push(u);
            work.pop();
        }
    }
    order.reverse();
    order
}

/// Immediate dominators (Cooper–Harvey–Kennedy). `total` nodes, `root` the
/// virtual super-source; returns `idom[node]` for nodes reachable from root
/// (`None` for unreachable; `idom[root] == root`).
fn dominators(total: usize, root: usize, succ: &[Vec<usize>]) -> Vec<Option<usize>> {
    let rpo = reverse_postorder(root, total, succ);
    let mut rpo_num = vec![usize::MAX; total];
    for (i, &node) in rpo.iter().enumerate() {
        rpo_num[node] = i;
    }
    let mut preds: Vec<Vec<usize>> = vec![Vec::new(); total];
    for (u, outs) in succ.iter().enumerate() {
        for &v in outs {
            preds[v].push(u);
        }
    }

    let mut idom = vec![None; total];
    idom[root] = Some(root);

    let intersect = |mut a: usize, mut b: usize, idom: &[Option<usize>]| -> usize {
        while a != b {
            while rpo_num[a] > rpo_num[b] {
                a = idom[a].unwrap();
            }
            while rpo_num[b] > rpo_num[a] {
                b = idom[b].unwrap();
            }
        }
        a
    };

    let mut changed = true;
    while changed {
        changed = false;
        for &b in &rpo {
            if b == root {
                continue;
            }
            let mut new_idom: Option<usize> = None;
            for &p in &preds[b] {
                if idom[p].is_some() {
                    new_idom = Some(match new_idom {
                        None => p,
                        Some(cur) => intersect(p, cur, &idom),
                    });
                }
            }
            if new_idom.is_some() && idom[b] != new_idom {
                idom[b] = new_idom;
                changed = true;
            }
        }
    }
    idom
}

/// Compute centrality metrics for every symbol. Returns a ranked list of
/// `{symbol, file, betweenness, dominates, degree}`. Raises FileNotFoundError
/// if the database does not exist.
#[pyfunction]
pub fn centrality(py: Python<'_>, db_path: &str) -> PyResult<Py<PyList>> {
    if !Path::new(db_path).exists() {
        return Err(PyFileNotFoundError::new_err(format!(
            "database not found: '{}'",
            db_path
        )));
    }
    let conn = Connection::open(db_path)
        .map_err(|e| PyOSError::new_err(format!("cannot open database '{}': {}", db_path, e)))?;

    // Definition symbols (call-graph nodes): (id, name, path).
    let defs: Vec<(i64, String, String)> = {
        let mut stmt = conn
            .prepare(
                "SELECT s.id, s.name, f.path FROM symbols s JOIN files f ON f.id = s.file_id \
                 WHERE s.kind IN ('function', 'class')",
            )
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        let rows = stmt
            .query_map([], |r| {
                Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?))
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

    let list = PyList::empty(py);
    let n = defs.len();
    if n == 0 {
        return Ok(list.unbind());
    }

    let idx_of: HashMap<i64, usize> = defs.iter().enumerate().map(|(i, d)| (d.0, i)).collect();
    let mut adj: Vec<Vec<usize>> = vec![Vec::new(); n];
    let mut degree = vec![0i64; n];
    for &(src, dst) in &call_edges {
        if let (Some(&u), Some(&v)) = (idx_of.get(&src), idx_of.get(&dst)) {
            adj[u].push(v);
            degree[u] += 1;
            degree[v] += 1;
        }
    }

    let bc = betweenness(n, &adj);

    // Dominators from a virtual super-source (index n) over the inferred
    // entrypoints (Feature 9.1).
    let root = n;
    let mut succ = adj.clone();
    succ.push(Vec::new()); // the virtual root's successors
    let pos_of: HashMap<(String, String), usize> =
        defs.iter().enumerate().map(|(i, d)| ((d.1.clone(), d.2.clone()), i)).collect();
    let eps = crate::entrypoints::infer_entrypoints(py, db_path)?;
    for item in eps.bind(py).iter() {
        let d = item.downcast::<PyDict>().map_err(PyErr::from)?;
        let name = d.get_item("symbol")?.and_then(|v| v.extract::<String>().ok());
        let file = d.get_item("file")?.and_then(|v| v.extract::<String>().ok());
        if let (Some(name), Some(file)) = (name, file) {
            if let Some(&i) = pos_of.get(&(name, file)) {
                succ[root].push(i);
            }
        }
    }

    let idom = dominators(n + 1, root, &succ);
    // Dominated-subtree sizes: children in the dom tree, then post-order sizes.
    let mut children: Vec<Vec<usize>> = vec![Vec::new(); n + 1];
    for b in 0..n {
        if b != root {
            if let Some(d) = idom[b] {
                children[d].push(b);
            }
        }
    }
    let mut subtree = vec![1i64; n + 1];
    // Post-order over the dom tree from root (iterative).
    let dom_order = {
        let mut order = Vec::new();
        let mut work = vec![(root, 0usize)];
        while let Some(&(u, ci)) = work.last() {
            if ci < children[u].len() {
                work.last_mut().unwrap().1 += 1;
                work.push((children[u][ci], 0));
            } else {
                order.push(u);
                work.pop();
            }
        }
        order
    };
    for &u in &dom_order {
        for &c in &children[u] {
            subtree[u] += subtree[c];
        }
    }
    // `dominates` = nodes strictly below (subtree minus self); unreachable -> 0.
    let dominates: Vec<i64> = (0..n)
        .map(|i| if idom[i].is_some() { subtree[i] - 1 } else { 0 })
        .collect();

    let mut ranked: Vec<usize> = (0..n).collect();
    ranked.sort_by(|&a, &b| {
        bc[b]
            .partial_cmp(&bc[a])
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(defs[a].1.cmp(&defs[b].1))
    });
    for i in ranked {
        let d = PyDict::new(py);
        d.set_item("symbol", &defs[i].1)?;
        d.set_item("file", &defs[i].2)?;
        d.set_item("betweenness", bc[i])?;
        d.set_item("dominates", dominates[i])?;
        d.set_item("degree", degree[i])?;
        list.append(d)?;
    }
    Ok(list.unbind())
}
