"""Feature 1.2 — SQLite schema and migrations.

Verifies `sylva.init_db(path)` creates the graph database, applies the schema
idempotently, and reports filesystem / migration errors clearly.
"""

import sqlite3

import pytest

import sylva


EXPECTED_TABLES = {"files", "symbols", "edges"}

# table name -> set of expected column names
EXPECTED_COLUMNS = {
    "files": {"id", "path", "hash", "indexed_at"},
    "symbols": {
        "id",
        "file_id",
        "name",
        "kind",
        "line_start",
        "line_end",
        "docstring",
        "coverage_pct",
        # Added by migration v3 (Feature 7.8 / #37).
        "import_module",
        "import_name",
    },
    "edges": {"id", "src_id", "dst_id", "kind"},
}


def _tables(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _columns(db_path, table):
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1] for r in rows}  # r[1] is the column name
    finally:
        conn.close()


def _user_version(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


class TestGeneral:
    def test_creates_db_file(self, tmp_path):
        db = tmp_path / ".codemcp" / "sylva.db"
        assert not db.exists()
        sylva.init_db(str(db))
        assert db.exists()

    def test_all_tables_present(self, tmp_path):
        db = tmp_path / "sylva.db"
        sylva.init_db(str(db))
        assert EXPECTED_TABLES.issubset(_tables(db))

    @pytest.mark.parametrize("table", sorted(EXPECTED_TABLES))
    def test_columns_correct(self, tmp_path, table):
        db = tmp_path / "sylva.db"
        sylva.init_db(str(db))
        assert _columns(db, table) == EXPECTED_COLUMNS[table]

    def test_schema_version_recorded(self, tmp_path):
        db = tmp_path / "sylva.db"
        sylva.init_db(str(db))
        # Migrations shipped: v1 (schema), v2 (unique edge index — 3.3),
        # v3 (import_module/import_name columns — 7.8).
        assert _user_version(db) == 3

    def test_creates_db_in_cwd_without_parent(self, tmp_path, monkeypatch):
        # A bare filename (no directory component) must not error on the
        # parent-directory handling.
        monkeypatch.chdir(tmp_path)
        sylva.init_db("sylva.db")
        assert (tmp_path / "sylva.db").exists()


class TestIdempotency:
    def test_second_call_no_error(self, tmp_path):
        db = tmp_path / "sylva.db"
        sylva.init_db(str(db))
        sylva.init_db(str(db))  # must not raise

    def test_no_duplicate_tables(self, tmp_path):
        db = tmp_path / "sylva.db"
        sylva.init_db(str(db))
        first = _tables(db)
        sylva.init_db(str(db))
        assert _tables(db) == first

    def test_existing_data_preserved(self, tmp_path):
        db = tmp_path / "sylva.db"
        sylva.init_db(str(db))

        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO files (path) VALUES ('a.py')")
        conn.commit()
        conn.close()

        sylva.init_db(str(db))  # re-init must not wipe data

        conn = sqlite3.connect(str(db))
        count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        conn.close()
        assert count == 1


class TestNegative:
    def test_parent_is_a_file(self, tmp_path):
        # A path whose parent component is an existing *file* cannot have its
        # directory created — must raise, not silently corrupt.
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file, not a directory")
        bad = blocker / "sub" / "sylva.db"
        with pytest.raises(Exception) as exc:
            sylva.init_db(str(bad))
        # Error message names the offending path (clear, actionable).
        assert str(bad) in str(exc.value)


class TestErrorControl:
    def test_not_a_database_file_reports_clearly(self, tmp_path):
        # Point init_db at an existing file that is not a valid SQLite db.
        # Opening/migrating must surface a meaningful error, not crash.
        junk = tmp_path / "sylva.db"
        junk.write_bytes(b"this is definitely not a sqlite database")
        with pytest.raises(Exception) as exc:
            sylva.init_db(str(junk))
        assert str(junk) in str(exc.value)
