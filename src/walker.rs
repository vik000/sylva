//! Python file walker.
//!
//! Recurses a directory tree and returns every `.py` file, honouring the
//! `.gitignore` hierarchy and `.codemcpignore` files (Feature 2.3, which
//! replaces the basic Epic 1 walker). Ignore handling:
//! - `.gitignore` files are respected at every level of the tree, and — unlike
//!   raw git — even when there is no enclosing `.git` repository
//!   (`require_git(false)`), so indexing a plain source tree still obeys them.
//! - `.codemcpignore` files are respected too, as Sylva's own ignore file.
//! - The caller may pass `extra_ignores` for ad-hoc patterns.
//!
//! Robustness (per project rules): every path is either yielded as a valid
//! result, skipped because it is not a `.py` file, or — if it cannot be read
//! (permission denied) or a `.gitignore` line is malformed — logged and
//! skipped. A missing root is explicitly rejected with `FileNotFoundError`.
//! The walk never aborts partway through because of one bad entry.

use ignore::gitignore::{Gitignore, GitignoreBuilder};
use ignore::WalkBuilder;
use pyo3::exceptions::{PyFileNotFoundError, PyValueError};
use pyo3::prelude::*;
use std::path::Path;

/// Build a gitignore-style matcher from caller-supplied extra patterns.
/// Malformed individual patterns are logged and skipped; a wholesale build
/// failure is rejected with `ValueError`.
fn build_extra_matcher(root: &Path, patterns: &Option<Vec<String>>) -> PyResult<Option<Gitignore>> {
    let patterns = match patterns {
        Some(p) if !p.is_empty() => p,
        _ => return Ok(None),
    };

    let mut builder = GitignoreBuilder::new(root);
    for pattern in patterns {
        if let Err(e) = builder.add_line(None, pattern) {
            eprintln!("sylva: skipping malformed ignore pattern '{}': {}", pattern, e);
        }
    }

    builder
        .build()
        .map(Some)
        .map_err(|e| PyValueError::new_err(format!("invalid extra ignore patterns: {}", e)))
}

/// Walk `root` and return the paths of all `.py` files, as strings.
///
/// - `root`: directory (or single file) to walk.
/// - `extra_ignores`: optional gitignore-style patterns to exclude, in
///   addition to any `.gitignore` / `.codemcpignore` rules.
///
/// Returns a sorted list for deterministic output. Raises `FileNotFoundError`
/// if `root` does not exist, and `ValueError` if the extra ignore patterns are
/// unusable.
#[pyfunction]
#[pyo3(signature = (root, extra_ignores=None))]
pub fn walk_python_files(root: &str, extra_ignores: Option<Vec<String>>) -> PyResult<Vec<String>> {
    let root_path = Path::new(root);
    if !root_path.exists() {
        return Err(PyFileNotFoundError::new_err(format!(
            "root path does not exist: '{}'",
            root
        )));
    }

    let extra = build_extra_matcher(root_path, &extra_ignores)?;

    let mut builder = WalkBuilder::new(root_path);
    builder
        // Honour .gitignore even when the tree is not a git repo. This is the
        // heart of Feature 2.3 — without it the ignore crate treats .gitignore
        // as inert outside a checkout.
        .require_git(false)
        // Keep results reproducible and confined to the walked tree: don't pull
        // in the developer's global gitignore or ignore files above `root`.
        .git_global(false)
        .parents(false)
        // Sylva's own ignore file, respected per-directory like .gitignore.
        .add_custom_ignore_filename(".codemcpignore");

    let mut results: Vec<String> = Vec::new();
    for entry in builder.build() {
        let entry = match entry {
            Ok(entry) => entry,
            Err(err) => {
                // Unreadable directory / dangling symlink / etc. — log and
                // keep going rather than aborting the whole walk.
                eprintln!("sylva: skipping unreadable path during walk: {}", err);
                continue;
            }
        };

        let path = entry.path();

        // Only files with a `.py` extension are of interest.
        let is_py = path.is_file() && path.extension().map_or(false, |ext| ext == "py");
        if !is_py {
            continue;
        }

        // Apply caller-supplied extra ignore patterns. Check parent
        // directories too, so a directory pattern like `build/` excludes the
        // files inside it, matching gitignore semantics.
        if let Some(matcher) = &extra {
            if matcher.matched_path_or_any_parents(path, false).is_ignore() {
                continue;
            }
        }

        results.push(path.to_string_lossy().into_owned());
    }

    results.sort();
    Ok(results)
}
