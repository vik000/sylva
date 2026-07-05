//! AST symbol extraction, dispatched by language (Feature 5.1).
//!
//! Each language is an [`Extractor`] — a small module that turns source into a
//! common `Vec<Symbol>`. `extract_symbols(path)` picks the extractor by file
//! extension and emits the shared dict shape `{name, kind, line, line_end,
//! docstring, import_module, import_name}`; `list_languages()` reports what's
//! registered. Adding a language is implementing one `Extractor` (Feature 5.2 /
//! 5.3). Python is the first (and, for now, only full) extractor.
//!
//! Robustness (per project rules): a missing file is rejected with
//! `FileNotFoundError`; an **unsupported extension** returns `[]` (not an error);
//! a non-UTF-8 (binary) file of a supported language is rejected with a clear
//! `ValueError`. A file that *parses with errors* is not fatal: recovered
//! symbols are returned, followed by a single `{"kind": "error", ...}` sentinel.
//! An extractor **panic is caught** and reported as that sentinel — never a crash.

use pyo3::exceptions::{PyFileNotFoundError, PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::HashSet;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::Path;
use tree_sitter::{Node, Parser};

/// One extracted symbol. `kind` is a fixed vocabulary so downstream code
/// (Feature 1.5) can rely on it.
struct Symbol {
    name: String,
    kind: &'static str,
    line: usize,
    line_end: usize,
    docstring: Option<String>,
    // For `import` symbols only: the original imported name and source module,
    // so aliased imports resolve to the real definition (Feature 7.8 / #37).
    import_module: Option<String>,
    import_name: Option<String>,
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
    // Source module for `from <module> import ...` (None for plain `import x`).
    let module = node.child_by_field_name("module_name").map(|m| node_text(m, src));

    let mut cursor = node.walk();
    for child in node.children_by_field_name("name", &mut cursor) {
        // `bound` is the name visible in the module (the alias, if any);
        // `original` is the name as it exists at the source, used for resolution.
        let (bound, original) = match child.kind() {
            "aliased_import" => {
                let orig = child.child_by_field_name("name").map(|n| node_text(n, src));
                let alias = child.child_by_field_name("alias").map(|n| node_text(n, src));
                (alias.or_else(|| orig.clone()), orig)
            }
            _ => {
                let n = node_text(child, src);
                (Some(n.clone()), Some(n))
            }
        };
        if let Some(bound) = bound {
            out.push(Symbol {
                name: bound,
                kind: "import",
                line,
                line_end,
                docstring: None,
                import_module: module.clone(),
                import_name: original,
            });
        }
    }

    let mut wc = node.walk();
    for child in node.children(&mut wc) {
        if child.kind() == "wildcard_import" {
            out.push(Symbol {
                name: "*".to_string(),
                kind: "import",
                line,
                line_end,
                docstring: None,
                import_module: module.clone(),
                import_name: Some("*".to_string()),
            });
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
                        import_module: None,
                        import_name: None,
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
                        import_module: None,
                        import_name: None,
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

/// The result of extracting one file: the recovered symbols plus the earliest
/// line at which the parse was incomplete (→ a trailing error sentinel).
pub(crate) struct ExtractResult {
    symbols: Vec<Symbol>,
    error_line: Option<usize>,
}

/// A language extractor: recognised extensions + source → symbols. Adding a
/// language is implementing this trait and registering it (see `registry`).
pub(crate) trait Extractor {
    /// The language name reported by `list_languages`.
    fn language(&self) -> &'static str;
    /// File extensions (without the dot) this extractor handles.
    fn extensions(&self) -> &'static [&'static str];
    /// Parse `source` into symbols. Must not raise — parse failures are
    /// reported via `error_line`, and any panic is caught by the caller.
    fn extract(&self, source: &str) -> ExtractResult;
}

/// The Python extractor (tree-sitter-python).
struct PythonExtractor;

impl Extractor for PythonExtractor {
    fn language(&self) -> &'static str {
        "python"
    }
    fn extensions(&self) -> &'static [&'static str] {
        &["py", "pyi"]
    }
    fn extract(&self, source: &str) -> ExtractResult {
        let src = source.as_bytes();
        let mut parser = Parser::new();
        if parser.set_language(&tree_sitter_python::LANGUAGE.into()).is_err() {
            return ExtractResult { symbols: Vec::new(), error_line: Some(1) };
        }
        let tree = match parser.parse(src, None) {
            Some(t) => t,
            None => return ExtractResult { symbols: Vec::new(), error_line: Some(1) },
        };
        let mut symbols = Vec::new();
        let mut err_line = None;
        collect(tree.root_node(), src, &mut symbols, &mut err_line);
        ExtractResult { symbols, error_line: err_line }
    }
}

/// The registered extractors. A fixed table — registration is inherently
/// idempotent (a language appears once).
fn registry() -> Vec<Box<dyn Extractor>> {
    vec![Box::new(PythonExtractor)]
}

/// The extractor handling `ext` (extension without the dot), if any.
fn extractor_for_ext(ext: &str) -> Option<Box<dyn Extractor>> {
    registry().into_iter().find(|e| e.extensions().contains(&ext))
}

/// The languages Sylva can fully extract, e.g. `["python"]`.
#[pyfunction]
pub fn list_languages(py: Python<'_>) -> PyResult<Py<PyList>> {
    let list = PyList::empty(py);
    let mut seen = HashSet::new();
    for e in registry() {
        if seen.insert(e.language()) {
            // dedup: registering a language twice is idempotent
            list.append(e.language())?;
        }
    }
    Ok(list.unbind())
}

/// Parse `path` and return a list of symbol dicts, each with keys `name`, `kind`
/// (`function` | `class` | `import`), `line` (1-based start), `line_end`
/// (1-based end), `docstring`, `import_module`, and `import_name`. An
/// unsupported extension returns `[]`; an incomplete parse (or a caught
/// extractor panic) appends a trailing `{"kind": "error", ...}` sentinel.
#[pyfunction]
pub fn extract_symbols(py: Python<'_>, path: &str) -> PyResult<Py<PyList>> {
    // A missing file is a real error regardless of language.
    let bytes = std::fs::read(path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            PyFileNotFoundError::new_err(format!("file not found: '{}'", path))
        } else {
            PyOSError::new_err(format!("cannot read file '{}': {}", path, e))
        }
    })?;

    // Dispatch by extension; an unsupported language yields no symbols.
    let ext = Path::new(path).extension().and_then(|e| e.to_str()).unwrap_or("");
    let extractor = match extractor_for_ext(ext) {
        Some(e) => e,
        None => return Ok(PyList::empty(py).unbind()),
    };

    let source = String::from_utf8(bytes).map_err(|_| {
        PyValueError::new_err(format!("file is not valid UTF-8 (binary file?): '{}'", path))
    })?;

    // An extractor bug must never crash the process — catch panics and report
    // them as an error sentinel (defensive for third-party language modules).
    let (symbols, err_line) = match catch_unwind(AssertUnwindSafe(|| extractor.extract(&source))) {
        Ok(r) => (r.symbols, r.error_line),
        Err(_) => (Vec::new(), Some(1usize)),
    };

    let list = PyList::empty(py);
    for s in symbols {
        let d = PyDict::new(py);
        d.set_item("name", s.name)?;
        d.set_item("kind", s.kind)?;
        d.set_item("line", s.line)?;
        d.set_item("line_end", s.line_end)?;
        d.set_item("docstring", s.docstring)?;
        d.set_item("import_module", s.import_module)?;
        d.set_item("import_name", s.import_name)?;
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
