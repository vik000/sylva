//! Graph writer — persists extracted symbols into the SQLite graph.
//!
//! Consumes the `{name, kind, line, line_end, docstring}` dicts produced by the
//! extractor (Feature 1.4) and writes them into the `files` / `symbols` tables
//! defined by the schema (Feature 1.2). The database must already be
//! initialised via `init_db`.
//!
//! Strategy: everything happens in a single transaction — upsert the file
//! record, delete that file's existing symbols, then insert the current set.
//! Delete-then-insert makes re-indexing a changed file clean (no stale rows)
//! and makes repeated writes of the same input idempotent (no duplicates).
//!
//! Robustness (per project rules):
//! - a symbol whose `kind` is the parse-error sentinel is *explicitly ignored*
//!   (not persisted);
//! - a malformed symbol dict (missing/!typed required keys) is *explicitly
//!   rejected* with a clear error, which aborts and rolls back the whole write;
//! - any SQLite failure mid-write rolls back the transaction — never a partial
//!   graph.

use pyo3::exceptions::{PyOSError, PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rusqlite::{params, Connection};

/// A symbol row ready to insert.
struct Row {
    name: String,
    kind: String,
    line_start: Option<i64>,
    line_end: Option<i64>,
    docstring: Option<String>,
    import_module: Option<String>,
    import_name: Option<String>,
}

/// Pull a required string value out of a symbol dict.
fn required_str(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<String> {
    match dict.get_item(key)? {
        Some(v) if !v.is_none() => v.extract().map_err(|_| {
            PyValueError::new_err(format!("symbol field '{}' must be a string", key))
        }),
        _ => Err(PyValueError::new_err(format!("symbol missing required '{}' field", key))),
    }
}

/// Pull an optional integer value (returns None if absent or Python `None`).
fn optional_int(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<i64>> {
    match dict.get_item(key)? {
        Some(v) if !v.is_none() => v
            .extract::<i64>()
            .map(Some)
            .map_err(|_| PyValueError::new_err(format!("symbol field '{}' must be an integer", key))),
        _ => Ok(None),
    }
}

/// Pull an optional string value (returns None if absent or Python `None`).
fn optional_str(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<String>> {
    match dict.get_item(key)? {
        Some(v) if !v.is_none() => v
            .extract::<String>()
            .map(Some)
            .map_err(|_| PyValueError::new_err(format!("symbol field '{}' must be a string", key))),
        _ => Ok(None),
    }
}

/// Write `symbols` for `file_path` into the graph database at `db_path`.
///
/// Returns the number of symbol rows written (parse-error sentinels are
/// skipped and not counted). Raises on an unusable database path, a malformed
/// symbol, or any write failure — in which case nothing is persisted.
#[pyfunction]
pub fn write_symbols(
    db_path: &str,
    file_path: &str,
    symbols: &Bound<'_, PyList>,
) -> PyResult<usize> {
    // Validate and normalise the input *before* touching the database, so a
    // bad symbol never leaves a half-written file record behind.
    let mut rows: Vec<Row> = Vec::with_capacity(symbols.len());
    for item in symbols.iter() {
        let dict = item.downcast::<PyDict>().map_err(|_| {
            PyTypeError::new_err("each symbol must be a dict")
        })?;

        let kind = required_str(dict, "kind")?;
        // Explicitly ignore the extractor's parse-error sentinel.
        if kind == "error" {
            continue;
        }

        rows.push(Row {
            name: required_str(dict, "name")?,
            kind,
            line_start: optional_int(dict, "line")?,
            line_end: optional_int(dict, "line_end")?,
            docstring: optional_str(dict, "docstring")?,
            import_module: optional_str(dict, "import_module")?,
            import_name: optional_str(dict, "import_name")?,
        });
    }

    let mut conn = Connection::open(db_path).map_err(|e| {
        PyOSError::new_err(format!("cannot open database '{}': {}", db_path, e))
    })?;
    conn.pragma_update(None, "foreign_keys", true).map_err(|e| {
        PyRuntimeError::new_err(format!("failed to enable foreign keys on '{}': {}", db_path, e))
    })?;

    let tx = conn.transaction().map_err(|e| {
        PyRuntimeError::new_err(format!("failed to begin transaction on '{}': {}", db_path, e))
    })?;

    // Upsert the file record and fetch its id.
    tx.execute(
        "INSERT INTO files (path, indexed_at) VALUES (?1, datetime('now')) \
         ON CONFLICT(path) DO UPDATE SET indexed_at = datetime('now')",
        params![file_path],
    )
    .map_err(|e| PyRuntimeError::new_err(format!("failed to upsert file '{}': {}", file_path, e)))?;

    let file_id: i64 = tx
        .query_row("SELECT id FROM files WHERE path = ?1", params![file_path], |r| r.get(0))
        .map_err(|e| PyRuntimeError::new_err(format!("failed to read file id for '{}': {}", file_path, e)))?;

    // Clear this file's previous symbols so re-indexing is clean/idempotent.
    tx.execute("DELETE FROM symbols WHERE file_id = ?1", params![file_id])
        .map_err(|e| PyRuntimeError::new_err(format!("failed to clear old symbols: {}", e)))?;

    let mut count = 0usize;
    {
        let mut stmt = tx
            .prepare(
                "INSERT INTO symbols \
                 (file_id, name, kind, line_start, line_end, docstring, import_module, import_name) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            )
            .map_err(|e| PyRuntimeError::new_err(format!("failed to prepare insert: {}", e)))?;

        for row in &rows {
            stmt.execute(params![
                file_id,
                row.name,
                row.kind,
                row.line_start,
                row.line_end,
                row.docstring,
                row.import_module,
                row.import_name
            ])
            .map_err(|e| {
                PyRuntimeError::new_err(format!("failed to write symbol '{}': {}", row.name, e))
            })?;
            count += 1;
        }
    }

    tx.commit()
        .map_err(|e| PyRuntimeError::new_err(format!("failed to commit write to '{}': {}", db_path, e)))?;

    Ok(count)
}
