"""Feature 3.3 — Test-to-symbol mapping.

`sylva.map_tests_to_symbols(db_path, trace)` builds `test_covers` edges from
test functions to the non-test symbols their covered lines hit. `trace` is
`{test_name: {file: [lines]}}` (coverage.py --contexts shape).
"""

import sqlite3

import pytest

import sylva


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _sym(name, kind, line, line_end):
    return {"name": name, "kind": kind, "line": line, "line_end": line_end}


def _edges(db):
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT s.name, d.name, e.kind FROM edges e "
            "JOIN symbols s ON s.id = e.src_id "
            "JOIN symbols d ON d.id = e.dst_id"
        ).fetchall()
        return set(rows)
    finally:
        conn.close()


def _edge_count(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    finally:
        conn.close()


def _seed(db):
    """A source file with two symbols, and a test file with two tests."""
    sylva.write_symbols(
        str(db), "src/foo.py",
        [_sym("greet", "function", 1, 3), _sym("Widget", "class", 5, 8)],
    )
    sylva.write_symbols(
        str(db), "tests/test_foo.py",
        [_sym("test_greet", "function", 1, 4), _sym("test_widget", "function", 6, 9)],
    )


class TestGeneral:
    def test_edge_to_covered_symbol(self, tmp_path):
        db = _init(tmp_path)
        _seed(db)
        # test_greet exercised line 2, which lives inside greet (1-3).
        trace = {"test_greet": {"src/foo.py": [2]}}
        n = sylva.map_tests_to_symbols(str(db), trace)
        assert n == 1
        assert _edges(db) == {("test_greet", "greet", "test_covers")}

    def test_multiple_symbols_hit(self, tmp_path):
        db = _init(tmp_path)
        _seed(db)
        trace = {"test_greet": {"src/foo.py": [2, 6]}}  # greet + Widget
        n = sylva.map_tests_to_symbols(str(db), trace)
        assert n == 2
        assert _edges(db) == {
            ("test_greet", "greet", "test_covers"),
            ("test_greet", "Widget", "test_covers"),
        }

    def test_multiple_lines_same_symbol_one_edge(self, tmp_path):
        db = _init(tmp_path)
        _seed(db)
        # Three lines, all inside greet -> a single edge.
        trace = {"test_greet": {"src/foo.py": [1, 2, 3]}}
        n = sylva.map_tests_to_symbols(str(db), trace)
        assert n == 1

    def test_innermost_symbol_wins(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(
            str(db), "src/c.py",
            [_sym("Outer", "class", 1, 5), _sym("method", "function", 2, 4)],
        )
        sylva.write_symbols(str(db), "tests/t.py", [_sym("test_m", "function", 1, 2)])
        # Line 3 is inside both Outer(1-5) and method(2-4) -> attaches to method.
        trace = {"test_m": {"src/c.py": [3]}}
        sylva.map_tests_to_symbols(str(db), trace)
        assert _edges(db) == {("test_m", "method", "test_covers")}


class TestEdge:
    def test_test_covering_nothing_makes_no_edge(self, tmp_path):
        db = _init(tmp_path)
        _seed(db)
        # Line 99 is outside every symbol span.
        trace = {"test_greet": {"src/foo.py": [99]}}
        n = sylva.map_tests_to_symbols(str(db), trace)
        assert n == 0
        assert _edges(db) == set()

    def test_self_and_test_symbols_excluded(self, tmp_path):
        db = _init(tmp_path)
        _seed(db)
        # The trace includes the test's own file lines; those resolve to the
        # test symbol (self) or another test_ symbol -> no edges from them.
        trace = {"test_greet": {"tests/test_foo.py": [1, 2, 6]}}
        n = sylva.map_tests_to_symbols(str(db), trace)
        assert n == 0
        assert _edges(db) == set()

    def test_fixture_only_lines_no_edge(self, tmp_path):
        db = _init(tmp_path)
        # A module-level line (line 10) not inside any symbol span.
        sylva.write_symbols(str(db), "src/foo.py", [_sym("greet", "function", 1, 3)])
        sylva.write_symbols(str(db), "tests/t.py", [_sym("test_fix", "function", 1, 2)])
        trace = {"test_fix": {"src/foo.py": [10]}}
        assert sylva.map_tests_to_symbols(str(db), trace) == 0


class TestNegative:
    def test_unindexed_test_skipped(self, tmp_path):
        db = _init(tmp_path)
        _seed(db)
        # 'test_missing' is not a symbol in the graph -> skipped, no error.
        trace = {"test_missing": {"src/foo.py": [2]}}
        assert sylva.map_tests_to_symbols(str(db), trace) == 0
        assert _edges(db) == set()

    def test_unknown_trace_file_skipped(self, tmp_path):
        db = _init(tmp_path)
        _seed(db)
        trace = {"test_greet": {"nowhere/ghost.py": [1]}}
        assert sylva.map_tests_to_symbols(str(db), trace) == 0

    def test_line_in_no_symbol_skipped(self, tmp_path):
        db = _init(tmp_path)
        _seed(db)
        trace = {"test_greet": {"src/foo.py": [4]}}  # gap between greet and Widget
        assert sylva.map_tests_to_symbols(str(db), trace) == 0


class TestErrorControl:
    def test_duplicate_edges_not_duplicated(self, tmp_path):
        db = _init(tmp_path)
        _seed(db)
        trace = {"test_greet": {"src/foo.py": [2]}}
        first = sylva.map_tests_to_symbols(str(db), trace)
        second = sylva.map_tests_to_symbols(str(db), trace)
        assert first == 1
        assert second == 0            # nothing new inserted the second time
        assert _edge_count(db) == 1   # no duplicate row

    def test_path_reconciliation_absolute_graph(self, tmp_path):
        # Relative trace path reconciles to an absolute graph path (suffix).
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "/repo/src/foo.py", [_sym("greet", "function", 1, 3)])
        sylva.write_symbols(str(db), "/repo/tests/t.py", [_sym("test_g", "function", 1, 2)])
        trace = {"test_g": {"src/foo.py": [2]}}
        assert sylva.map_tests_to_symbols(str(db), trace) == 1
        assert _edges(db) == {("test_g", "greet", "test_covers")}


class TestMigration:
    def test_migrations_apply_incrementally_from_v1(self, tmp_path):
        # Build a genuine v1-schema database by hand (no unique edge index, no
        # import_* columns), as real users from an early release would have, and
        # verify init_db upgrades it all the way to the current version (v3).
        db = tmp_path / "sylva.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(
            """
            CREATE TABLE files (
                id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                hash TEXT, indexed_at TEXT
            );
            CREATE TABLE symbols (
                id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL,
                name TEXT NOT NULL, kind TEXT NOT NULL,
                line_start INTEGER, line_end INTEGER,
                docstring TEXT, coverage_pct REAL
            );
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY, src_id INTEGER NOT NULL,
                dst_id INTEGER NOT NULL, kind TEXT NOT NULL
            );
            PRAGMA user_version = 1;
            """
        )
        conn.commit()
        conn.close()

        sylva.init_db(str(db))  # incremental upgrade v1 -> v2 -> v3

        conn = sqlite3.connect(str(db))
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            idx = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name='idx_edges_unique'"
            ).fetchone()
            cols = {r[1] for r in conn.execute("PRAGMA table_info(symbols)")}
        finally:
            conn.close()
        assert version == 3
        assert idx is not None  # v2 applied
        assert {"import_module", "import_name"} <= cols  # v3 applied
