//! Architecture summary — a top-level view of the indexed codebase.
//!
//! Pure aggregation over the graph (no parsing): the modules (files) with their
//! symbol counts, the most-connected symbols ("hubs"), and the entry points.
//!
//! - **hubs**: symbols ranked by total degree — inbound + outbound `calls` and
//!   `imports` edges — capped at `hub_limit`. A hub is heavily used and/or
//!   heavily using.
//! - **entry points**: function/class symbols that nothing calls (zero inbound
//!   `calls` edges), excluding `test_`-prefixed symbols — the top-level / main
//!   -like surface of the codebase.

use pyo3::exceptions::{PyFileNotFoundError, PyOSError, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rusqlite::Connection;
use std::collections::{HashMap, HashSet};
use std::path::Path;

/// Build the architecture summary dict for the graph at `db_path`.
///
/// Returns `{modules, hubs, entry_points}`. Empty sections for an empty
/// database; raises `FileNotFoundError` if the database does not exist.
#[pyfunction]
#[pyo3(signature = (db_path, hub_limit=10))]
pub fn get_architecture(py: Python<'_>, db_path: &str, hub_limit: usize) -> PyResult<Py<PyDict>> {
    if !Path::new(db_path).exists() {
        return Err(PyFileNotFoundError::new_err(format!(
            "database not found: '{}'",
            db_path
        )));
    }
    let conn = Connection::open(db_path)
        .map_err(|e| PyOSError::new_err(format!("cannot open database '{}': {}", db_path, e)))?;

    // Symbols: id -> (name, kind, path).
    let mut symbols: Vec<(i64, String, String, String)> = {
        let mut stmt = conn
            .prepare(
                "SELECT s.id, s.name, s.kind, f.path \
                 FROM symbols s JOIN files f ON f.id = s.file_id",
            )
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        let rows = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                ))
            })
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        rows.collect::<Result<_, _>>()
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?
    };

    // Edges: accumulate degree (calls + imports + inherits, both directions) and
    // the set of symbols with an inbound `calls` edge.
    let mut degree: HashMap<i64, i64> = HashMap::new();
    let mut has_inbound_call: HashSet<i64> = HashSet::new();
    {
        let mut stmt = conn
            .prepare("SELECT src_id, dst_id, kind FROM edges WHERE kind IN ('calls', 'imports', 'inherits')")
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
        let rows = stmt
            .query_map([], |r| {
                Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?, r.get::<_, String>(2)?))
            })
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
        for row in rows {
            let (src, dst, kind) =
                row.map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
            *degree.entry(src).or_insert(0) += 1;
            *degree.entry(dst).or_insert(0) += 1;
            if kind == "calls" {
                has_inbound_call.insert(dst);
            }
        }
    }

    // Modules: every file with its symbol count (files with no symbols included).
    let modules = PyList::empty(py);
    {
        let mut stmt = conn
            .prepare(
                "SELECT f.path, COUNT(s.id) \
                 FROM files f LEFT JOIN symbols s ON s.file_id = f.id \
                 GROUP BY f.id ORDER BY f.path",
            )
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read modules: {}", e)))?;
        let rows = stmt
            .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read modules: {}", e)))?;
        for row in rows {
            let (path, count) =
                row.map_err(|e| PyRuntimeError::new_err(format!("failed to read modules: {}", e)))?;
            let d = PyDict::new(py);
            d.set_item("path", path)?;
            d.set_item("symbols", count)?;
            modules.append(d)?;
        }
    }

    // Hubs: symbols with degree > 0, ranked by degree desc then name asc, top-N.
    let mut ranked: Vec<(&(i64, String, String, String), i64)> = symbols
        .iter()
        .filter_map(|s| degree.get(&s.0).filter(|&&d| d > 0).map(|&d| (s, d)))
        .collect();
    ranked.sort_by(|a, b| b.1.cmp(&a.1).then(a.0 .1.cmp(&b.0 .1)));

    let hubs = PyList::empty(py);
    for (sym, deg) in ranked.into_iter().take(hub_limit) {
        let d = PyDict::new(py);
        d.set_item("name", &sym.1)?;
        d.set_item("kind", &sym.2)?;
        d.set_item("file", &sym.3)?;
        d.set_item("degree", deg)?;
        hubs.append(d)?;
    }

    // Entry points: function/class symbols nothing calls, excluding test_ names.
    symbols.sort_by(|a, b| a.1.cmp(&b.1));
    let entry_points = PyList::empty(py);
    for (id, name, kind, path) in &symbols {
        let is_def = kind == "function" || kind == "class";
        if is_def && !name.starts_with("test_") && !has_inbound_call.contains(id) {
            let d = PyDict::new(py);
            d.set_item("name", name)?;
            d.set_item("kind", kind)?;
            d.set_item("file", path)?;
            entry_points.append(d)?;
        }
    }

    let out = PyDict::new(py);
    out.set_item("modules", modules)?;
    out.set_item("hubs", hubs)?;
    out.set_item("entry_points", entry_points)?;
    Ok(out.unbind())
}
