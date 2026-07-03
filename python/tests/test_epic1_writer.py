"""Feature 1.5 — Graph writer.

Verifies `sylva.write_symbols(db_path, file_path, symbols)` upserts a file
record and its symbols into SQLite, is idempotent across repeated writes,
rejects an unusable db path, and rolls back the whole transaction on a partial
write failure.
"""

import sqlite3

import pytest

import sylva


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _rows(db, table):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        conn.close()


def _symbol_names(db):
    conn = sqlite3.connect(str(db))
    try:
        return {r[0] for r in conn.execute("SELECT name FROM symbols").fetchall()}
    finally:
        conn.close()


SYMS = [
    {"name": "greet", "kind": "function", "line": 4, "docstring": "hi"},
    {"name": "Widget", "kind": "class", "line": 8, "docstring": None},
    {"name": "os", "kind": "import", "line": 1, "docstring": None},
]


class TestGeneral:
    def test_symbols_written_and_count_returned(self, tmp_path):
        db = _init(tmp_path)
        n = sylva.write_symbols(str(db), "mod.py", SYMS)
        assert n == 3
        assert _symbol_names(db) == {"greet", "Widget", "os"}

    def test_file_record_created(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "mod.py", SYMS)
        files = _rows(db, "files")
        assert len(files) == 1
        assert files[0][1] == "mod.py"  # path column
        assert files[0][3] is not None  # indexed_at populated

    def test_fields_persisted_correctly(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "mod.py", SYMS)
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT kind, line_start, line_end, docstring "
                "FROM symbols WHERE name = 'greet'"
            ).fetchone()
        finally:
            conn.close()
        kind, line_start, line_end, docstring = row
        assert kind == "function"
        assert line_start == 4
        assert line_end is None  # not provided by the extractor yet
        assert docstring == "hi"

    def test_error_sentinel_is_skipped(self, tmp_path):
        db = _init(tmp_path)
        syms = SYMS + [{"name": "parse_error", "kind": "error", "line": 9, "docstring": None}]
        n = sylva.write_symbols(str(db), "mod.py", syms)
        assert n == 3  # error sentinel not counted
        assert "parse_error" not in _symbol_names(db)

    def test_empty_symbol_list(self, tmp_path):
        db = _init(tmp_path)
        n = sylva.write_symbols(str(db), "empty.py", [])
        assert n == 0
        # File record still created even with no symbols.
        assert len(_rows(db, "files")) == 1


class TestIdempotency:
    def test_writing_twice_no_duplicates(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "mod.py", SYMS)
        sylva.write_symbols(str(db), "mod.py", SYMS)
        assert len(_rows(db, "symbols")) == 3
        assert len(_rows(db, "files")) == 1

    def test_reindex_replaces_symbols(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "mod.py", SYMS)
        # Simulate the file changing: different symbol set.
        new = [{"name": "renamed", "kind": "function", "line": 1, "docstring": None}]
        sylva.write_symbols(str(db), "mod.py", new)
        assert _symbol_names(db) == {"renamed"}  # stale rows gone


class TestNegative:
    def test_invalid_db_path_raises(self, tmp_path):
        # Parent directory does not exist — cannot open/create the db.
        bad = tmp_path / "no_such_dir" / "sylva.db"
        with pytest.raises(Exception):
            sylva.write_symbols(str(bad), "mod.py", SYMS)

    def test_non_dict_symbol_raises(self, tmp_path):
        db = _init(tmp_path)
        with pytest.raises(Exception):
            sylva.write_symbols(str(db), "mod.py", ["not a dict"])


class TestErrorControl:
    def test_partial_write_rolls_back(self, tmp_path):
        db = _init(tmp_path)
        # Seed a good first write.
        sylva.write_symbols(str(db), "mod.py", [{"name": "keeper", "kind": "function", "line": 1}])
        assert _symbol_names(db) == {"keeper"}

        # Second write: a valid symbol followed by a malformed one (name=None).
        # It must raise AND leave the original data untouched (the delete +
        # partial insert are rolled back together).
        bad_batch = [
            {"name": "newsym", "kind": "function", "line": 2},
            {"name": None, "kind": "function", "line": 3},
        ]
        with pytest.raises(Exception):
            sylva.write_symbols(str(db), "mod.py", bad_batch)

        # Rollback verified: original symbol survives, partial insert discarded.
        assert _symbol_names(db) == {"keeper"}
