//! Coverage report parsing.
//!
//! Parses the two line-level coverage formats that coverage.py (and the
//! equivalent Rust/JS tools, later) can emit — LCOV and Cobertura XML — into a
//! uniform `file -> { line -> covered? }` map. Pure and deterministic: no DB,
//! no filesystem beyond reading the report itself.
//!
//! Robustness (per project rules):
//! - an unknown `format` is *explicitly rejected* with `ValueError`;
//! - a missing report is `FileNotFoundError`; unreadable / non-UTF-8 / invalid
//!   XML is a clear `ValueError`;
//! - individual malformed records (a bad `DA:` line, a `<class>` with no
//!   filename, a `DA` outside any file section) are *explicitly ignored* —
//!   logged and skipped — so one bad line never discards a whole report.

use pyo3::exceptions::{PyFileNotFoundError, PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use quick_xml::events::{BytesStart, Event};
use quick_xml::reader::Reader;
use rusqlite::{params, Connection};
use std::collections::HashMap;

type Coverage = HashMap<String, HashMap<i64, bool>>;

/// Record a line's coverage, OR-ing with any prior observation (covered if hit
/// by any test run).
fn record(map: &mut HashMap<i64, bool>, line: i64, covered: bool) {
    map.entry(line).and_modify(|c| *c |= covered).or_insert(covered);
}

/// Parse LCOV text: `SF:<file>` opens a section, `DA:<line>,<hits>` records a
/// line, `end_of_record` closes it. All other record types are ignored.
fn parse_lcov(content: &str) -> Coverage {
    let mut result: Coverage = HashMap::new();
    let mut current: Option<String> = None;

    for raw in content.lines() {
        let line = raw.trim();

        if let Some(file) = line.strip_prefix("SF:") {
            let file = file.trim();
            if file.is_empty() {
                eprintln!("sylva: skipping LCOV section with empty SF path");
                current = None;
            } else {
                result.entry(file.to_string()).or_default();
                current = Some(file.to_string());
            }
        } else if let Some(rest) = line.strip_prefix("DA:") {
            let mut parts = rest.split(',');
            let lineno = parts.next().and_then(|s| s.trim().parse::<i64>().ok());
            let hits = parts.next().and_then(|s| s.trim().parse::<i64>().ok());
            match (lineno, hits, current.as_ref()) {
                (Some(lineno), Some(hits), Some(file)) => {
                    let entry = result.get_mut(file).expect("section exists");
                    record(entry, lineno, hits > 0);
                }
                (_, _, None) => {
                    eprintln!("sylva: skipping DA record outside any SF section: '{}'", line)
                }
                _ => eprintln!("sylva: skipping malformed DA record: '{}'", line),
            }
        } else if line == "end_of_record" {
            current = None;
        }
        // Every other record type (TN, FN, FNDA, BRDA, LF, LH, ...) is ignored.
    }

    result
}

/// Read an attribute value off an XML element, if present and well-formed.
fn attr(e: &BytesStart, key: &[u8]) -> Option<String> {
    e.attributes()
        .flatten()
        .find(|a| a.key.as_ref() == key)
        .and_then(|a| a.unescape_value().ok().map(|v| v.into_owned()))
}

/// Parse Cobertura XML: `<class filename="...">` opens a file, each
/// `<line number="N" hits="H"/>` records a line.
fn parse_cobertura(content: &str) -> PyResult<Coverage> {
    let mut reader = Reader::from_str(content);
    let mut result: Coverage = HashMap::new();
    let mut current: Option<String> = None;

    loop {
        match reader.read_event() {
            Ok(Event::Eof) => break,
            Ok(Event::Start(e)) | Ok(Event::Empty(e)) => match e.name().as_ref() {
                b"class" => match attr(&e, b"filename") {
                    Some(file) if !file.is_empty() => {
                        result.entry(file.clone()).or_default();
                        current = Some(file);
                    }
                    _ => {
                        eprintln!("sylva: skipping <class> element without a filename");
                        current = None;
                    }
                },
                b"line" => {
                    if let Some(file) = current.as_ref() {
                        let number = attr(&e, b"number").and_then(|s| s.parse::<i64>().ok());
                        let hits = attr(&e, b"hits").and_then(|s| s.parse::<i64>().ok());
                        match (number, hits) {
                            (Some(number), Some(hits)) => {
                                let entry = result.get_mut(file).expect("class section exists");
                                record(entry, number, hits > 0);
                            }
                            _ => eprintln!("sylva: skipping malformed <line> element"),
                        }
                    }
                }
                _ => {}
            },
            Ok(Event::End(e)) if e.name().as_ref() == b"class" => current = None,
            Ok(_) => {}
            Err(err) => {
                return Err(PyValueError::new_err(format!(
                    "corrupt Cobertura XML: {}",
                    err
                )))
            }
        }
    }

    Ok(result)
}

/// Read the report file, mapping missing/unreadable to clear errors.
fn read_report(path: &str) -> PyResult<String> {
    match std::fs::read_to_string(path) {
        Ok(s) => Ok(s),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            Err(PyFileNotFoundError::new_err(format!("coverage report not found: '{}'", path)))
        }
        Err(e) => Err(PyValueError::new_err(format!(
            "cannot read coverage report '{}': {}",
            path, e
        ))),
    }
}

/// Parse a coverage report into `{ file: { line: covered } }`.
///
/// `format` must be `"lcov"` or `"cobertura"` (case-insensitive); anything else
/// raises `ValueError`.
#[pyfunction]
pub fn parse_coverage(py: Python<'_>, path: &str, format: &str) -> PyResult<Py<PyDict>> {
    // Validate the format first — cheap, and independent of the file existing.
    let fmt = format.to_lowercase();
    if fmt != "lcov" && fmt != "cobertura" {
        return Err(PyValueError::new_err(format!(
            "unknown coverage format '{}': expected 'lcov' or 'cobertura'",
            format
        )));
    }

    let content = read_report(path)?;
    let data = match fmt.as_str() {
        "lcov" => parse_lcov(&content),
        _ => parse_cobertura(&content)?,
    };

    let out = PyDict::new(py);
    for (file, lines) in data {
        let inner = PyDict::new(py);
        for (line, covered) in lines {
            inner.set_item(line, covered)?;
        }
        out.set_item(file, inner)?;
    }
    Ok(out.unbind())
}

// --------------------------------------------------------------------------- //
// Feature 3.2 — correlate coverage to symbols
// --------------------------------------------------------------------------- //

/// Split a path into its non-empty components, tolerating both separators and
/// dropping `.` segments, so absolute and relative paths compare cleanly.
pub(crate) fn components(path: &str) -> Vec<&str> {
    path.split(['/', '\\'])
        .filter(|c| !c.is_empty() && *c != ".")
        .collect()
}

/// True if one component list is a suffix of the other (component-boundary
/// aware, so `foo.py` never matches `barfoo.py`). This lets a report path like
/// `src/foo.py` reconcile with a graph path like `/repo/src/foo.py`.
pub(crate) fn suffix_match(a: &[&str], b: &[&str]) -> bool {
    if a.is_empty() || b.is_empty() {
        return false;
    }
    let (short, long) = if a.len() <= b.len() { (a, b) } else { (b, a) };
    long.ends_with(short)
}

/// Pull the Python `{file: {line: covered}}` dict into Rust.
fn extract_coverage(coverage: &Bound<'_, PyDict>) -> PyResult<Coverage> {
    let mut out: Coverage = HashMap::new();
    for (key, value) in coverage.iter() {
        let file: String = key.extract().map_err(|_| {
            PyValueError::new_err("coverage keys must be file-path strings")
        })?;
        let inner = value.downcast::<PyDict>().map_err(|_| {
            PyValueError::new_err(format!("coverage value for '{}' must be a dict", file))
        })?;
        let mut lines = HashMap::new();
        for (lk, lv) in inner.iter() {
            let line: i64 = lk.extract().map_err(|_| {
                PyValueError::new_err("coverage line numbers must be ints")
            })?;
            let covered: bool = lv.extract().map_err(|_| {
                PyValueError::new_err("coverage line values must be bools")
            })?;
            lines.insert(line, covered);
        }
        out.insert(file, lines);
    }
    Ok(out)
}

/// Reconcile each report file to a single graph file id, using exact match
/// first and falling back to a unique suffix match. Report paths that match no
/// graph file, or ambiguously match several, are logged and skipped.
fn reconcile<'a>(
    cov: &'a Coverage,
    files: &[(i64, String)],
) -> HashMap<i64, &'a HashMap<i64, bool>> {
    let mut mapping: HashMap<i64, &HashMap<i64, bool>> = HashMap::new();

    for (report_path, lines) in cov {
        let report_components = components(report_path);
        let matches: Vec<&(i64, String)> = files
            .iter()
            .filter(|(_, graph)| suffix_match(&components(graph), &report_components))
            .collect();

        let chosen = match matches.len() {
            0 => {
                eprintln!(
                    "sylva: coverage path '{}' matches no indexed file; skipping",
                    report_path
                );
                None
            }
            1 => Some(matches[0]),
            _ => {
                // Prefer an exact string match if there is exactly one.
                let exact: Vec<_> = matches.iter().filter(|(_, g)| g == report_path).collect();
                if exact.len() == 1 {
                    Some(*exact[0])
                } else {
                    eprintln!(
                        "sylva: coverage path '{}' ambiguously matches {} indexed files; skipping",
                        report_path,
                        matches.len()
                    );
                    None
                }
            }
        };

        if let Some((file_id, _)) = chosen {
            mapping.insert(*file_id, lines);
        }
    }

    mapping
}

/// Coverage percentage for a symbol spanning `[start, end]`, given a file's
/// line→covered map. `None` when no reported (executable) line falls in the
/// span — distinct from `Some(0.0)`, which means "has reported lines, none
/// covered".
fn symbol_pct(lines: &HashMap<i64, bool>, start: i64, end: i64) -> Option<f64> {
    let mut total = 0i64;
    let mut covered = 0i64;
    for (&line, &is_covered) in lines {
        if line >= start && line <= end {
            total += 1;
            if is_covered {
                covered += 1;
            }
        }
    }
    if total == 0 {
        None
    } else {
        Some(covered as f64 / total as f64 * 100.0)
    }
}

/// Correlate parsed coverage onto the graph's symbols, writing each symbol's
/// `coverage_pct`. Report files are matched to graph files by path suffix (see
/// `reconcile`); each symbol's covered/total is computed over its
/// `line_start..line_end` span (Feature 7.1). A symbol whose span has no
/// reported lines is set to NULL, not 0.
///
/// Returns the number of symbols assigned a numeric percentage (symbols set to
/// NULL, and files not present in the report, are not counted). Each symbol is
/// updated independently, so a failure on one is logged and skipped without
/// aborting the rest.
///
/// Known limitations (tracked in issue #34): if two report entries suffix-match
/// the *same* graph file, the last one wins nondeterministically; and coverage
/// is not reset for files absent from a later report (stale values persist).
#[pyfunction]
pub fn apply_coverage(db_path: &str, coverage: &Bound<'_, PyDict>) -> PyResult<usize> {
    let cov = extract_coverage(coverage)?;

    let conn = Connection::open(db_path)
        .map_err(|e| PyOSError::new_err(format!("cannot open database '{}': {}", db_path, e)))?;

    // Load the graph's file records once.
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

    let mapping = reconcile(&cov, &files);

    let mut updated = 0usize;
    for (file_id, lines) in &mapping {
        // Collect this file's symbols first (statement dropped before updates).
        let symbols: Vec<(i64, Option<i64>, Option<i64>)> = {
            let mut stmt = conn
                .prepare("SELECT id, line_start, line_end FROM symbols WHERE file_id = ?1")
                .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
            let rows = stmt
                .query_map(params![file_id], |r| {
                    Ok((r.get::<_, i64>(0)?, r.get::<_, Option<i64>>(1)?, r.get::<_, Option<i64>>(2)?))
                })
                .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
            rows.collect::<Result<_, _>>()
                .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?
        };

        for (sym_id, line_start, line_end) in symbols {
            // Without a start line we cannot correlate; leave it untouched.
            let start = match line_start {
                Some(s) => s,
                None => continue,
            };
            let end = line_end.unwrap_or(start);
            let pct = symbol_pct(lines, start, end);

            // Per-symbol update — a single failure is isolated and logged, the
            // batch continues.
            match conn.execute(
                "UPDATE symbols SET coverage_pct = ?1 WHERE id = ?2",
                params![pct, sym_id],
            ) {
                Ok(_) => {
                    if pct.is_some() {
                        updated += 1;
                    }
                }
                Err(e) => eprintln!(
                    "sylva: failed to set coverage for symbol id {}: {}",
                    sym_id, e
                ),
            }
        }
    }

    Ok(updated)
}
