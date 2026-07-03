//! SQLite schema and migrations for the Sylva knowledge graph.
//!
//! The data model (see DESIGN.md) is three tables plus supporting indexes:
//!   files   (id, path, hash, indexed_at)
//!   symbols (id, file_id, name, kind, line_start, line_end, docstring, coverage_pct)
//!   edges   (id, src_id, dst_id, kind)   -- kind: calls | imports | test_covers
//!
//! Migrations are applied idempotently and tracked via SQLite's built-in
//! `PRAGMA user_version`, so `init_db` is safe to call any number of times.

use pyo3::exceptions::{PyOSError, PyRuntimeError};
use pyo3::prelude::*;
use rusqlite::Connection;
use std::path::Path;

/// Ordered list of schema migrations. Index + 1 is the version a migration
/// brings the database *to*. Never edit an existing entry once shipped —
/// append a new one instead, so older databases upgrade cleanly.
const MIGRATIONS: &[&str] = &[
    // v1 — initial graph schema
    r#"
    CREATE TABLE IF NOT EXISTS files (
        id          INTEGER PRIMARY KEY,
        path        TEXT NOT NULL UNIQUE,
        hash        TEXT,
        indexed_at  TEXT
    );

    CREATE TABLE IF NOT EXISTS symbols (
        id           INTEGER PRIMARY KEY,
        file_id      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        name         TEXT NOT NULL,
        kind         TEXT NOT NULL,
        line_start   INTEGER,
        line_end     INTEGER,
        docstring    TEXT,
        coverage_pct REAL
    );

    CREATE TABLE IF NOT EXISTS edges (
        id      INTEGER PRIMARY KEY,
        src_id  INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
        dst_id  INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
        kind    TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
    CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
    CREATE INDEX IF NOT EXISTS idx_edges_src   ON edges(src_id);
    CREATE INDEX IF NOT EXISTS idx_edges_dst   ON edges(dst_id);
    CREATE INDEX IF NOT EXISTS idx_edges_kind  ON edges(kind);
    "#,
];

/// Apply any migrations the database has not yet seen, inside a single
/// transaction so a failure leaves the schema version untouched (no partial
/// upgrade). Returns the version the database is now at.
fn run_migrations(conn: &mut Connection) -> rusqlite::Result<i64> {
    let current: i64 = conn.pragma_query_value(None, "user_version", |row| row.get(0))?;
    let target = MIGRATIONS.len() as i64;

    if current >= target {
        return Ok(current);
    }

    let tx = conn.transaction()?;
    for version in current..target {
        tx.execute_batch(MIGRATIONS[version as usize])?;
    }
    // pragma_update cannot bind the value, but `target` is an internally
    // computed integer (never user input), so formatting it in is safe.
    tx.execute_batch(&format!("PRAGMA user_version = {};", target))?;
    tx.commit()?;

    Ok(target)
}

/// Create (or open) the SQLite database at `path` and bring its schema up to
/// the latest version. Idempotent: calling it on an already-initialised
/// database is a no-op that preserves existing data.
///
/// Errors are surfaced explicitly:
/// - filesystem problems (unwritable path, parent is not a directory) →
///   `OSError` with the offending path and OS message
/// - migration / SQLite problems → `RuntimeError` with the underlying message
#[pyfunction]
pub fn init_db(path: &str) -> PyResult<()> {
    // Ensure the parent directory exists (e.g. `.codemcp/`). An empty parent
    // means the db lives in the current directory — nothing to create.
    if let Some(parent) = Path::new(path).parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent).map_err(|e| {
                PyOSError::new_err(format!(
                    "cannot create parent directory for database '{}': {}",
                    path, e
                ))
            })?;
        }
    }

    let mut conn = Connection::open(path).map_err(|e| {
        PyOSError::new_err(format!("cannot open database '{}': {}", path, e))
    })?;

    // Enforce referential integrity (off by default in SQLite).
    conn.pragma_update(None, "foreign_keys", true).map_err(|e| {
        PyRuntimeError::new_err(format!(
            "failed to enable foreign keys on '{}': {}",
            path, e
        ))
    })?;

    run_migrations(&mut conn).map_err(|e| {
        PyRuntimeError::new_err(format!("schema migration failed for '{}': {}", path, e))
    })?;

    Ok(())
}
