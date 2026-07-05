//! Feature 10.2 — test-target recommender (what to e2e-test, and why).
//!
//! Deterministic ranking of the symbols most worth writing end-to-end tests for:
//! those with **no inbound `test_covers` edge** (untested), ranked by importance
//! — inferred entrypoints (Feature 9.1) first, then centrality (Feature 9.3:
//! betweenness chokepoints, dominator gateways) and degree. Test code itself is
//! excluded. This tells the `generate-tests` skill *what* to target; Sylva runs
//! no tests — the agent writes them and the project's own workflow runs them.

use pyo3::exceptions::{PyFileNotFoundError, PyOSError, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rusqlite::Connection;
use std::collections::{HashMap, HashSet};
use std::path::Path;

/// Is `name`/`path` test code (excluded as a target)?
fn is_test_ctx(name: &str, path: &str) -> bool {
    if name.starts_with("test_") || name == "test" {
        return true;
    }
    let p = path.replace('\\', "/");
    let base = p.rsplit('/').next().unwrap_or("");
    base.starts_with("test_")
        || base.ends_with("_test.py")
        || p.split('/').any(|c| c == "tests" || c == "test")
}

fn get_str(d: &Bound<'_, PyDict>, key: &str) -> Option<String> {
    d.get_item(key).ok().flatten().and_then(|v| v.extract().ok())
}

/// Rank the untested symbols most worth e2e-testing. Returns
/// `[{symbol, file, reason, score}]`, highest score first. Raises
/// FileNotFoundError if the database does not exist.
#[pyfunction]
pub fn suggest_test_targets(py: Python<'_>, db_path: &str) -> PyResult<Py<PyList>> {
    if !Path::new(db_path).exists() {
        return Err(PyFileNotFoundError::new_err(format!(
            "database not found: '{}'",
            db_path
        )));
    }
    let conn = Connection::open(db_path)
        .map_err(|e| PyOSError::new_err(format!("cannot open database '{}': {}", db_path, e)))?;

    // Definition symbols (id, name, path).
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
    // Symbols with an inbound test_covers edge = already covered.
    let covered: HashSet<i64> = {
        let mut stmt = conn
            .prepare("SELECT DISTINCT dst_id FROM edges WHERE kind = 'test_covers'")
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
        let rows = stmt
            .query_map([], |r| r.get::<_, i64>(0))
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
        rows.collect::<Result<_, _>>()
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?
    };

    let list = PyList::empty(py);
    if defs.is_empty() {
        return Ok(list.unbind());
    }

    // Reuse 9.1 (entrypoints) + 9.3 (centrality) for importance, keyed by (name, file).
    let mut ep: HashMap<(String, String), (bool, Option<String>)> = HashMap::new();
    for item in crate::entrypoints::infer_entrypoints(py, db_path)?.bind(py).iter() {
        if let Ok(d) = item.downcast::<PyDict>() {
            if let (Some(s), Some(f)) = (get_str(d, "symbol"), get_str(d, "file")) {
                let primary = d
                    .get_item("primary")
                    .ok()
                    .flatten()
                    .and_then(|v| v.extract::<bool>().ok())
                    .unwrap_or(false);
                ep.insert((s, f), (primary, get_str(d, "marker_kind")));
            }
        }
    }
    let mut cen: HashMap<(String, String), (f64, i64, i64)> = HashMap::new();
    for item in crate::centrality::centrality(py, db_path)?.bind(py).iter() {
        if let Ok(d) = item.downcast::<PyDict>() {
            if let (Some(s), Some(f)) = (get_str(d, "symbol"), get_str(d, "file")) {
                let bt = d.get_item("betweenness").ok().flatten().and_then(|v| v.extract::<f64>().ok()).unwrap_or(0.0);
                let dom = d.get_item("dominates").ok().flatten().and_then(|v| v.extract::<i64>().ok()).unwrap_or(0);
                let deg = d.get_item("degree").ok().flatten().and_then(|v| v.extract::<i64>().ok()).unwrap_or(0);
                cen.insert((s, f), (bt, dom, deg));
            }
        }
    }

    // Score every untested, non-test definition.
    let mut ranked: Vec<(String, String, String, f64)> = Vec::new();
    for (id, name, path) in &defs {
        if covered.contains(id) || is_test_ctx(name, path) {
            continue;
        }
        let key = (name.clone(), path.clone());
        let (bt, dom, deg) = cen.get(&key).copied().unwrap_or((0.0, 0, 0));
        let (primary, marker) = ep.get(&key).cloned().unwrap_or((false, None));
        let is_ep = ep.contains_key(&key);

        let mut score = 0f64;
        let mut reasons: Vec<String> = Vec::new();
        if primary {
            score += 1000.0;
            reasons.push("primary entrypoint".to_string());
        } else if is_ep {
            score += 200.0;
            reasons.push(match &marker {
                Some(m) => format!("entrypoint ({})", m),
                None => "entrypoint".to_string(),
            });
        }
        if bt > 0.0 {
            score += bt * 10.0;
            reasons.push(format!("chokepoint (betweenness {:.1})", bt));
        }
        if dom > 0 {
            score += dom as f64 * 5.0;
            reasons.push(format!("gateway (dominates {})", dom));
        }
        score += deg as f64;
        if reasons.is_empty() {
            reasons.push("untested".to_string());
        }
        ranked.push((name.clone(), path.clone(), reasons.join("; "), score));
    }

    ranked.sort_by(|a, b| {
        b.3.partial_cmp(&a.3)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(a.0.cmp(&b.0))
    });
    for (symbol, file, reason, score) in ranked {
        let d = PyDict::new(py);
        d.set_item("symbol", symbol)?;
        d.set_item("file", file)?;
        d.set_item("reason", reason)?;
        d.set_item("score", score)?;
        list.append(d)?;
    }
    Ok(list.unbind())
}
