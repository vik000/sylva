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
import pathlib

import sylva


def _walk_foreign(root):
    """Yield foreign-language source files to black-box (Feature 5.0).

    Currently Rust (`.rs`); build artefacts (`target/`) and hidden dirs skipped.
    """
    for p in pathlib.Path(root).rglob("*.rs"):
        if "target" in p.parts or any(seg.startswith(".") for seg in p.parts):
            continue
        yield str(p)


def index_codebase(root, db_path, log=False):
    """Index `root` into the graph at `db_path`; return a summary dict.

    Walks Python files (symbols) and foreign Rust files (black-box exports),
    writes them, then builds call/import edges and static data-flow. Unreadable
    files are skipped, not fatal. Raises NotADirectoryError if `root` is not a
    directory.
    """
    if not os.path.isdir(root):
        raise NotADirectoryError(root)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    sylva.init_db(db_path)

    files = sylva.walk_python_files(root)
    symbols = 0
    skipped = 0
    for path in files:
        try:
            symbols += sylva.write_symbols(db_path, path, sylva.extract_symbols(path))
        except Exception as e:  # unreadable / undecodable — skip, don't abort
            skipped += 1
            if log:
                print(f"sylva: skipping '{path}': {e}")

    foreign = 0
    for path in _walk_foreign(root):
        try:
            exports = sylva.extract_foreign_exports(path)
            if exports:
                sylva.write_symbols(db_path, path, exports)
                foreign += len(exports)
        except Exception as e:
            if log:
                print(f"sylva: skipping foreign '{path}': {e}")

    edges = sylva.build_edges(db_path)
    flows = sylva.build_dataflow(db_path)
    return {
        "files": len(files),
        "symbols": symbols,
        "skipped": skipped,
        "foreign": foreign,
        "edges": edges,
        "flows": flows,
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
