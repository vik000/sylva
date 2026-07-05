"""Native pipeline subcommands: `coverage`, `logic-paths`, `understand`.

Covers the deterministic core in `sylva.pipeline`:
- the dependency-free coverage.py contexts reader (`numbits` decode + `.coverage`
  sqlite read) → `test_covers` edges (the execution-path block diagrams),
- the LCOV coverage overlay ingest,
- the `understand` orchestrator plan (which entrypoints lack a test),
and the three CLI subcommands that wrap them.

The `.coverage` file is synthesised directly (same schema coverage.py writes) so
the bridge is tested without importing coverage.py — matching how Sylva reads it.
"""

import json
import sqlite3

import pytest

import sylva
from sylva import pipeline


# --- synthetic coverage.py data file (dependency-free) --------------------- #

def _nums_to_numbits(nums):
    if not nums:
        return b""
    ba = bytearray((max(nums) // 8) + 1)
    for n in nums:
        ba[n // 8] |= 1 << (n % 8)
    return bytes(ba)


def _make_coverage(path, data):
    """`data`: {context: {file_path: [lines]}} → a coverage.py contexts db."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE file (id integer primary key, path text, unique(path));"
        "CREATE TABLE context (id integer primary key, context text, unique(context));"
        "CREATE TABLE line_bits (file_id integer, context_id integer, numbits blob,"
        " unique(file_id, context_id));"
    )
    files, ctxs = {}, {}

    def fid(p):
        if p not in files:
            files[p] = len(files) + 1
            conn.execute("INSERT INTO file(id, path) VALUES(?, ?)", (files[p], p))
        return files[p]

    def cid(c):
        if c not in ctxs:
            ctxs[c] = len(ctxs) + 1
            conn.execute("INSERT INTO context(id, context) VALUES(?, ?)", (ctxs[c], c))
        return ctxs[c]

    cid("")  # the empty (no-test) context, as real coverage writes
    for ctx, fmap in data.items():
        for f, lines in fmap.items():
            conn.execute(
                "INSERT INTO line_bits(file_id, context_id, numbits) VALUES(?, ?, ?)",
                (fid(f), cid(ctx), _nums_to_numbits(lines)),
            )
    conn.commit()
    conn.close()


# --- a small indexed graph: a source target + a test ----------------------- #

SRC = "def target():\n    return 1\n\ndef helper():\n    return 2\n"
TEST = "def test_foo():\n    from src import target\n    assert target() == 1\n"


def _repo(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    src = tmp_path / "src.py"
    src.write_text(SRC)
    tst = tmp_path / "test_src.py"
    tst.write_text(TEST)
    for p in (src, tst):
        sylva.write_symbols(str(db), str(p), sylva.extract_symbols(str(p)))
    sylva.build_edges(str(db))
    return db, src, tst


# --- numbits + contexts reader --------------------------------------------- #

class TestBridge:
    def test_numbits_roundtrip(self):
        for nums in ([], [1], [1, 2, 4, 5], [0, 7, 8, 63]):
            assert pipeline._numbits_to_nums(_nums_to_numbits(nums)) == sorted(nums)

    def test_read_contexts(self, tmp_path):
        cov = tmp_path / ".coverage"
        _make_coverage(cov, {"m.test_a": {"/x/src.py": [1, 2]}})
        ctx = pipeline.read_coverage_contexts(str(cov))
        assert ctx == {"m.test_a": {"/x/src.py": [1, 2]}}

    def test_read_contexts_skips_empty(self, tmp_path):
        cov = tmp_path / ".coverage"
        _make_coverage(cov, {"": {"/x/src.py": [1]}})  # only the no-test context
        assert pipeline.read_coverage_contexts(str(cov)) == {}

    def test_read_rejects_non_contexts_db(self, tmp_path):
        bogus = tmp_path / ".coverage"
        sqlite3.connect(str(bogus)).execute("CREATE TABLE x(a)")
        with pytest.raises(ValueError):
            pipeline.read_coverage_contexts(str(bogus))

    def test_build_trace_bare_func_key(self):
        trace = pipeline.build_trace({"pkg.mod.test_thing[case1]": {"/a.py": [1]}})
        assert list(trace) == ["test_thing"]           # module + params stripped


# --- logic paths end to end ------------------------------------------------ #

class TestLogicPaths:
    def test_ingest_writes_test_covers(self, tmp_path):
        db, src, _ = _repo(tmp_path)
        cov = tmp_path / ".coverage"
        # test_foo exercised target()'s lines (1-2) in src.py.
        _make_coverage(cov, {"test_src.test_foo": {str(src): [1, 2]}})
        res = pipeline.ingest_logic_paths(str(db), str(cov))
        assert res["tests"] == 1 and res["edges"] == 1
        n = sqlite3.connect(str(db)).execute(
            "SELECT COUNT(*) FROM edges WHERE kind='test_covers'").fetchone()[0]
        assert n == 1

    def test_missing_db_raises(self, tmp_path):
        cov = tmp_path / ".coverage"
        _make_coverage(cov, {"m.test_a": {"/x.py": [1]}})
        with pytest.raises(FileNotFoundError):
            pipeline.ingest_logic_paths(str(tmp_path / "nope.db"), str(cov))

    def test_missing_coverage_raises(self, tmp_path):
        db, _, _ = _repo(tmp_path)
        with pytest.raises(FileNotFoundError):
            pipeline.ingest_logic_paths(str(db), str(tmp_path / "nope.coverage"))


# --- coverage overlay ------------------------------------------------------ #

class TestCoverageOverlay:
    def test_ingest_lcov(self, tmp_path):
        db, src, _ = _repo(tmp_path)
        lcov = tmp_path / "coverage.lcov"
        lcov.write_text(f"SF:{src}\nDA:1,1\nDA:2,1\nDA:4,0\nDA:5,0\nend_of_record\n")
        n = pipeline.ingest_coverage(str(db), str(lcov), "lcov")
        assert n == 1
        cov = sqlite3.connect(str(db)).execute(
            "SELECT coverage_pct FROM symbols WHERE name='target'").fetchone()[0]
        assert cov == 100.0

    def test_missing_report_raises(self, tmp_path):
        db, _, _ = _repo(tmp_path)
        with pytest.raises(FileNotFoundError):
            pipeline.ingest_coverage(str(db), str(tmp_path / "nope.lcov"), "lcov")


# --- understand plan ------------------------------------------------------- #

class TestTestPlan:
    def test_flags_untested_entrypoint(self, tmp_path):
        db, src, _ = _repo(tmp_path)
        plan = pipeline.test_plan(str(db))
        names = {p["entrypoint"] for p in plan["entrypoints"]}
        assert "target" in names
        assert all(p["has_test"] is False for p in plan["entrypoints"])
        assert plan["untested"]

    def test_test_marks_covered(self, tmp_path):
        db, src, _ = _repo(tmp_path)
        cov = tmp_path / ".coverage"
        _make_coverage(cov, {"test_src.test_foo": {str(src): [1, 2]}})
        pipeline.ingest_logic_paths(str(db), str(cov))
        plan = pipeline.test_plan(str(db))
        by = {p["entrypoint"]: p for p in plan["entrypoints"]}
        assert by["target"]["has_test"] is True   # now exercised by a test

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            pipeline.test_plan(str(tmp_path / "nope.db"))


# --- CLI wiring ------------------------------------------------------------ #

class TestCli:
    def test_coverage_cmd(self, tmp_path):
        import sylva.__main__ as cli

        db, src, _ = _repo(tmp_path)
        lcov = tmp_path / "coverage.lcov"
        lcov.write_text(f"SF:{src}\nDA:1,1\nDA:2,1\nend_of_record\n")
        rc = cli.main(["coverage", "--db", str(db), "--report", str(lcov)])
        assert rc == 0

    def test_coverage_cmd_no_report(self, tmp_path):
        import sylva.__main__ as cli

        db, _, _ = _repo(tmp_path)
        rc = cli.main(["coverage", "--db", str(db)])  # nothing to ingest
        assert rc == 1

    def test_logic_paths_cmd(self, tmp_path):
        import sylva.__main__ as cli

        db, src, _ = _repo(tmp_path)
        cov = tmp_path / ".coverage"
        _make_coverage(cov, {"test_src.test_foo": {str(src): [1, 2]}})
        rc = cli.main(["logic-paths", "--db", str(db), "--coverage-file", str(cov)])
        assert rc == 0

    def test_logic_paths_cmd_missing_file(self, tmp_path):
        import sylva.__main__ as cli

        db, _, _ = _repo(tmp_path)
        rc = cli.main(["logic-paths", "--db", str(db), "--coverage-file", str(tmp_path / "no.coverage")])
        assert rc == 1

    def test_understand_cmd(self, tmp_path):
        import sylva.__main__ as cli

        (tmp_path / "src.py").write_text(SRC)
        db = tmp_path / "sylva.db"
        rc = cli.main(["understand", "--root", str(tmp_path), "--db", str(db), "--json"])
        assert rc == 0

    def test_understand_cmd_bad_root(self, tmp_path):
        import sylva.__main__ as cli

        rc = cli.main(["understand", "--root", str(tmp_path / "nope"), "--db", str(tmp_path / "x.db")])
        assert rc == 1
