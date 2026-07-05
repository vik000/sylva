"""Coverage + logic-path pipeline and the `understand` orchestrator core.

Three deterministic capabilities the CLI wraps as native subcommands:

- `ingest_coverage(db, report, fmt)` — attach an LCOV/Cobertura report to the
  graph (the red→green overlay + the report's coverage section). Thin wrapper
  over the existing `parse_coverage` / `apply_coverage`.
- `ingest_logic_paths(db, coverage_file)` — turn a coverage.py **contexts**
  database (`dynamic_context = test_function`) into `test_covers` edges, so the
  interactive **execution-path** block diagrams populate. The `.coverage` file
  is read **directly via sqlite3** (dependency-free — Sylva does not import
  coverage.py), decoding the `numbits` bitmap ourselves.
- `test_plan(db)` — the orchestrator's deterministic core: rank the real
  entrypoints and flag which have **no** test exercising them, so an agent knows
  which e2e tests to write.

Sylva stays deterministic and never runs an LLM. The optional `run=` helpers
execute the *project's own* test command (opt-in, in the project's environment)
purely as a convenience; the core ingest paths run nothing.
"""

import os
import subprocess
import sqlite3
import tempfile

import sylva
from .report import _is_test_file


# --------------------------------------------------------------------------- #
# Coverage overlay (LCOV / Cobertura → module colours + report)               #
# --------------------------------------------------------------------------- #

def ingest_coverage(db_path, report_path, fmt="lcov"):
    """Parse an LCOV/Cobertura report and attach it to the graph.

    Returns the number of files in the report. Raises FileNotFoundError if the
    db or report is missing.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")
    if not os.path.exists(report_path):
        raise FileNotFoundError(f"coverage report not found: {report_path}")
    cov = sylva.parse_coverage(report_path, fmt)
    sylva.apply_coverage(db_path, cov)
    return len(cov)


# --------------------------------------------------------------------------- #
# Logic paths — coverage.py contexts → test_covers edges (dependency-free)     #
# --------------------------------------------------------------------------- #

def _numbits_to_nums(blob):
    """Decode coverage.py's `numbits` blob (a little-endian bitmap) to the list
    of line numbers it encodes — bit `b` of byte `i` set ⇒ line `i*8 + b`."""
    nums = []
    for i, byte in enumerate(blob):
        if not byte:
            continue
        for b in range(8):
            if byte & (1 << b):
                nums.append(i * 8 + b)
    return nums


def read_coverage_contexts(coverage_file):
    """Read a coverage.py data file **directly** (no coverage.py import).

    Returns `{context_name: {file_path: [lines]}}` for every non-empty context.
    Raises FileNotFoundError if the file is missing, or ValueError if it is not
    a contexts database (line-level `numbits` schema).
    """
    if not os.path.exists(coverage_file):
        raise FileNotFoundError(f"coverage data file not found: {coverage_file}")
    conn = sqlite3.connect(coverage_file)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"file", "context", "line_bits"} <= tables:
            raise ValueError(
                f"'{coverage_file}' is not a coverage.py line-contexts database. "
                "Run tests with `dynamic_context = test_function` (see `sylva "
                "logic-paths --help`)."
            )
        files = dict(conn.execute("SELECT id, path FROM file"))
        ctxs = dict(conn.execute("SELECT id, context FROM context"))
        out = {}
        for fid, cid, numbits in conn.execute(
                "SELECT file_id, context_id, numbits FROM line_bits"):
            ctx = ctxs.get(cid) or ""
            if not ctx:                       # the empty (no-test) context
                continue
            path = files.get(fid)
            if path is None:
                continue
            lines = _numbits_to_nums(numbits)
            if lines:
                out.setdefault(ctx, {})[path] = lines
        return out
    finally:
        conn.close()


def build_trace(contexts):
    """Convert coverage contexts (`test_module.test_func`) into the trace shape
    `map_tests_to_symbols` expects (`{test_name: {file: [lines]}}`).

    The test key is the bare function name (the last dotted segment) so it
    resolves to the test's symbol by name. Same-named tests across files that
    can't be disambiguated are skipped downstream, not mis-linked.
    """
    trace = {}
    for ctx, files in contexts.items():
        func = ctx.rsplit(".", 1)[-1].split("[", 1)[0]  # drop module + params
        dst = trace.setdefault(func, {})
        for path, lines in files.items():
            merged = set(dst.get(path, ())) | set(lines)
            dst[path] = sorted(merged)
    return trace


def ingest_logic_paths(db_path, coverage_file):
    """Read a coverage contexts db and write `test_covers` edges into the graph.

    Returns `{"tests": n, "edges": m}`. Raises FileNotFoundError if the db or
    coverage file is missing, ValueError if the file lacks contexts.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")
    contexts = read_coverage_contexts(coverage_file)
    trace = build_trace(contexts)
    edges = sylva.map_tests_to_symbols(db_path, trace)
    return {"tests": len(trace), "edges": edges}


# --------------------------------------------------------------------------- #
# Optional convenience: run the project's own tests (opt-in)                   #
# --------------------------------------------------------------------------- #

def run_tests(root, cmd, per_test=False, source=None):
    """Run the project's *own* test command `cmd` in `root` and return the path
    to the `.coverage` it produces. Opt-in convenience — Sylva runs nothing on
    its own; this executes the command the caller supplied, in their env.

    When `per_test` is set, a temporary coverage config enabling
    `dynamic_context = test_function` (so each test is its own context) is wired
    in via `COVERAGE_RCFILE`.
    """
    env = dict(os.environ)
    rc = None
    if per_test:
        fd, rc = tempfile.mkstemp(prefix="sylva-cov-", suffix=".rc")
        with os.fdopen(fd, "w") as f:
            f.write("[run]\ndynamic_context = test_function\n")
            if source:
                f.write(f"source = {source}\n")
        env["COVERAGE_RCFILE"] = rc
    try:
        subprocess.run(cmd, shell=True, cwd=root, env=env, check=False)
    finally:
        if rc and os.path.exists(rc):
            os.remove(rc)
    return os.path.join(root, ".coverage")


# --------------------------------------------------------------------------- #
# understand — orchestrator core: which entrypoints lack a test?              #
# --------------------------------------------------------------------------- #

def test_plan(db_path, limit=15):
    """Rank the real entrypoints and flag which have no test exercising them.

    Returns `{"entrypoints": [...], "untested": [...]}`; each entry carries
    `entrypoint`, `file`, `primary`, `has_test`, and a `reason`. This is what an
    agent reads to know which e2e tests to write. Raises FileNotFoundError if the
    db is missing.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")

    eps = [e for e in sylva.infer_entrypoints(db_path) if not _is_test_file(e["file"])]
    eps.sort(key=lambda e: (not e.get("primary"), -(e.get("reachable") or 0)))
    eps = eps[:limit]

    conn = sqlite3.connect(db_path)
    try:
        covered = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT s.name FROM symbols s "
                "JOIN edges e ON e.dst_id = s.id WHERE e.kind = 'test_covers'")
        }
    finally:
        conn.close()

    reason_by = {t["symbol"]: t.get("reason", "") for t in sylva.suggest_test_targets(db_path)}

    plan = []
    for e in eps:
        name = e["symbol"]
        plan.append({
            "entrypoint": name,
            "file": e["file"],
            "primary": bool(e.get("primary")),
            "reachable": e.get("reachable"),
            "has_test": name in covered,
            "reason": reason_by.get(name) or ("primary entrypoint" if e.get("primary") else "entrypoint"),
        })
    plan.sort(key=lambda p: (p["has_test"], not p["primary"], -(p["reachable"] or 0), p["entrypoint"]))
    return {"entrypoints": plan, "untested": [p for p in plan if not p["has_test"]]}
