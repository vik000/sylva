//! File watcher — reindex the graph as files change on disk.
//!
//! Structured so the *logic* is testable without the *plumbing*:
//! - `reindex_path` — pure, synchronous. Runs the full incremental pipeline for
//!   one file (needs-reindex? → extract → write → checkpoint). Directly unit-
//!   tested.
//! - `handle_delete` — pure, synchronous. Removes a file's records; symbols and
//!   edges cascade away via the schema's `ON DELETE CASCADE`.
//! - `start_watcher` — wraps the two above with `notify` + a debounce loop on a
//!   background thread, returning a `WatcherHandle` that stops deterministically
//!   via `.stop()` (and best-effort on drop/GC).
//!
//! Robustness (per project rules): the watch loop never panics — a bad event or
//! a failed reindex is logged and the loop continues. A missing root is
//! explicitly rejected at start. Reindex decisions are checkpointed (via
//! `mark_indexed`) only after a successful write, inheriting Feature 2.1's
//! crash-safety.

use notify::{Event, RecommendedWatcher, RecursiveMode, Watcher};
use pyo3::exceptions::{PyFileNotFoundError, PyOSError, PyRuntimeError};
use pyo3::prelude::*;
use rusqlite::{params, Connection};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, TryRecvError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

fn is_python_file(path: &Path) -> bool {
    path.extension().map_or(false, |ext| ext == "py")
}

/// Reindex a single file: if its contents changed, re-extract its symbols,
/// write them, and checkpoint the hash. Returns true if it was (re)indexed,
/// false if skipped (non-`.py`, or unchanged since last index).
///
/// Pure and synchronous — this is the unit-testable core of the watcher.
#[pyfunction]
pub fn reindex_path(py: Python<'_>, db_path: &str, file_path: &str) -> PyResult<bool> {
    if !is_python_file(Path::new(file_path)) {
        return Ok(false);
    }
    // Decide (read-only) before doing any work.
    if !crate::hash::file_needs_reindex(db_path, file_path)? {
        return Ok(false);
    }
    // Extract → write → checkpoint, reusing the existing pipeline functions.
    let symbols = crate::extractor::extract_symbols(py, file_path)?;
    crate::writer::write_symbols(db_path, file_path, symbols.bind(py))?;
    crate::hash::mark_indexed(db_path, file_path)?;
    Ok(true)
}

/// Remove a deleted file's records from the graph. The file row is deleted and
/// its symbols/edges cascade away (schema `ON DELETE CASCADE`). Returns true if
/// a record was actually removed. Pure and synchronous.
#[pyfunction]
pub fn handle_delete(db_path: &str, file_path: &str) -> PyResult<bool> {
    let conn = Connection::open(db_path)
        .map_err(|e| PyOSError::new_err(format!("cannot open database '{}': {}", db_path, e)))?;
    // Cascade only fires with foreign keys enabled (off by default in SQLite).
    conn.pragma_update(None, "foreign_keys", true)
        .map_err(|e| PyRuntimeError::new_err(format!("failed to enable foreign keys: {}", e)))?;
    let removed = conn
        .execute("DELETE FROM files WHERE path = ?1", params![file_path])
        .map_err(|e| PyRuntimeError::new_err(format!("failed to delete '{}': {}", file_path, e)))?;
    Ok(removed > 0)
}

/// Apply one debounced path event: reindex if the file exists, delete its
/// records if it no longer does. All failures are logged, never propagated —
/// the watch loop must survive them.
fn process_path(db_path: &str, path: &Path) {
    if !is_python_file(path) {
        return;
    }
    let path_str = path.to_string_lossy().to_string();

    if path.exists() {
        // reindex_path needs the GIL (extract/write use Python objects).
        let outcome = Python::with_gil(|py| {
            reindex_path(py, db_path, &path_str).map_err(|e| e.to_string())
        });
        if let Err(msg) = outcome {
            eprintln!("sylva: reindex failed for '{}': {}", path_str, msg);
        }
    } else if let Err(e) = handle_delete(db_path, &path_str) {
        eprintln!("sylva: delete handling failed for '{}': {}", path_str, e);
    }
}

/// The background watch loop: receive raw events, debounce per-path, and flush
/// paths that have been quiet for `debounce`. Exits when signalled via `stop_rx`.
///
/// The `watcher` is already registered (in `start_watcher`) and is owned here
/// only to keep the OS watch alive for the lifetime of the loop; it drops — and
/// unregisters — when the loop exits.
fn run_watch_loop(
    _watcher: RecommendedWatcher,
    event_rx: Receiver<notify::Result<Event>>,
    db_path: String,
    debounce: Duration,
    stop_rx: Receiver<()>,
) {
    let mut pending: HashMap<PathBuf, Instant> = HashMap::new();
    loop {
        match stop_rx.try_recv() {
            Ok(()) | Err(TryRecvError::Disconnected) => break,
            Err(TryRecvError::Empty) => {}
        }

        match event_rx.recv_timeout(Duration::from_millis(50)) {
            Ok(Ok(event)) => {
                let now = Instant::now();
                for path in event.paths {
                    pending.insert(path, now);
                }
            }
            Ok(Err(e)) => eprintln!("sylva: watch error: {}", e),
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => break,
        }

        // Flush paths quiet for at least `debounce`.
        let now = Instant::now();
        let ready: Vec<PathBuf> = pending
            .iter()
            .filter(|(_, seen)| now.duration_since(**seen) >= debounce)
            .map(|(path, _)| path.clone())
            .collect();
        for path in ready {
            pending.remove(&path);
            process_path(&db_path, &path);
        }
    }
    // `watcher` drops here, unregistering the OS watch.
}

/// Handle to a running watcher. Stops the background thread on `.stop()` and,
/// as a safety net, on drop/garbage-collection.
#[pyclass]
pub struct WatcherHandle {
    stop_tx: Option<mpsc::Sender<()>>,
    thread: Option<JoinHandle<()>>,
}

#[pymethods]
impl WatcherHandle {
    /// Signal the watcher to stop and wait for its thread to finish. Idempotent.
    fn stop(&mut self, py: Python<'_>) {
        if let Some(tx) = self.stop_tx.take() {
            let _ = tx.send(());
        }
        if let Some(handle) = self.thread.take() {
            // Release the GIL so the watcher thread can complete any in-flight
            // reindex (which needs the GIL) and then exit, avoiding deadlock.
            py.allow_threads(move || {
                let _ = handle.join();
            });
        }
    }
}

impl Drop for WatcherHandle {
    fn drop(&mut self) {
        // Best-effort stop on GC: signal and detach (do not join) so we never
        // risk blocking on the GIL during finalization.
        if let Some(tx) = self.stop_tx.take() {
            let _ = tx.send(());
        }
    }
}

/// Start watching `root` for changes, reindexing modified `.py` files into the
/// database at `db_path`. Non-blocking: returns immediately with a handle.
/// `debounce_ms` coalesces rapid successive events per file (default 200ms).
///
/// Raises `FileNotFoundError` if `root` does not exist.
#[pyfunction]
#[pyo3(signature = (root, db_path, debounce_ms=200))]
pub fn start_watcher(root: &str, db_path: &str, debounce_ms: u64) -> PyResult<WatcherHandle> {
    let root_path = Path::new(root);
    if !root_path.exists() {
        return Err(PyFileNotFoundError::new_err(format!(
            "watch root does not exist: '{}'",
            root
        )));
    }
    if !root_path.is_dir() {
        return Err(PyOSError::new_err(format!(
            "watch root is not a directory: '{}'",
            root
        )));
    }

    let db_path = db_path.to_string();
    let debounce = Duration::from_millis(debounce_ms);

    // Register the OS watch SYNCHRONOUSLY, before returning, so the caller is
    // guaranteed that watching is active — no startup race where a file created
    // immediately after this call is missed. Events from here on queue into the
    // channel for the background loop to drain.
    let (event_tx, event_rx) = mpsc::channel();
    let mut watcher = notify::recommended_watcher(move |res| {
        let _ = event_tx.send(res);
    })
    .map_err(|e| PyRuntimeError::new_err(format!("failed to create file watcher: {}", e)))?;
    watcher
        .watch(root_path, RecursiveMode::Recursive)
        .map_err(|e| PyOSError::new_err(format!("failed to watch '{}': {}", root, e)))?;

    let (stop_tx, stop_rx) = mpsc::channel::<()>();
    let thread =
        thread::spawn(move || run_watch_loop(watcher, event_rx, db_path, debounce, stop_rx));

    Ok(WatcherHandle { stop_tx: Some(stop_tx), thread: Some(thread) })
}
