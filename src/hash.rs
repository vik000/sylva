//! File hash tracking for incremental indexing.
//!
//! Two responsibilities, deliberately split so the reindex decision is never
//! committed before the work it gates actually succeeds:
//!
//! - `file_needs_reindex` — *read-only*. Hashes the file and compares against
//!   the hash stored in the `files` table (the `hash` column from Feature 1.2).
//!   Returns true if new/changed, false if unchanged. It never writes.
//! - `mark_indexed` — persists the current hash, marking the file as done.
//!   Call this only AFTER a successful (re)index, so a crash mid-index leaves
//!   the file still flagged for reindex (safe) rather than prematurely skipped.
//!
//! The intended loop is therefore:
//!   if file_needs_reindex(db, f):
//!       write_symbols(db, f, extract_symbols(f))
//!       mark_indexed(db, f)          # checkpoint only on success
//!
//! Robustness (per project rules):
//! - an unreadable / missing file is *explicitly rejected* with a clear error;
//! - a failed hash write in `mark_indexed` is logged, *retried once*, and — if
//!   still failing — logged again without raising; the file simply stays
//!   flagged for reindex next run. No crash, no silent corruption.

use pyo3::exceptions::{PyFileNotFoundError, PyOSError, PyRuntimeError};
use pyo3::prelude::*;
use rusqlite::{params, Connection, OptionalExtension};
use sha2::{Digest, Sha256};
use std::io::Read;

/// Stream a file through SHA-256 and return the lowercase hex digest.
/// Chunked reads keep memory flat regardless of file size.
fn hash_file(path: &str) -> PyResult<String> {
    let file = std::fs::File::open(path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            PyFileNotFoundError::new_err(format!("file not found: '{}'", path))
        } else {
            PyOSError::new_err(format!("cannot read file '{}': {}", path, e))
        }
    })?;

    let mut reader = std::io::BufReader::new(file);
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 8192];
    loop {
        let n = reader
            .read(&mut buf)
            .map_err(|e| PyOSError::new_err(format!("error reading '{}': {}", path, e)))?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }

    let digest = hasher.finalize();
    Ok(digest.iter().map(|b| format!("{:02x}", b)).collect())
}

/// Open the graph database.
fn open_db(db_path: &str) -> PyResult<Connection> {
    Connection::open(db_path)
        .map_err(|e| PyOSError::new_err(format!("cannot open database '{}': {}", db_path, e)))
}

/// Upsert the stored hash for a file path.
fn store_hash(conn: &Connection, path: &str, hash: &str) -> rusqlite::Result<()> {
    conn.execute(
        "INSERT INTO files (path, hash) VALUES (?1, ?2) \
         ON CONFLICT(path) DO UPDATE SET hash = excluded.hash",
        params![path, hash],
    )?;
    Ok(())
}

/// Persist the hash, retrying once on failure. A persistent failure is logged
/// but not fatal: the file simply stays flagged for reindex on the next run.
fn store_hash_with_retry(conn: &Connection, path: &str, hash: &str) {
    if let Err(first) = store_hash(conn, path, hash) {
        eprintln!("sylva: hash update failed for '{}', retrying once: {}", path, first);
        if let Err(second) = store_hash(conn, path, hash) {
            eprintln!(
                "sylva: hash update retry failed for '{}'; file stays flagged for \
                 reindex, hash not persisted this round: {}",
                path, second
            );
        }
    }
}

/// SHA-256 (lowercase hex) of a file's contents. Exposed so a caller can hash
/// once and thread the exact value through the decision and the checkpoint,
/// closing the re-hash race (issue #29).
#[pyfunction]
pub fn file_hash(file_path: &str) -> PyResult<String> {
    hash_file(file_path)
}

/// Return whether `file_path` needs to be (re)indexed: true if it is new or its
/// contents changed since the stored hash, false if unchanged.
///
/// Read-only: this makes no changes to the database. Pass `hash` (from
/// `file_hash`) to compare a precomputed digest without re-reading the file, so
/// the decision and the later `mark_indexed` agree on exactly one hash.
/// Persisting the "indexed" state is the job of `mark_indexed`.
#[pyfunction]
#[pyo3(signature = (db_path, file_path, hash=None))]
pub fn file_needs_reindex(
    db_path: &str,
    file_path: &str,
    hash: Option<String>,
) -> PyResult<bool> {
    let current = match hash {
        Some(h) => h,
        None => hash_file(file_path)?,
    };
    let conn = open_db(db_path)?;

    // stored:
    //   None            -> no file record at all
    //   Some(None)      -> record exists but hash is NULL (e.g. written by the
    //                      graph writer, which doesn't set hash)
    //   Some(Some(h))   -> a previously stored hash
    let stored: Option<Option<String>> = conn
        .query_row("SELECT hash FROM files WHERE path = ?1", params![file_path], |r| r.get(0))
        .optional()
        .map_err(|e| PyRuntimeError::new_err(format!("failed to read stored hash: {}", e)))?;

    Ok(!matches!(stored, Some(Some(ref h)) if *h == current))
}

/// Record that `file_path` has been successfully indexed, by persisting its
/// SHA-256. Call this only AFTER the (re)index write has succeeded, so a failure
/// between the reindex decision and this checkpoint leaves the file flagged for
/// reindex rather than silently skipped.
///
/// Pass `hash` (from `file_hash`) — the exact digest of the content that was
/// indexed — to store *that* rather than re-hashing here; this closes the race
/// where the file changes between the index write and the checkpoint (issue
/// #29). If omitted, the current contents are re-hashed.
///
/// Best-effort persistence: a write failure is retried once and then logged,
/// never raised — the worst case is a harmless re-index next run.
#[pyfunction]
#[pyo3(signature = (db_path, file_path, hash=None))]
pub fn mark_indexed(db_path: &str, file_path: &str, hash: Option<String>) -> PyResult<()> {
    let current = match hash {
        Some(h) => h,
        None => hash_file(file_path)?,
    };
    let conn = open_db(db_path)?;
    store_hash_with_retry(&conn, file_path, &current);
    Ok(())
}
