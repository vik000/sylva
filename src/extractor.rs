//! Python AST extractor.
//!
//! Parses a `.py` file with tree-sitter and extracts its symbols — functions,
//! classes, and imports — each as a dict `{name, kind, line, line_end, docstring}`.
//!
//! Robustness (per project rules): a missing file is rejected with
//! `FileNotFoundError`; a non-UTF-8 (binary) file is rejected with a clear
//! `ValueError`. A file that *parses with errors* is not fatal: whatever
//! symbols tree-sitter recovers are returned, followed by a single sentinel
//! entry `{"kind": "error", ...}` flagging that the parse was incomplete. This
//! is the "partial results with an error flag" contract from the spec.

use pyo3::exceptions::{PyFileNotFoundError, PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use tree_sitter::{Node, Parser};

/// One extracted symbol. `kind` is a fixed vocabulary so downstream code
/// (Feature 1.5) can rely on it.
struct Symbol {
    name: String,
    kind: &'static str,
    line: usize,
    line_end: usize,
    docstring: Option<String>,
}

fn node_text(node: Node, src: &[u8]) -> String {
    node.utf8_text(src).unwrap_or("").to_string()
}

/// Extract a function/class docstring: the first statement of its body, if that
/// statement is a bare string literal.
fn get_docstring(def_node: Node, src: &[u8]) -> Option<String> {
    let body = def_node.child_by_field_name("body")?;
    let mut cursor = body.walk();
    let first = body.named_children(&mut cursor).next()?;
    if first.kind() != "expression_statement" {
        return None;
    }
    let mut inner = first.walk();
    let expr = first.named_children(&mut inner).next()?;
    if expr.kind() != "string" {
        return None;
    }
    Some(clean_string(expr, src))
}

/// Return the textual content of a `string` node, preferring its
/// `string_content` child (quotes/prefixes stripped) and falling back to
/// trimming surrounding quotes manually.
fn clean_string(node: Node, src: &[u8]) -> String {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "string_content" {
            return node_text(child, src);
        }
    }
    strip_quotes(&node_text(node, src))
}

fn strip_quotes(s: &str) -> String {
    let t = s.trim();
    for q in ["\"\"\"", "'''", "\"", "'"] {
        if t.len() >= 2 * q.len() && t.starts_with(q) && t.ends_with(q) {
            return t[q.len()..t.len() - q.len()].to_string();
        }
    }
    t.to_string()
}

/// Collect the imported binding names from an `import_statement` or
/// `import_from_statement`. Both grammars expose the imported items under the
/// `name` field; `from x import *` is captured as a `wildcard_import` child.
fn collect_imports(node: Node, src: &[u8], out: &mut Vec<Symbol>) {
    let line = node.start_position().row + 1;
    // An import statement usually occupies one line, but a parenthesised
    // `from x import (a, b)` can span several; use the statement's own extent.
    let line_end = node.end_position().row + 1;

    let mut cursor = node.walk();
    for child in node.children_by_field_name("name", &mut cursor) {
        let name = match child.kind() {
            // `import numpy as np` / `from x import y as z` — prefer the bound
            // alias, since that is the name visible in the module.
            "aliased_import" => child
                .child_by_field_name("alias")
                .or_else(|| child.child_by_field_name("name"))
                .map(|n| node_text(n, src)),
            _ => Some(node_text(child, src)),
        };
        if let Some(name) = name {
            out.push(Symbol { name, kind: "import", line, line_end, docstring: None });
        }
    }

    let mut wc = node.walk();
    for child in node.children(&mut wc) {
        if child.kind() == "wildcard_import" {
            out.push(Symbol { name: "*".to_string(), kind: "import", line, line_end, docstring: None });
        }
    }
}

/// Depth-first walk collecting definitions and imports at any nesting level.
/// `err_line` records the earliest line at which tree-sitter flagged an ERROR
/// or MISSING node, so the caller can emit a single error sentinel.
fn collect(node: Node, src: &[u8], out: &mut Vec<Symbol>, err_line: &mut Option<usize>) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.is_error() || child.is_missing() {
            let line = child.start_position().row + 1;
            *err_line = Some(err_line.map_or(line, |e| e.min(line)));
        }

        match child.kind() {
            "function_definition" => {
                if let Some(name) = child.child_by_field_name("name") {
                    out.push(Symbol {
                        name: node_text(name, src),
                        kind: "function",
                        line: child.start_position().row + 1,
                        line_end: child.end_position().row + 1,
                        docstring: get_docstring(child, src),
                    });
                }
            }
            "class_definition" => {
                if let Some(name) = child.child_by_field_name("name") {
                    out.push(Symbol {
                        name: node_text(name, src),
                        kind: "class",
                        line: child.start_position().row + 1,
                        line_end: child.end_position().row + 1,
                        docstring: get_docstring(child, src),
                    });
                }
            }
            "import_statement" | "import_from_statement" => {
                collect_imports(child, src, out);
            }
            _ => {}
        }

        // Recurse so nested classes, methods, and inner functions are found.
        collect(child, src, out, err_line);
    }
}

/// Parse `path` and return a list of symbol dicts, each with keys
/// `name`, `kind` (`function` | `class` | `import`), `line` (1-based start),
/// `line_end` (1-based end), and `docstring` (str or None). If the parse is
/// incomplete, a trailing `{"kind": "error", ...}` dict is appended after the
/// partial results.
#[pyfunction]
pub fn extract_symbols(py: Python<'_>, path: &str) -> PyResult<Py<PyList>> {
    let bytes = std::fs::read(path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            PyFileNotFoundError::new_err(format!("file not found: '{}'", path))
        } else {
            PyOSError::new_err(format!("cannot read file '{}': {}", path, e))
        }
    })?;

    let source = String::from_utf8(bytes).map_err(|_| {
        PyValueError::new_err(format!(
            "file is not valid UTF-8 (binary file?): '{}'",
            path
        ))
    })?;
    let src = source.as_bytes();

    let mut parser = Parser::new();
    parser
        .set_language(&tree_sitter_python::LANGUAGE.into())
        .map_err(|e| PyRuntimeError::new_err(format!("failed to load Python grammar: {}", e)))?;
    let tree = parser
        .parse(src, None)
        .ok_or_else(|| PyRuntimeError::new_err(format!("failed to parse '{}'", path)))?;

    let mut symbols: Vec<Symbol> = Vec::new();
    let mut err_line: Option<usize> = None;
    collect(tree.root_node(), src, &mut symbols, &mut err_line);

    let list = PyList::empty(py);
    for s in symbols {
        let d = PyDict::new(py);
        d.set_item("name", s.name)?;
        d.set_item("kind", s.kind)?;
        d.set_item("line", s.line)?;
        d.set_item("line_end", s.line_end)?;
        d.set_item("docstring", s.docstring)?;
        list.append(d)?;
    }

    if let Some(line) = err_line {
        let d = PyDict::new(py);
        d.set_item("name", "parse_error")?;
        d.set_item("kind", "error")?;
        d.set_item("line", line)?;
        d.set_item("line_end", line)?;
        d.set_item("docstring", Option::<String>::None)?;
        list.append(d)?;
    }

    Ok(list.unbind())
}
