//! Test-to-symbol mapping.
//!
//! Given per-test coverage trace data — `{test_name: {file: [lines]}}`, the
//! shape coverage.py's `--contexts` produces — build `test_covers` edges from
//! each test function (a symbol in the graph) to the non-test symbols its lines
//! exercised.
//!
//! Resolution:
//! - the edge `src` is the symbol whose name equals the trace's test name
//!   (ambiguous / unindexed test names are logged and skipped);
//! - the edge `dst` is, for each covered line, the *innermost* symbol whose
//!   `line_start..line_end` span (Feature 7.1) contains that line, in the graph
//!   file reconciled to the trace file by path suffix (Feature 3.2);
//! - self-edges and `test_`-prefixed destinations are excluded ("non-test").
//!
//! Idempotency: edges are inserted with `INSERT OR IGNORE` against the unique
//! index `(src_id, dst_id, kind)` (migration v2), so re-running never
//! duplicates. The return value counts only newly-inserted edges.
//!
//! Note (issue #35): trace keys are treated as plain test-function symbol names
//! (`test_greet`), not raw pytest node-ids (`tests/test_foo.py::test_greet`).
//! Callers must reduce context labels to symbol names until that adapter lands.

use crate::coverage::{components, suffix_match};
use pyo3::exceptions::{PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use rusqlite::{params, Connection};
use std::collections::{HashMap, HashSet};

type Trace = HashMap<String, HashMap<String, Vec<i64>>>;

/// A symbol as needed for span/name resolution.
type Sym = (i64, String, Option<i64>, Option<i64>);

fn extract_trace(trace: &Bound<'_, PyDict>) -> PyResult<Trace> {
    let mut out: Trace = HashMap::new();
    for (test_key, files_val) in trace.iter() {
        let test: String = test_key
            .extract()
            .map_err(|_| PyValueError::new_err("trace keys must be test-name strings"))?;
        let files = files_val.downcast::<PyDict>().map_err(|_| {
            PyValueError::new_err(format!("trace value for '{}' must be a dict", test))
        })?;
        let mut per_file = HashMap::new();
        for (file_key, lines_val) in files.iter() {
            let file: String = file_key
                .extract()
                .map_err(|_| PyValueError::new_err("trace file keys must be strings"))?;
            let lines: Vec<i64> = lines_val.extract().map_err(|_| {
                PyValueError::new_err(format!("trace lines for '{}' must be a list of ints", file))
            })?;
            per_file.insert(file, lines);
        }
        out.insert(test, per_file);
    }
    Ok(out)
}

/// Reconcile a trace file path to a single graph file id (exact match wins,
/// else a unique suffix match). None (logged) if unmatched or ambiguous.
fn resolve_file(report_path: &str, files: &[(i64, String)]) -> Option<i64> {
    let rc = components(report_path);
    let matches: Vec<&(i64, String)> = files
        .iter()
        .filter(|(_, graph)| suffix_match(&components(graph), &rc))
        .collect();

    match matches.len() {
        0 => {
            eprintln!("sylva: test-trace file '{}' matches no indexed file; skipping", report_path);
            None
        }
        1 => Some(matches[0].0),
        _ => {
            let exact: Vec<_> = matches.iter().filter(|(_, g)| g == report_path).collect();
            if exact.len() == 1 {
                Some(exact[0].0)
            } else {
                eprintln!(
                    "sylva: test-trace file '{}' ambiguously matches {} files; skipping",
                    report_path,
                    matches.len()
                );
                None
            }
        }
    }
}

/// The innermost symbol whose span contains `line` (narrowest span wins, so a
/// line inside a method attaches to the method, not the enclosing class).
fn symbol_at(syms: &[Sym], line: i64) -> Option<&Sym> {
    syms.iter()
        .filter(|(_, _, ls, le)| match ls {
            Some(s) => *s <= line && line <= le.unwrap_or(*s),
            None => false,
        })
        .min_by_key(|(_, _, ls, le)| {
            let start = ls.expect("filtered to Some");
            le.unwrap_or(start) - start
        })
}

/// Build `test_covers` edges from test functions to the symbols they exercised.
/// Returns the number of new edges written (duplicates are ignored, not
/// counted).
#[pyfunction]
pub fn map_tests_to_symbols(db_path: &str, trace: &Bound<'_, PyDict>) -> PyResult<usize> {
    let trace = extract_trace(trace)?;

    let mut conn = Connection::open(db_path)
        .map_err(|e| PyOSError::new_err(format!("cannot open database '{}': {}", db_path, e)))?;

    let files: Vec<(i64, String)> = {
        let mut stmt = conn
            .prepare("SELECT id, path FROM files")
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read files: {}", e)))?;
        let rows = stmt
            .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read files: {}", e)))?;
        rows.collect::<Result<_, _>>()
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read files: {}", e)))?
    };

    // Symbols grouped by file (for span lookup) and by name (for test src).
    let mut by_file: HashMap<i64, Vec<Sym>> = HashMap::new();
    let mut by_name: HashMap<String, Vec<i64>> = HashMap::new();
    {
        let mut stmt = conn
            .prepare("SELECT id, name, file_id, line_start, line_end FROM symbols")
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        let rows = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, i64>(2)?,
                    r.get::<_, Option<i64>>(3)?,
                    r.get::<_, Option<i64>>(4)?,
                ))
            })
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        for row in rows {
            let (id, name, file_id, ls, le) =
                row.map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
            by_name.entry(name.clone()).or_default().push(id);
            by_file.entry(file_id).or_default().push((id, name, ls, le));
        }
    }

    let tx = conn
        .transaction()
        .map_err(|e| PyRuntimeError::new_err(format!("failed to begin transaction: {}", e)))?;

    let mut written = 0usize;
    {
        let mut insert = tx
            .prepare(
                "INSERT OR IGNORE INTO edges (src_id, dst_id, kind) \
                 VALUES (?1, ?2, 'test_covers')",
            )
            .map_err(|e| PyRuntimeError::new_err(format!("failed to prepare insert: {}", e)))?;

        for (test_name, files_map) in &trace {
            // Resolve the test's own symbol (edge src).
            let src_id = match by_name.get(test_name).map(|v| v.as_slice()) {
                Some([single]) => *single,
                Some(many) if many.len() > 1 => {
                    eprintln!(
                        "sylva: test '{}' matches {} symbols; skipping (ambiguous)",
                        test_name,
                        many.len()
                    );
                    continue;
                }
                _ => {
                    eprintln!("sylva: test '{}' is not indexed as a symbol; skipping", test_name);
                    continue;
                }
            };

            // Collect the distinct non-test symbols the test's lines hit.
            let mut dsts: HashSet<i64> = HashSet::new();
            for (file, lines) in files_map {
                let file_id = match resolve_file(file, &files) {
                    Some(id) => id,
                    None => continue,
                };
                if let Some(syms) = by_file.get(&file_id) {
                    for &line in lines {
                        if let Some((dst_id, dst_name, _, _)) = symbol_at(syms, line) {
                            if *dst_id == src_id || dst_name.starts_with("test_") {
                                continue; // no self-edge; destinations are non-test
                            }
                            dsts.insert(*dst_id);
                        }
                    }
                }
            }

            for dst in dsts {
                written += insert
                    .execute(params![src_id, dst])
                    .map_err(|e| PyRuntimeError::new_err(format!("failed to write edge: {}", e)))?;
            }
        }
    }

    tx.commit()
        .map_err(|e| PyRuntimeError::new_err(format!("failed to commit edges: {}", e)))?;

    Ok(written)
}
