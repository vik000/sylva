//! Feature 9.8 — project archetype + architectural layer inference.
//!
//! Deterministic (no LLM) classification that sits *above* files/dirs: infer
//! whether the repo is a library or an application/service, and for the latter
//! tag each symbol with an architectural tier — **interface** (routes / CLI),
//! **transport** (HTTP / queue / RPC clients), **data** (ORM / DB / cache), or
//! **business** (the default). A symbol inherits its file's layer, classified by
//! the module families the file imports (already stored on `import` symbols — no
//! re-parse) plus Feature 9.1.1's route/CLI markers.
//!
//! Cheap curated subset of frameworks; genuinely ambiguous files fall back to
//! `business` rather than guessing. `business` default, interface precedence.

use pyo3::exceptions::{PyFileNotFoundError, PyOSError, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rusqlite::Connection;
use std::collections::{HashMap, HashSet};
use std::path::Path;

// Curated import families (top-level package roots).
const WEB: &[&str] = &[
    "flask", "fastapi", "starlette", "django", "aiohttp", "bottle", "tornado", "sanic", "quart",
];
const QUEUE_RPC: &[&str] =
    &["kafka", "confluent_kafka", "celery", "pika", "grpc", "aio_pika", "nameko"];
const TRANSPORT: &[&str] = &[
    "requests", "httpx", "urllib3", "websockets", "kafka", "confluent_kafka", "celery", "pika",
    "grpc", "aio_pika", "nameko",
];
const DATA: &[&str] = &[
    "sqlalchemy", "psycopg", "psycopg2", "sqlite3", "pymongo", "redis", "mysql", "asyncpg",
    "peewee", "tortoise", "motor", "cassandra", "elasticsearch",
];

/// Top-level package root of an import (`sqlalchemy.orm` → `sqlalchemy`).
fn root_of(module: &str) -> &str {
    module.split('.').next().unwrap_or(module)
}

/// Infer the project archetype and per-symbol architectural layers.
///
/// Returns `{archetype, layers: [{symbol, file, layer}]}` for function/class
/// symbols. Raises `FileNotFoundError` if the database does not exist.
#[pyfunction]
pub fn infer_layers(py: Python<'_>, db_path: &str) -> PyResult<Py<PyDict>> {
    if !Path::new(db_path).exists() {
        return Err(PyFileNotFoundError::new_err(format!(
            "database not found: '{}'",
            db_path
        )));
    }
    let conn = Connection::open(db_path)
        .map_err(|e| PyOSError::new_err(format!("cannot open database '{}': {}", db_path, e)))?;

    // (id, name, kind, file_id, import_module, import_name)
    let syms: Vec<(i64, String, String, i64, Option<String>, Option<String>)> = {
        let mut stmt = conn
            .prepare("SELECT id, name, kind, file_id, import_module, import_name FROM symbols")
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        let rows = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, i64>(3)?,
                    r.get::<_, Option<String>>(4)?,
                    r.get::<_, Option<String>>(5)?,
                ))
            })
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        rows.collect::<Result<_, _>>()
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?
    };
    let files: HashMap<i64, String> = {
        let mut stmt = conn
            .prepare("SELECT id, path FROM files")
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read files: {}", e)))?;
        let rows = stmt
            .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read files: {}", e)))?;
        rows.collect::<Result<_, _>>()
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read files: {}", e)))?
    };

    // Per-file imported package roots (from `import` symbols; no re-parse).
    let mut file_imports: HashMap<i64, HashSet<String>> = HashMap::new();
    for (_, name, kind, file_id, import_module, import_name) in &syms {
        if kind != "import" {
            continue;
        }
        // Prefer the from-module, then the original imported name, then the binding.
        let key = import_module.as_deref().or(import_name.as_deref()).unwrap_or(name);
        file_imports.entry(*file_id).or_default().insert(root_of(key).to_string());
    }

    // Feature 9.1.1 markers: files defining routes / CLI commands are interface;
    // web/queue markers also signal a service archetype.
    let mut interface_files: HashSet<i64> = HashSet::new();
    let mut has_web_marker = false;
    let mut has_run_marker = false;
    let path_to_id: HashMap<&str, i64> =
        files.iter().map(|(id, p)| (p.as_str(), *id)).collect();
    let eps = crate::entrypoints::infer_entrypoints(py, db_path)?;
    for item in eps.bind(py).iter() {
        let d = item.downcast::<PyDict>().map_err(PyErr::from)?;
        let marker = d
            .get_item("marker_kind")?
            .and_then(|v| v.extract::<String>().ok());
        let file = d.get_item("file")?.and_then(|v| v.extract::<String>().ok());
        match marker.as_deref() {
            Some("web_route") => {
                has_web_marker = true;
                if let Some(id) = file.as_deref().and_then(|p| path_to_id.get(p)) {
                    interface_files.insert(*id);
                }
            }
            Some("cli") => {
                if let Some(id) = file.as_deref().and_then(|p| path_to_id.get(p)) {
                    interface_files.insert(*id);
                }
                has_run_marker = true;
            }
            Some("main") | Some("main_guard") | Some("console_script") => has_run_marker = true,
            _ => {}
        }
    }

    // Classify each file into a layer (interface > data > transport > business).
    let has_any = |roots: &HashSet<String>, set: &[&str]| roots.iter().any(|r| set.contains(&r.as_str()));
    let empty = HashSet::new();
    let layer_of_file = |file_id: i64| -> &'static str {
        let roots = file_imports.get(&file_id).unwrap_or(&empty);
        if interface_files.contains(&file_id) || has_any(roots, WEB) {
            "interface"
        } else if has_any(roots, DATA) {
            "data"
        } else if has_any(roots, TRANSPORT) {
            "transport"
        } else {
            "business"
        }
    };

    // Archetype.
    let mut has_service = has_web_marker;
    for roots in file_imports.values() {
        if has_any(roots, WEB) || has_any(roots, QUEUE_RPC) {
            has_service = true;
            break;
        }
    }
    let archetype = if has_service {
        "service"
    } else if has_run_marker {
        "application"
    } else {
        "library"
    };

    // Per-symbol layers (function/class definitions only), deterministically ordered.
    let mut out: Vec<(String, String, &'static str)> = syms
        .iter()
        .filter(|(_, _, kind, _, _, _)| kind == "function" || kind == "class")
        .map(|(_, name, _, file_id, _, _)| {
            let path = files.get(file_id).cloned().unwrap_or_default();
            (name.clone(), path, layer_of_file(*file_id))
        })
        .collect();
    out.sort_by(|a, b| a.1.cmp(&b.1).then(a.0.cmp(&b.0)));

    let layers = PyList::empty(py);
    for (symbol, file, layer) in out {
        let d = PyDict::new(py);
        d.set_item("symbol", symbol)?;
        d.set_item("file", file)?;
        d.set_item("layer", layer)?;
        layers.append(d)?;
    }
    let result = PyDict::new(py);
    result.set_item("archetype", archetype)?;
    result.set_item("layers", layers)?;
    Ok(result.unbind())
}
