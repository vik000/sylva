//! Edge population — build `calls` and `imports` relationships across the graph.
//!
//! Resolving a reference (`b()` in one file → the symbol `b` defined in another)
//! needs the *whole* graph, so this is a standalone pass run **after** all files
//! are indexed, not a per-file step. It:
//!   - clears existing `calls`/`imports` edges (leaving `test_covers` intact),
//!   - for every `import` symbol, links it to the unique definition of the same
//!     name (`imports` edge),
//!   - re-parses each indexed file, finds call sites, attributes each to the
//!     innermost enclosing symbol (the caller), and links it to the unique
//!     definition of the callee name (`calls` edge).
//!
//! Resolution is by name with ambiguous-skip: a unique matching definition wins;
//! several same-named definitions are skipped; unresolved names (builtins /
//! external / not indexed) are skipped. Skips are counted and reported as a
//! single bounded summary (issue #39) — set `SYLVA_LOG` for per-reference
//! detail. This never invents a false edge, and self-calls (recursion) produce
//! a self-edge. Edges use
//! `INSERT OR IGNORE` against the unique index (migration v2), so the pass is
//! idempotent. Returns the number of edges written.
//!
//! Precision limits of name-only resolution (tracked in issue #36): method
//! calls resolve by method name ignoring receiver type; there is no scope /
//! shadowing model; and genuinely-colliding names are skipped rather than
//! disambiguated. Type-aware resolution is future work.

use pyo3::exceptions::{PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rusqlite::{params, Connection};
use std::collections::{HashMap, HashSet};
use std::path::Path;
use tree_sitter::{Node, Parser};

struct SymRow {
    id: i64,
    name: String,
    kind: String,
    file_id: i64,
    line_start: Option<i64>,
    line_end: Option<i64>,
    import_module: Option<String>,
    import_name: Option<String>,
}

fn node_text(node: Node, src: &[u8]) -> String {
    node.utf8_text(src).unwrap_or("").to_string()
}

/// Outcome of resolving a referenced name to a definition. Kept silent here so
/// the caller can aggregate counts and emit one bounded summary (issue #39).
enum Resolution {
    Resolved(i64),
    Ambiguous,
    Unresolved,
}

/// Resolve a referenced name to a unique definition, as `(symbol_id, file_id)`
/// pairs. When the name is globally ambiguous, prefer a definition in
/// `caller_file` (issue #36): a call to `get()` inside a module that defines
/// `get` resolves to *that* `get`, matching Python's module-scope resolution.
/// This recovers local calls without ever inventing a false edge.
fn resolve_def(
    defs_by_name: &HashMap<String, Vec<(i64, i64)>>,
    name: &str,
    caller_file: Option<i64>,
) -> Resolution {
    match defs_by_name.get(name).map(Vec::as_slice) {
        Some([(only, _)]) => Resolution::Resolved(*only),
        Some(many) if many.len() > 1 => {
            if let Some(file) = caller_file {
                let same_file: Vec<i64> =
                    many.iter().filter(|(_, f)| *f == file).map(|(id, _)| *id).collect();
                if same_file.len() == 1 {
                    return Resolution::Resolved(same_file[0]);
                }
            }
            Resolution::Ambiguous
        }
        _ => Resolution::Unresolved,
    }
}

/// Does `file_path` correspond to the dotted `module`? e.g. `flask.app` matches
/// `.../flask/app.py`, and `flask` matches `.../flask/__init__.py`. Used to pick
/// the right definition among same-named candidates for an import.
fn file_matches_module(file_path: &str, module: &str) -> bool {
    let stripped = file_path.strip_suffix(".py").unwrap_or(file_path);
    let mut path_comps: Vec<&str> =
        stripped.split(['/', '\\']).filter(|c| !c.is_empty() && *c != ".").collect();
    if path_comps.last() == Some(&"__init__") {
        path_comps.pop();
    }
    let mod_comps: Vec<&str> = module.split('.').filter(|c| !c.is_empty()).collect();
    !mod_comps.is_empty() && path_comps.ends_with(&mod_comps)
}

/// Resolve an import's original name to a definition, preferring the one in the
/// named source module when several share the name (Feature 7.8 / #37).
fn resolve_import(
    defs_by_name: &HashMap<String, Vec<(i64, i64)>>,
    files_by_id: &HashMap<i64, String>,
    name: &str,
    module: Option<&str>,
) -> Resolution {
    match defs_by_name.get(name).map(Vec::as_slice) {
        Some([(only, _)]) => Resolution::Resolved(*only),
        Some(many) if many.len() > 1 => {
            if let Some(module) = module {
                let matches: Vec<i64> = many
                    .iter()
                    .filter(|(_, fid)| {
                        files_by_id.get(fid).map_or(false, |p| file_matches_module(p, module))
                    })
                    .map(|(id, _)| *id)
                    .collect();
                if matches.len() == 1 {
                    return Resolution::Resolved(matches[0]);
                }
            }
            Resolution::Ambiguous
        }
        _ => Resolution::Unresolved,
    }
}

/// Innermost symbol (narrowest span) among `candidates` whose span contains
/// `line`. Used to find the caller enclosing a call site.
fn innermost<'a>(syms: &'a [SymRow], candidates: &[usize], line: i64) -> Option<&'a SymRow> {
    candidates
        .iter()
        .map(|&i| &syms[i])
        .filter(|s| match s.line_start {
            Some(start) => start <= line && line <= s.line_end.unwrap_or(start),
            None => false,
        })
        .min_by_key(|s| {
            let start = s.line_start.expect("filtered to Some");
            s.line_end.unwrap_or(start) - start
        })
}

/// The receiver of a call, for type-aware resolution (Feature 7.10).
enum Receiver {
    /// A bare call `foo()` — no receiver.
    Bare,
    /// `self.foo()` / `cls.foo()` — resolves within the enclosing class.
    SelfCls,
    /// `obj.foo()` where `obj` is a plain identifier — resolves via `obj`'s type.
    Local(String),
    /// A complex receiver (`a.b.foo()`, `f().g()`, `x[0].foo()`) — not inferred.
    Other,
}

/// One call site: line, callee name (`b`/`method`), and its receiver.
struct CallSite {
    line: i64,
    callee: String,
    receiver: Receiver,
}

/// Collect every call site with its receiver. The callee is the function
/// identifier (`b()`) or the attribute name (`obj.method()`); the receiver
/// classifies `obj` so Feature 7.10 can type-resolve method calls.
fn collect_call_sites(node: Node, src: &[u8], out: &mut Vec<CallSite>) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "call" {
            if let Some(func) = child.child_by_field_name("function") {
                let (name, receiver) = match func.kind() {
                    "identifier" => (Some(node_text(func, src)), Receiver::Bare),
                    "attribute" => {
                        let attr =
                            func.child_by_field_name("attribute").map(|a| node_text(a, src));
                        let recv = match func.child_by_field_name("object") {
                            Some(obj) if obj.kind() == "identifier" => {
                                let t = node_text(obj, src);
                                if t == "self" || t == "cls" {
                                    Receiver::SelfCls
                                } else {
                                    Receiver::Local(t)
                                }
                            }
                            _ => Receiver::Other, // attribute / call / subscript object
                        };
                        (attr, recv)
                    }
                    _ => (None, Receiver::Other),
                };
                if let Some(name) = name {
                    out.push(CallSite {
                        line: child.start_position().row as i64 + 1,
                        callee: name,
                        receiver,
                    });
                }
            }
        }
        collect_call_sites(child, src, out); // recurse: calls nest in args/bodies
    }
}

/// Collect TS/JS call sites (Feature 5.6). A `call_expression` whose callee is a
/// bare `identifier` (`b()`) or a `member_expression` (`obj.m()`): the callee is
/// the method name; the receiver is `this` → SelfCls, a plain identifier →
/// Local, else Other. Same `CallSite` shape as Python, so the resolver is reused.
fn collect_call_sites_ts(node: Node, src: &[u8], out: &mut Vec<CallSite>) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "call_expression" {
            if let Some(func) = child.child_by_field_name("function") {
                let (name, receiver) = match func.kind() {
                    "identifier" => (Some(node_text(func, src)), Receiver::Bare),
                    "member_expression" => {
                        let prop =
                            func.child_by_field_name("property").map(|p| node_text(p, src));
                        let recv = match func.child_by_field_name("object") {
                            Some(obj) if obj.kind() == "this" => Receiver::SelfCls,
                            Some(obj) if obj.kind() == "identifier" => {
                                Receiver::Local(node_text(obj, src))
                            }
                            _ => Receiver::Other,
                        };
                        (prop, recv)
                    }
                    _ => (None, Receiver::Other),
                };
                if let Some(name) = name {
                    out.push(CallSite {
                        line: child.start_position().row as i64 + 1,
                        callee: name,
                        receiver,
                    });
                }
            }
        }
        collect_call_sites_ts(child, src, out);
    }
}

/// The call-graph language of a file (by extension): Python is fully resolved;
/// TS/JS get name-based call edges (Feature 5.6); everything else (`.rs` foreign,
/// unknown) contributes no call sites.
enum SrcLang {
    Python,
    TsJs,
    Skip,
}

fn src_lang(path: &str) -> SrcLang {
    let ext = std::path::Path::new(path).extension().and_then(|e| e.to_str()).unwrap_or("");
    match ext {
        "py" | "pyi" => SrcLang::Python,
        "ts" | "tsx" | "mts" | "cts" | "js" | "jsx" | "mjs" | "cjs" => SrcLang::TsJs,
        _ => SrcLang::Skip,
    }
}

/// Collect trivial `var = ClassName(...)` assignments as `(line, var, class_name)`
/// — the local-binding type hints used by Feature 7.10's `obj.method()`
/// resolution. Only a bare-identifier target with a bare-identifier constructor
/// call is recorded (nothing fancier is inferred).
fn collect_assignments(node: Node, src: &[u8], out: &mut Vec<(i64, String, String)>) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "assignment" {
            if let (Some(left), Some(right)) =
                (child.child_by_field_name("left"), child.child_by_field_name("right"))
            {
                if left.kind() == "identifier" && right.kind() == "call" {
                    if let Some(f) = right.child_by_field_name("function") {
                        if f.kind() == "identifier" {
                            out.push((
                                child.start_position().row as i64 + 1,
                                node_text(left, src),
                                node_text(f, src),
                            ));
                        }
                    }
                }
            }
        }
        collect_assignments(child, src, out);
    }
}

/// The parameter names declared by a `function_definition`. Handles plain,
/// typed, defaulted, and `*args`/`**kwargs` parameters by taking each
/// parameter's first identifier (its `name` field when present).
fn param_names(func: Node, src: &[u8]) -> HashSet<String> {
    let mut names = HashSet::new();
    if let Some(params) = func.child_by_field_name("parameters") {
        let mut c = params.walk();
        for p in params.named_children(&mut c) {
            if p.kind() == "identifier" {
                names.insert(node_text(p, src));
            } else if let Some(n) = p.child_by_field_name("name") {
                names.insert(node_text(n, src));
            } else {
                // typed_parameter / splat patterns: first identifier descendant.
                let mut ic = p.walk();
                let ident = p.named_children(&mut ic).find(|ch| ch.kind() == "identifier");
                if let Some(id) = ident {
                    names.insert(node_text(id, src));
                }
            }
        }
    }
    names
}

/// Map each `function_definition`'s start line → its parameter names, so a call
/// site can be attributed to the parameters of its innermost enclosing function.
fn collect_func_params(node: Node, src: &[u8], out: &mut HashMap<i64, HashSet<String>>) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "function_definition" {
            let start = child.start_position().row as i64 + 1;
            out.insert(start, param_names(child, src));
        }
        collect_func_params(child, src, out);
    }
}

/// Collect `(line, callee_name, arg_identifiers)` for every call site — like
/// `collect_calls`, but also recording each argument that is a bare identifier
/// (including the value of a `keyword_argument`), which is what parameter
/// pass-through detection matches against (Feature 4.12).
fn collect_calls_with_args(node: Node, src: &[u8], out: &mut Vec<(i64, String, Vec<String>)>) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "call" {
            if let Some(func) = child.child_by_field_name("function") {
                let name = match func.kind() {
                    "identifier" => Some(node_text(func, src)),
                    "attribute" => func.child_by_field_name("attribute").map(|a| node_text(a, src)),
                    _ => None,
                };
                if let Some(name) = name {
                    let mut args = Vec::new();
                    if let Some(arglist) = child.child_by_field_name("arguments") {
                        let mut ac = arglist.walk();
                        for a in arglist.named_children(&mut ac) {
                            match a.kind() {
                                "identifier" => args.push(node_text(a, src)),
                                "keyword_argument" => {
                                    if let Some(v) = a.child_by_field_name("value") {
                                        if v.kind() == "identifier" {
                                            args.push(node_text(v, src));
                                        }
                                    }
                                }
                                _ => {}
                            }
                        }
                    }
                    out.push((child.start_position().row as i64 + 1, name, args));
                }
            }
        }
        collect_calls_with_args(child, src, out); // recurse: calls nest in args/bodies
    }
}

/// Build static data-flow rows (Feature 4.12, tier 1). For each call site, if an
/// argument is a bare identifier matching a parameter of the innermost enclosing
/// **function**, record `(caller, callee, param)` in the `dataflow` table: the
/// caller passes its parameter `param` onward into the callee.
///
/// Callee resolution reuses Feature 4.0 (import-aware, then same-file/unique
/// global by name) so no false edges are invented. This is a deliberately
/// **approximate** analysis: name-based only, with no aliasing/reassignment
/// (`y = x; g(y)` is not followed) and no closure capture (a param is matched
/// only to calls inside its own function, not nested inner functions). `DELETE`
/// then `INSERT OR IGNORE` makes re-indexing idempotent. Returns rows written.
#[pyfunction]
pub fn build_dataflow(db_path: &str) -> PyResult<usize> {
    if !Path::new(db_path).exists() {
        return Err(PyOSError::new_err(format!("database not found: '{}'", db_path)));
    }
    let mut conn = Connection::open(db_path)
        .map_err(|e| PyOSError::new_err(format!("cannot open database '{}': {}", db_path, e)))?;

    let syms: Vec<SymRow> = {
        let mut stmt = conn
            .prepare(
                "SELECT id, name, kind, file_id, line_start, line_end, import_module, import_name \
                 FROM symbols",
            )
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        let rows = stmt
            .query_map([], |r| {
                Ok(SymRow {
                    id: r.get(0)?,
                    name: r.get(1)?,
                    kind: r.get(2)?,
                    file_id: r.get(3)?,
                    line_start: r.get(4)?,
                    line_end: r.get(5)?,
                    import_module: r.get(6)?,
                    import_name: r.get(7)?,
                })
            })
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        rows.collect::<Result<_, _>>()
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?
    };

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
    let files_by_id: HashMap<i64, String> = files.iter().cloned().collect();

    // Definition targets by name, non-import symbols by file (caller lookup),
    // and per-function parameter sets (built from the parse below).
    let mut defs_by_name: HashMap<String, Vec<(i64, i64)>> = HashMap::new();
    let mut by_file: HashMap<i64, Vec<usize>> = HashMap::new();
    for (i, s) in syms.iter().enumerate() {
        if s.kind == "function" || s.kind == "class" {
            defs_by_name.entry(s.name.clone()).or_default().push((s.id, s.file_id));
        }
        if s.kind != "import" {
            by_file.entry(s.file_id).or_default().push(i);
        }
    }

    // Import targets per file, so `callee` resolution matches call-edge behaviour.
    let mut import_targets: HashMap<i64, HashMap<String, i64>> = HashMap::new();
    for s in syms.iter().filter(|s| s.kind == "import") {
        let original = s.import_name.as_deref().unwrap_or(&s.name);
        if original == "*" {
            continue;
        }
        if let Resolution::Resolved(dst) =
            resolve_import(&defs_by_name, &files_by_id, original, s.import_module.as_deref())
        {
            if dst != s.id {
                import_targets.entry(s.file_id).or_default().insert(s.name.clone(), dst);
            }
        }
    }

    let mut parser = Parser::new();
    parser
        .set_language(&tree_sitter_python::LANGUAGE.into())
        .map_err(|e| PyRuntimeError::new_err(format!("failed to load Python grammar: {}", e)))?;

    let verbose = crate::verbose();
    let mut skipped = 0usize;
    // (caller_id, callee_id, param)
    let mut rows: Vec<(i64, i64, String)> = Vec::new();

    for (file_id, path) in &files {
        // Data-flow is Python-specific; skip non-Python files (Feature 5.5).
        if !path.ends_with(".py") {
            continue;
        }
        let source = match std::fs::read_to_string(path) {
            Ok(s) => s,
            Err(_) => continue, // unreadable — skip (build_edges already reports)
        };
        let tree = match parser.parse(source.as_bytes(), None) {
            Some(t) => t,
            None => continue,
        };
        let bytes = source.as_bytes();

        let mut func_params: HashMap<i64, HashSet<String>> = HashMap::new();
        collect_func_params(tree.root_node(), bytes, &mut func_params);
        let mut calls = Vec::new();
        collect_calls_with_args(tree.root_node(), bytes, &mut calls);

        let empty = Vec::new();
        let candidates = by_file.get(file_id).unwrap_or(&empty);
        let file_imports = import_targets.get(file_id);

        for (line, callee, args) in calls {
            if args.is_empty() {
                continue;
            }
            // Attribute to the innermost enclosing symbol; only functions carry
            // parameters, so a class-level call site has no pass-through.
            let caller = match innermost(&syms, candidates, line) {
                Some(s) if s.kind == "function" => s,
                _ => continue,
            };
            let params = match caller.line_start.and_then(|ls| func_params.get(&ls)) {
                Some(p) if !p.is_empty() => p,
                _ => continue,
            };
            // Which of this call's arguments are the caller's own parameters?
            let flowing: HashSet<&String> = args.iter().filter(|a| params.contains(*a)).collect();
            if flowing.is_empty() {
                continue;
            }

            // Resolve the callee once (import-aware, then same-file/unique global).
            let dst = if let Some(dst) = file_imports.and_then(|m| m.get(&callee)) {
                Some(*dst)
            } else {
                match resolve_def(&defs_by_name, &callee, Some(*file_id)) {
                    Resolution::Resolved(dst) => Some(dst),
                    _ => {
                        skipped += 1;
                        None
                    }
                }
            };
            if let Some(dst) = dst {
                for param in flowing {
                    rows.push((caller.id, dst, param.clone()));
                }
            }
        }
    }

    let tx = conn
        .transaction()
        .map_err(|e| PyRuntimeError::new_err(format!("failed to begin transaction: {}", e)))?;
    tx.execute("DELETE FROM dataflow", [])
        .map_err(|e| PyRuntimeError::new_err(format!("failed to clear dataflow: {}", e)))?;

    let mut written = 0usize;
    {
        let mut insert = tx
            .prepare("INSERT OR IGNORE INTO dataflow (src_id, dst_id, param) VALUES (?1, ?2, ?3)")
            .map_err(|e| PyRuntimeError::new_err(format!("failed to prepare insert: {}", e)))?;
        for (src_id, dst_id, param) in &rows {
            written += insert
                .execute(params![src_id, dst_id, param])
                .map_err(|e| PyRuntimeError::new_err(format!("failed to write dataflow: {}", e)))?;
        }
    }
    tx.commit()
        .map_err(|e| PyRuntimeError::new_err(format!("failed to commit dataflow: {}", e)))?;

    if skipped > 0 && verbose {
        eprintln!("sylva: build_dataflow: {} rows; {} unresolved callee(s) skipped", written, skipped);
    }
    Ok(written)
}

/// Build `calls` and `imports` edges for the indexed graph. Returns the number
/// of edges written.
#[pyfunction]
pub fn build_edges(db_path: &str) -> PyResult<usize> {
    if !Path::new(db_path).exists() {
        return Err(PyOSError::new_err(format!("database not found: '{}'", db_path)));
    }
    let mut conn = Connection::open(db_path)
        .map_err(|e| PyOSError::new_err(format!("cannot open database '{}': {}", db_path, e)))?;

    // Load all symbols and file records once.
    let syms: Vec<SymRow> = {
        let mut stmt = conn
            .prepare(
                "SELECT id, name, kind, file_id, line_start, line_end, import_module, import_name \
                 FROM symbols",
            )
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        let rows = stmt
            .query_map([], |r| {
                Ok(SymRow {
                    id: r.get(0)?,
                    name: r.get(1)?,
                    kind: r.get(2)?,
                    file_id: r.get(3)?,
                    line_start: r.get(4)?,
                    line_end: r.get(5)?,
                    import_module: r.get(6)?,
                    import_name: r.get(7)?,
                })
            })
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        rows.collect::<Result<_, _>>()
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?
    };

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

    let files_by_id: HashMap<i64, String> = files.iter().cloned().collect();

    // Definitions by name (resolution targets) and non-import symbols by file
    // (caller-span lookup).
    let mut defs_by_name: HashMap<String, Vec<(i64, i64)>> = HashMap::new();
    let mut by_file: HashMap<i64, Vec<usize>> = HashMap::new();
    for (i, s) in syms.iter().enumerate() {
        // `foreign_export` (Feature 5.0) is a resolution target too, so a Python
        // call to a PyO3-exported name links across the language boundary.
        if s.kind == "function" || s.kind == "class" || s.kind == "foreign_export" {
            defs_by_name.entry(s.name.clone()).or_default().push((s.id, s.file_id));
        }
        if s.kind != "import" {
            by_file.entry(s.file_id).or_default().push(i);
        }
    }

    // --- Feature 7.10: class membership for type-aware method resolution ------
    // Each method (a `function` nested in a `class` span) maps to its innermost
    // enclosing class; each class maps its method names to their symbol ids.
    let classes: Vec<(i64, i64, i64, i64)> = syms
        .iter()
        .filter(|s| s.kind == "class")
        .filter_map(|s| Some((s.id, s.file_id, s.line_start?, s.line_end?)))
        .collect();
    let mut method_class: HashMap<i64, i64> = HashMap::new(); // method id -> class id
    let mut class_methods: HashMap<i64, HashMap<String, i64>> = HashMap::new();
    for s in syms.iter().filter(|s| s.kind == "function") {
        if let (Some(fs), Some(fe)) = (s.line_start, s.line_end) {
            let mut best: Option<(i64, i64)> = None; // (class id, span size) — innermost
            for &(cid, cfid, cstart, cend) in &classes {
                if cfid == s.file_id && cstart <= fs && fe <= cend && cid != s.id {
                    let size = cend - cstart;
                    if best.map_or(true, |(_, bsize)| size < bsize) {
                        best = Some((cid, size));
                    }
                }
            }
            if let Some((cid, _)) = best {
                method_class.insert(s.id, cid);
                class_methods.entry(cid).or_default().insert(s.name.clone(), s.id);
            }
        }
    }

    // Parse each file up front (before the transaction borrows the connection).
    let mut parser = Parser::new();
    parser
        .set_language(&tree_sitter_python::LANGUAGE.into())
        .map_err(|e| PyRuntimeError::new_err(format!("failed to load Python grammar: {}", e)))?;
    // Feature 5.6 — a second parser for TS/JS call sites (TSX grammar superset).
    let mut ts_parser = Parser::new();
    ts_parser
        .set_language(&tree_sitter_typescript::LANGUAGE_TSX.into())
        .map_err(|e| PyRuntimeError::new_err(format!("failed to load TS grammar: {}", e)))?;

    let verbose = crate::verbose();
    let mut skipped_ambiguous = 0usize;
    let mut skipped_unresolved = 0usize;
    let mut resolved_typed = 0usize; // Feature 7.10: edges recovered by type inference

    // --- Import edges (resolved first, so calls can use them) ---------------
    // Each import binding → the definition it names, resolved by the *original*
    // imported name + source module (survives aliasing). Also records, per file,
    // which bound names map to which definition, to disambiguate call resolution.
    let mut import_edges: Vec<(i64, i64)> = Vec::new();
    let mut import_targets: HashMap<i64, HashMap<String, i64>> = HashMap::new();
    for s in syms.iter().filter(|s| s.kind == "import") {
        let original = s.import_name.as_deref().unwrap_or(&s.name);
        if original == "*" {
            continue; // wildcard import — nothing specific to resolve
        }
        match resolve_import(&defs_by_name, &files_by_id, original, s.import_module.as_deref()) {
            Resolution::Resolved(dst) if dst != s.id => {
                import_edges.push((s.id, dst));
                import_targets.entry(s.file_id).or_default().insert(s.name.clone(), dst);
            }
            Resolution::Resolved(_) => {} // self-reference, skip
            Resolution::Ambiguous => skipped_ambiguous += 1,
            Resolution::Unresolved => skipped_unresolved += 1,
        }
    }

    // --- Call edges --------------------------------------------------------
    let mut call_edges: Vec<(i64, i64)> = Vec::new();
    for (file_id, path) in &files {
        // Dispatch by language: Python (full), TS/JS (name-based, Feature 5.6),
        // or skip (foreign `.rs` per 5.0, unknown extensions).
        let lang = src_lang(path);
        if matches!(lang, SrcLang::Skip) {
            continue;
        }
        let source = match std::fs::read_to_string(path) {
            Ok(s) => s,
            Err(e) => {
                if verbose {
                    eprintln!("sylva: skipping unreadable file '{}' during edge build: {}", path, e);
                }
                continue;
            }
        };
        let active = match lang {
            SrcLang::TsJs => &mut ts_parser,
            _ => &mut parser,
        };
        let tree = match active.parse(source.as_bytes(), None) {
            Some(t) => t,
            None => {
                if verbose {
                    eprintln!("sylva: failed to parse '{}' during edge build; skipping", path);
                }
                continue;
            }
        };

        // Call sites per language; local-binding assignments are Python-only.
        let mut calls = Vec::new();
        let mut assigns = Vec::new();
        match lang {
            SrcLang::Python => {
                collect_call_sites(tree.root_node(), source.as_bytes(), &mut calls);
                collect_assignments(tree.root_node(), source.as_bytes(), &mut assigns);
            }
            SrcLang::TsJs => {
                collect_call_sites_ts(tree.root_node(), source.as_bytes(), &mut calls);
            }
            SrcLang::Skip => unreachable!(),
        }

        let empty = Vec::new();
        let candidates = by_file.get(file_id).unwrap_or(&empty);
        let file_imports = import_targets.get(file_id);
        for cs in calls {
            let caller = match innermost(&syms, candidates, cs.line) {
                Some(s) => s,
                None => continue, // call outside any symbol (module-level) — no src
            };

            // 0. Feature 7.10 — type-aware receiver resolution (never a false
            //    edge: only a *known* class's own method resolves here).
            let typed_dst: Option<i64> = match &cs.receiver {
                // `self.m()` / `cls.m()` → the enclosing class's `m`.
                Receiver::SelfCls => method_class
                    .get(&caller.id)
                    .and_then(|cid| class_methods.get(cid))
                    .and_then(|m| m.get(&cs.callee))
                    .copied(),
                // `obj.m()` → the method of `obj`'s inferred class, when a single
                // `obj = Class(...)` binding sits in the caller's body.
                Receiver::Local(var) => {
                    let cstart = caller.line_start.unwrap_or(cs.line);
                    let cend = caller.line_end.unwrap_or(cs.line);
                    let mut cls_names: HashSet<&str> = HashSet::new();
                    for (aline, avar, acls) in &assigns {
                        if avar == var && *aline >= cstart && *aline <= cend {
                            cls_names.insert(acls.as_str());
                        }
                    }
                    if cls_names.len() == 1 {
                        let cls_name = cls_names.into_iter().next().unwrap();
                        match resolve_def(&defs_by_name, cls_name, Some(*file_id)) {
                            Resolution::Resolved(cid) => {
                                class_methods.get(&cid).and_then(|m| m.get(&cs.callee)).copied()
                            }
                            _ => None,
                        }
                    } else {
                        None // 0 or conflicting bindings — don't guess
                    }
                }
                _ => None,
            };
            if let Some(dst) = typed_dst {
                call_edges.push((caller.id, dst));
                resolved_typed += 1;
                continue;
            }

            // 1. Import-aware: if this file imports `callee`, use its target.
            if let Some(dst) = file_imports.and_then(|m| m.get(&cs.callee)) {
                call_edges.push((caller.id, *dst));
                continue;
            }

            // 2. Same-file preference, then unique global (issue #36).
            match resolve_def(&defs_by_name, &cs.callee, Some(*file_id)) {
                Resolution::Resolved(dst) => call_edges.push((caller.id, dst)),
                Resolution::Ambiguous => {
                    skipped_ambiguous += 1;
                    if verbose {
                        eprintln!("sylva: ambiguous call reference '{}'; skipping", cs.callee);
                    }
                }
                Resolution::Unresolved => {
                    skipped_unresolved += 1;
                    if verbose {
                        eprintln!("sylva: unresolved call reference '{}'; skipping", cs.callee);
                    }
                }
            }
        }
    }

    // --- Write: clear old calls/imports, then insert imports + calls -------
    let tx = conn
        .transaction()
        .map_err(|e| PyRuntimeError::new_err(format!("failed to begin transaction: {}", e)))?;

    tx.execute("DELETE FROM edges WHERE kind IN ('calls', 'imports')", [])
        .map_err(|e| PyRuntimeError::new_err(format!("failed to clear edges: {}", e)))?;

    let mut written = 0usize;
    {
        let mut insert = tx
            .prepare("INSERT OR IGNORE INTO edges (src_id, dst_id, kind) VALUES (?1, ?2, ?3)")
            .map_err(|e| PyRuntimeError::new_err(format!("failed to prepare insert: {}", e)))?;

        for (src_id, dst_id) in &import_edges {
            written += insert
                .execute(params![src_id, dst_id, "imports"])
                .map_err(|e| PyRuntimeError::new_err(format!("failed to write edge: {}", e)))?;
        }
        // Call edges (self-calls allowed → recursion self-edge).
        for (src_id, dst_id) in &call_edges {
            written += insert
                .execute(params![src_id, dst_id, "calls"])
                .map_err(|e| PyRuntimeError::new_err(format!("failed to write edge: {}", e)))?;
        }
    }

    tx.commit()
        .map_err(|e| PyRuntimeError::new_err(format!("failed to commit edges: {}", e)))?;

    // One bounded summary instead of a line per skipped reference (issue #39).
    // Includes the count recovered by Feature 7.10 type inference.
    if skipped_ambiguous + skipped_unresolved + resolved_typed > 0 {
        eprintln!(
            "sylva: build_edges: {} edges ({} via type inference); \
             skipped {} ambiguous and {} unresolved reference(s){}",
            written,
            resolved_typed,
            skipped_ambiguous,
            skipped_unresolved,
            if verbose { "" } else { " (set SYLVA_LOG=1 for per-reference detail)" }
        );
    }

    Ok(written)
}

// --------------------------------------------------------------------------- //
// Feature 4.1 — call chain tracing
// --------------------------------------------------------------------------- //

/// Breadth-first traversal from `start` over an adjacency map, up to `max_depth`
/// hops. Nodes are visited once (cycle-safe), and each newly-reached node is
/// pushed to `out` as `(id, depth, direction)`.
fn bfs(
    start: &[i64],
    adjacency: &HashMap<i64, Vec<i64>>,
    max_depth: i64,
    direction: &'static str,
    out: &mut Vec<(i64, i64, &'static str)>,
) {
    let mut visited: HashSet<i64> = start.iter().copied().collect();
    let mut frontier: Vec<i64> = start.to_vec();
    let mut depth = 1;
    while depth <= max_depth && !frontier.is_empty() {
        let mut next = Vec::new();
        for node in &frontier {
            if let Some(neighbors) = adjacency.get(node) {
                for &nb in neighbors {
                    if visited.insert(nb) {
                        out.push((nb, depth, direction));
                        next.push(nb);
                    }
                }
            }
        }
        frontier = next;
        depth += 1;
    }
}

/// Trace call chains from `symbol` over `calls` edges.
///
/// `direction`: `outbound` (callees), `inbound` (callers), or `both`. `depth` is
/// a strict cap on the number of hops. Returns a flat list of reached symbols as
/// `{name, kind, file, line, depth, direction}`, where `depth` 0 is the start
/// symbol itself (direction `self`). An unknown symbol yields an empty list; an
/// invalid direction or negative depth raises `ValueError`.
#[pyfunction]
pub fn trace_calls(
    py: Python<'_>,
    db_path: &str,
    symbol: &str,
    direction: &str,
    depth: i64,
) -> PyResult<Py<PyList>> {
    // Validate arguments first, before any DB work.
    if !matches!(direction, "inbound" | "outbound" | "both") {
        return Err(PyValueError::new_err(format!(
            "invalid direction '{}': expected 'inbound', 'outbound', or 'both'",
            direction
        )));
    }
    if depth < 0 {
        return Err(PyValueError::new_err("depth must be >= 0"));
    }
    if !Path::new(db_path).exists() {
        return Err(PyOSError::new_err(format!("database not found: '{}'", db_path)));
    }

    let conn = Connection::open(db_path)
        .map_err(|e| PyOSError::new_err(format!("cannot open database '{}': {}", db_path, e)))?;

    // Symbol details for the output, and name → ids for the start set.
    let mut detail: HashMap<i64, (String, String, Option<i64>, String)> = HashMap::new();
    let mut name_to_ids: HashMap<String, Vec<i64>> = HashMap::new();
    {
        let mut stmt = conn
            .prepare(
                "SELECT s.id, s.name, s.kind, s.line_start, f.path \
                 FROM symbols s JOIN files f ON f.id = s.file_id",
            )
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        let rows = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, Option<i64>>(3)?,
                    r.get::<_, String>(4)?,
                ))
            })
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        for row in rows {
            let (id, name, kind, line, path) =
                row.map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
            name_to_ids.entry(name.clone()).or_default().push(id);
            detail.insert(id, (name, kind, line, path));
        }
    }

    let start_ids: Vec<i64> = name_to_ids.get(symbol).cloned().unwrap_or_default();
    if start_ids.is_empty() {
        return Ok(PyList::empty(py).unbind()); // unknown symbol -> empty, not error
    }

    // Build call adjacency in both directions.
    let mut outgoing: HashMap<i64, Vec<i64>> = HashMap::new();
    let mut incoming: HashMap<i64, Vec<i64>> = HashMap::new();
    {
        let mut stmt = conn
            .prepare("SELECT src_id, dst_id FROM edges WHERE kind = 'calls'")
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
        let rows = stmt
            .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?)))
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
        for row in rows {
            let (src, dst) =
                row.map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
            outgoing.entry(src).or_default().push(dst);
            incoming.entry(dst).or_default().push(src);
        }
    }

    let mut reached: Vec<(i64, i64, &'static str)> = Vec::new();
    for &id in &start_ids {
        reached.push((id, 0, "self"));
    }
    if direction == "outbound" || direction == "both" {
        bfs(&start_ids, &outgoing, depth, "outbound", &mut reached);
    }
    if direction == "inbound" || direction == "both" {
        bfs(&start_ids, &incoming, depth, "inbound", &mut reached);
    }

    // Deterministic order: by depth, then name, then direction.
    reached.sort_by(|a, b| {
        let name_a = detail.get(&a.0).map(|d| d.0.as_str()).unwrap_or("");
        let name_b = detail.get(&b.0).map(|d| d.0.as_str()).unwrap_or("");
        a.1.cmp(&b.1).then(name_a.cmp(name_b)).then(a.2.cmp(b.2))
    });

    let list = PyList::empty(py);
    for (id, node_depth, node_dir) in reached {
        let (name, kind, line, path) = detail.get(&id).expect("id came from detail");
        let d = PyDict::new(py);
        d.set_item("name", name)?;
        d.set_item("kind", kind)?;
        d.set_item("file", path)?;
        d.set_item("line", *line)?;
        d.set_item("depth", node_depth)?;
        d.set_item("direction", node_dir)?;
        list.append(d)?;
    }
    Ok(list.unbind())
}

// --------------------------------------------------------------------------- //
// Feature 4.2 — blast radius analysis
// --------------------------------------------------------------------------- //

/// Everything that would break if `symbol` changed: the full transitive set of
/// symbols that depend on it, via inbound `calls` **and** `imports` edges.
///
/// Returns each affected symbol as `{name, kind, file, line, distance, via}`,
/// where `distance` is the number of hops from the target (1 = a direct
/// dependent) and `via` is the edge kind (`calls`/`imports`) it was first
/// reached by. The target itself is not included, so a leaf symbol (nothing
/// depends on it) returns an empty list. An unknown symbol also returns empty.
/// A `visited` set makes the traversal finite despite cycles.
///
/// Note (issue #37): `imports` edges from Feature 4.0 are name-conflated with
/// their target (and lost for aliased imports), so import relationships rarely
/// surface here yet. The `imports` traversal is correct and will benefit once
/// import-edge modeling is fixed.
#[pyfunction]
pub fn blast_radius(py: Python<'_>, db_path: &str, symbol: &str) -> PyResult<Py<PyList>> {
    if !Path::new(db_path).exists() {
        return Err(PyOSError::new_err(format!("database not found: '{}'", db_path)));
    }
    let conn = Connection::open(db_path)
        .map_err(|e| PyOSError::new_err(format!("cannot open database '{}': {}", db_path, e)))?;

    let mut detail: HashMap<i64, (String, String, Option<i64>, String)> = HashMap::new();
    let mut name_to_ids: HashMap<String, Vec<i64>> = HashMap::new();
    {
        let mut stmt = conn
            .prepare(
                "SELECT s.id, s.name, s.kind, s.line_start, f.path \
                 FROM symbols s JOIN files f ON f.id = s.file_id",
            )
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        let rows = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, Option<i64>>(3)?,
                    r.get::<_, String>(4)?,
                ))
            })
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
        for row in rows {
            let (id, name, kind, line, path) =
                row.map_err(|e| PyRuntimeError::new_err(format!("failed to read symbols: {}", e)))?;
            name_to_ids.entry(name.clone()).or_default().push(id);
            detail.insert(id, (name, kind, line, path));
        }
    }

    // Start from definitions, not import bindings: a `from lib import util`
    // binding named `util` must appear as a dependent (via imports), not as a
    // conflated start node (Feature 7.8 / #37). Fall back to all matches if the
    // name is only ever an import binding.
    let all_ids: Vec<i64> = name_to_ids.get(symbol).cloned().unwrap_or_default();
    let def_ids: Vec<i64> = all_ids
        .iter()
        .copied()
        .filter(|id| detail.get(id).map_or(false, |d| d.1 != "import"))
        .collect();
    let start_ids = if def_ids.is_empty() { all_ids } else { def_ids };
    if start_ids.is_empty() {
        return Ok(PyList::empty(py).unbind());
    }

    // Inbound adjacency over calls + imports: for edge (src -> dst), `src`
    // depends on `dst`, so record `dst -> (src, kind)`.
    let mut inbound: HashMap<i64, Vec<(i64, &'static str)>> = HashMap::new();
    {
        let mut stmt = conn
            .prepare("SELECT src_id, dst_id, kind FROM edges WHERE kind IN ('calls', 'imports')")
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
        let rows = stmt
            .query_map([], |r| {
                Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?, r.get::<_, String>(2)?))
            })
            .map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
        for row in rows {
            let (src, dst, kind) =
                row.map_err(|e| PyRuntimeError::new_err(format!("failed to read edges: {}", e)))?;
            let via: &'static str = if kind == "imports" { "imports" } else { "calls" };
            inbound.entry(dst).or_default().push((src, via));
        }
    }

    // BFS outward from the target over dependents. Unbounded depth; `visited`
    // guarantees termination. The target(s) are excluded from the output.
    let mut visited: HashSet<i64> = start_ids.iter().copied().collect();
    let mut frontier: Vec<i64> = start_ids.clone();
    let mut affected: Vec<(i64, i64, &'static str)> = Vec::new();
    let mut distance = 1;
    while !frontier.is_empty() {
        let mut next = Vec::new();
        for node in &frontier {
            if let Some(dependents) = inbound.get(node) {
                for &(dep, via) in dependents {
                    if visited.insert(dep) {
                        affected.push((dep, distance, via));
                        next.push(dep);
                    }
                }
            }
        }
        frontier = next;
        distance += 1;
    }

    affected.sort_by(|a, b| {
        let name_a = detail.get(&a.0).map(|d| d.0.as_str()).unwrap_or("");
        let name_b = detail.get(&b.0).map(|d| d.0.as_str()).unwrap_or("");
        a.1.cmp(&b.1).then(name_a.cmp(name_b))
    });

    let list = PyList::empty(py);
    for (id, dist, via) in affected {
        let (name, kind, line, path) = detail.get(&id).expect("id came from detail");
        let d = PyDict::new(py);
        d.set_item("name", name)?;
        d.set_item("kind", kind)?;
        d.set_item("file", path)?;
        d.set_item("line", *line)?;
        d.set_item("distance", dist)?;
        d.set_item("via", via)?;
        list.append(d)?;
    }
    Ok(list.unbind())
}
