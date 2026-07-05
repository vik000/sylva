"""Feature 10.4 — understand-project orchestrator.

`onboard(root, db)` runs the full deterministic pipeline that takes an unknown
repo to a documented, navigable, agent-ready state in one call: index the
codebase → write the instruction brief (10.1) → write the diagrams (10.3) →
write the per-project MCP scaffold (8.2). Each step is wrapped so one failure is
recorded and the rest continues (graceful degradation); the pipeline never runs
the target repo.

The optional runtime step — generating e2e tests and tracing real execution
paths (Feature 10.2) — is agent-side and documented in the `sylva-onboard`
skill; it is not invoked by this deterministic CLI.

`index_codebase` is the shared indexing core used by both `analyze` and
`onboard`.
"""

import os

import sylva

_LANG_BY_EXT = {
    "py": "python", "pyi": "python",
    "rs": "rust",
    "ts": "typescript", "tsx": "typescript", "mts": "typescript", "cts": "typescript",
    "js": "javascript", "jsx": "javascript", "mjs": "javascript", "cjs": "javascript",
}


def _lang_of(path):
    return _LANG_BY_EXT.get(os.path.splitext(path)[1].lstrip(".").lower(), "other")


def index_codebase(root, db_path, log=False):
    """Index `root` into the graph at `db_path`; return a summary dict.

    Walks every supported-language file (Feature 5.5) and routes by extension:
    `.rs` is black-boxed by its export surface (Feature 5.0), everything else is
    fully parsed via `extract_symbols` (dispatched by the 5.1 trait). Then builds
    call/import edges and static data-flow (both Python-only for now — non-Python
    symbols have no edges until Feature 5.6). Unreadable files are skipped, not
    fatal. Raises NotADirectoryError if `root` is not a directory.
    """
    if not os.path.isdir(root):
        raise NotADirectoryError(root)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    sylva.init_db(db_path)

    files = sylva.walk_source_files(root)
    symbols, foreign, skipped = 0, 0, 0
    by_language = {}
    for path in files:
        try:
            if path.endswith(".rs"):
                exports = sylva.extract_foreign_exports(path)  # black-box (5.0)
                if exports:
                    sylva.write_symbols(db_path, path, exports)
                    foreign += len(exports)
                    by_language["rust"] = by_language.get("rust", 0) + len(exports)
            else:
                n = sylva.write_symbols(db_path, path, sylva.extract_symbols(path))
                symbols += n
                lang = _lang_of(path)
                by_language[lang] = by_language.get(lang, 0) + n
        except Exception as e:  # unreadable / undecodable — skip, don't abort
            skipped += 1
            if log:
                print(f"sylva: skipping '{path}': {e}")

    edges = sylva.build_edges(db_path)
    flows = sylva.build_dataflow(db_path)
    return {
        "files": len(files),
        "symbols": symbols,
        "skipped": skipped,
        "foreign": foreign,
        "edges": edges,
        "flows": flows,
        "by_language": by_language,
    }


def onboard(root, db_path, brief_out="SYLVA.md", diagram_out="DIAGRAMS.md", mcp_out=".codemcp"):
    """Run the full onboarding pipeline; return `{index, artifacts}`.

    Deterministic and safe (never runs the target). Each artifact step is
    isolated: a failure is recorded under `<name>_error` and the pipeline
    continues, so a partial environment still yields whatever can be produced.
    """
    from .diagrams import generate_diagrams
    from .report import generate_brief

    index = index_codebase(root, db_path)
    artifacts = {"db": db_path}

    def _step(name, fn):
        try:
            artifacts[name] = fn()
        except Exception as e:  # graceful degradation — record, keep going
            artifacts[f"{name}_error"] = str(e)

    def _write(path, producer):
        with open(path, "w") as f:
            f.write(producer(db_path))
        return path

    _step("brief", lambda: _write(brief_out, generate_brief))
    _step("diagrams", lambda: _write(diagram_out, generate_diagrams))
    _step("mcp", lambda: sylva.init_mcp(db_path, mcp_out))

    return {"index": index, "artifacts": artifacts}
