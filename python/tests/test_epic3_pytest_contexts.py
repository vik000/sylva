"""Feature 3.3 follow-up — pytest context-id mapping (issue #35).

`map_tests_to_symbols` now accepts coverage.py `--contexts` labels
(`tests/test_x.py::test_m[param]|run`) as trace keys, not just bare test names,
and uses the file component to disambiguate same-named tests across files.
"""

import sqlite3

import pytest

import sylva


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    sylva.write_symbols(str(db), str(path), sylva.extract_symbols(str(path)))
    return path


def _test_covers(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT s.name, sf.path, d.name FROM edges e "
            "JOIN symbols s ON s.id = e.src_id JOIN files sf ON sf.id = s.file_id "
            "JOIN symbols d ON d.id = e.dst_id WHERE e.kind = 'test_covers'"
        ).fetchall()
    finally:
        conn.close()


def _setup(tmp_path):
    """A source symbol `target` (mod.py:1-2) and a test file with test_a."""
    db = _init(tmp_path)
    src = _index(db, tmp_path / "src" / "mod.py", "def target():\n    return 1\n")
    _index(db, tmp_path / "tests" / "test_mod.py", "def test_a():\n    return 1\n")
    return db, str(src)


class TestNodeIdResolution:
    def test_plain_node_id(self, tmp_path):
        db, src = _setup(tmp_path)
        trace = {"tests/test_mod.py::test_a": {src: [1]}}
        assert sylva.map_tests_to_symbols(str(db), trace) == 1
        covers = _test_covers(db)
        assert any(s == "test_a" and d == "target" for (s, _p, d) in covers)

    def test_coverage_phase_suffix(self, tmp_path):
        db, src = _setup(tmp_path)
        # coverage.py appends |run / |setup etc.
        trace = {"tests/test_mod.py::test_a|run": {src: [1]}}
        assert sylva.map_tests_to_symbols(str(db), trace) == 1

    def test_parametrized_id(self, tmp_path):
        db, src = _setup(tmp_path)
        trace = {"tests/test_mod.py::test_a[case-1]": {src: [1]}}
        assert sylva.map_tests_to_symbols(str(db), trace) == 1

    def test_class_based_node_id(self, tmp_path):
        db = _init(tmp_path)
        src = _index(db, tmp_path / "src" / "mod.py", "def target():\n    return 1\n")
        _index(
            db, tmp_path / "tests" / "test_c.py",
            "class TestX:\n    def test_m(self):\n        return 1\n",
        )
        trace = {"tests/test_c.py::TestX::test_m": {str(src): [1]}}
        assert sylva.map_tests_to_symbols(str(db), trace) == 1
        assert any(s == "test_m" and d == "target" for (s, _p, d) in _test_covers(db))

    def test_backward_compatible_plain_name(self, tmp_path):
        # A bare test name (no ::) still resolves, as before.
        db, src = _setup(tmp_path)
        trace = {"test_a": {src: [1]}}
        assert sylva.map_tests_to_symbols(str(db), trace) == 1


class TestFileDisambiguation:
    def test_same_named_tests_disambiguated_by_file(self, tmp_path):
        db = _init(tmp_path)
        src = _index(db, tmp_path / "src" / "mod.py", "def target():\n    return 1\n")
        # Two test files define the same test name.
        a = _index(db, tmp_path / "tests" / "a" / "test_x.py", "def test_dup():\n    return 1\n")
        _index(db, tmp_path / "tests" / "b" / "test_x.py", "def test_dup():\n    return 2\n")

        # Node-id names the a/ file -> edge must come from a/'s test_dup.
        trace = {"tests/a/test_x.py::test_dup": {str(src): [1]}}
        assert sylva.map_tests_to_symbols(str(db), trace) == 1
        rows = [(s, p) for (s, p, d) in _test_covers(db) if s == "test_dup"]
        assert len(rows) == 1
        assert rows[0][1] == str(a)  # resolved to tests/a, not tests/b

    def test_ambiguous_plain_name_still_skipped(self, tmp_path):
        # Without a file hint, a same-named test across files stays ambiguous.
        db = _init(tmp_path)
        src = _index(db, tmp_path / "src" / "mod.py", "def target():\n    return 1\n")
        _index(db, tmp_path / "tests" / "a" / "test_x.py", "def test_dup():\n    return 1\n")
        _index(db, tmp_path / "tests" / "b" / "test_x.py", "def test_dup():\n    return 2\n")
        trace = {"test_dup": {str(src): [1]}}  # bare name, ambiguous
        assert sylva.map_tests_to_symbols(str(db), trace) == 0
