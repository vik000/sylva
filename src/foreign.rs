//! Feature 5.0 — black-box foreign-module export surface (Rust / PyO3).
//!
//! Non-Python "accelerator" modules are not fully parsed. Instead a lightweight
//! export-surface extractor recognises only the **binding declarations** — the
//! ground truth of what is callable across the language boundary — and emits
//! opaque nodes:
//!   - a `foreign_module` node for a `#[pymodule] fn NAME`,
//!   - a `foreign_export` node for each `#[pyfunction] fn NAME`.
//!
//! The dicts returned match the extractor/`write_symbols` shape (name, kind,
//! line, line_end, docstring, import_module, import_name), so a foreign file is
//! indexed with the same writer as Python. Cross-boundary resolution then falls
//! out of the normal name-based call resolution: a Python call to an exported
//! name resolves to its `foreign_export` node (see `edges::build_edges`).
//!
//! Internals are never parsed — opaque by design. Only PyO3 is recognised in
//! this first pass (C `PyMethodDef` / FFI are future extensions).

use pyo3::exceptions::{PyFileNotFoundError, PyOSError, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use tree_sitter::{Node, Parser};

fn node_text(node: Node, src: &[u8]) -> String {
    node.utf8_text(src).unwrap_or("").to_string()
}

/// Gather the attribute text attached to `func` — both attribute children of
/// the item and any preceding `attribute_item` siblings — so `#[pyfunction]` /
/// `#[pymodule]` are detected regardless of how the grammar nests them.
fn attrs_of(func: Node, src: &[u8]) -> String {
    let mut s = String::new();
    let mut c = func.walk();
    for ch in func.children(&mut c) {
        if ch.kind() == "attribute_item" {
            s.push_str(&node_text(ch, src));
            s.push(' ');
        }
    }
    let mut prev = func.prev_sibling();
    while let Some(p) = prev {
        match p.kind() {
            "attribute_item" => {
                s.push_str(&node_text(p, src));
                s.push(' ');
            }
            "line_comment" | "block_comment" => {}
            _ => break,
        }
        prev = p.prev_sibling();
    }
    s
}

/// Walk collecting PyO3 export declarations.
fn scan(
    node: Node,
    src: &[u8],
    exports: &mut Vec<(String, i64)>,
    module: &mut Option<(String, i64)>,
) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "function_item" {
            if let Some(name) = child.child_by_field_name("name") {
                let attrs = attrs_of(child, src);
                let line = child.start_position().row as i64 + 1;
                if attrs.contains("pyfunction") {
                    exports.push((node_text(name, src), line));
                }
                if attrs.contains("pymodule") {
                    *module = Some((node_text(name, src), line));
                }
            }
        }
        scan(child, src, exports, module);
    }
}

/// Extract the PyO3 export surface of a Rust file as symbol dicts.
///
/// Returns a list matching the `write_symbols` shape: a `foreign_module` dict
/// (if the file declares a `#[pymodule]`) followed by a `foreign_export` dict
/// per `#[pyfunction]`. A file with no recognisable exports yields `[]` (never
/// an error). Raises `FileNotFoundError` for a missing file.
#[pyfunction]
pub fn extract_foreign_exports(py: Python<'_>, path: &str) -> PyResult<Py<PyList>> {
    let bytes = std::fs::read(path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            PyFileNotFoundError::new_err(format!("file not found: '{}'", path))
        } else {
            PyOSError::new_err(format!("cannot read file '{}': {}", path, e))
        }
    })?;
    // Non-UTF-8 (binary) foreign file: nothing to extract, not an error.
    let source = match String::from_utf8(bytes) {
        Ok(s) => s,
        Err(_) => return Ok(PyList::empty(py).unbind()),
    };
    let src = source.as_bytes();

    let mut parser = Parser::new();
    parser
        .set_language(&tree_sitter_rust::LANGUAGE.into())
        .map_err(|e| PyRuntimeError::new_err(format!("failed to load Rust grammar: {}", e)))?;
    let tree = match parser.parse(src, None) {
        Some(t) => t,
        None => return Ok(PyList::empty(py).unbind()),
    };

    let mut exports: Vec<(String, i64)> = Vec::new();
    let mut module: Option<(String, i64)> = None;
    scan(tree.root_node(), src, &mut exports, &mut module);

    let list = PyList::empty(py);
    let emit = |name: String, kind: &str, line: i64| -> PyResult<()> {
        let d = PyDict::new(py);
        d.set_item("name", name)?;
        d.set_item("kind", kind)?;
        d.set_item("line", line)?;
        d.set_item("line_end", line)?;
        d.set_item("docstring", Option::<String>::None)?;
        d.set_item("import_module", Option::<String>::None)?;
        d.set_item("import_name", Option::<String>::None)?;
        list.append(d)?;
        Ok(())
    };
    if let Some((name, line)) = module {
        emit(name, "foreign_module", line)?;
    }
    for (name, line) in exports {
        emit(name, "foreign_export", line)?;
    }
    Ok(list.unbind())
}
