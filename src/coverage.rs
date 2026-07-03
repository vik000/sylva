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

use pyo3::exceptions::{PyFileNotFoundError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use quick_xml::events::{BytesStart, Event};
use quick_xml::reader::Reader;
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
