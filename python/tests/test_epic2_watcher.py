"""Feature 2.2 — File watcher.

Two layers:
  * Fast, deterministic unit tests on the pure functions `reindex_path` and
    `handle_delete` (the watcher's core logic).
  * Slower integration tests that start a real watcher and poll the DB until the
    expected state appears (bounded timeout, never a fixed sleep), covering the
    live create / modify / delete paths.
"""

import os
import sqlite3
import time

import pytest

import sylva


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _symbol_names(db):
    conn = sqlite3.connect(str(db))
    try:
        return {r[0] for r in conn.execute("SELECT name FROM symbols").fetchall()}
    finally:
        conn.close()


def _file_count(db, path):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT COUNT(*) FROM files WHERE path = ?", (path,)).fetchone()[0]
    finally:
        conn.close()


def _wait_until(predicate, timeout=10.0, interval=0.05):
    """Poll until predicate() is truthy or timeout elapses. Returns final value."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# --------------------------------------------------------------------------- #
# Unit tests — the pure reindex/delete logic
# --------------------------------------------------------------------------- #

class TestReindexPath:
    def test_indexes_new_python_file(self, tmp_path):
        db = _init(tmp_path)
        f = tmp_path / "m.py"
        f.write_text("def hello():\n    pass\n")
        assert sylva.reindex_path(str(db), str(f)) is True
        assert "hello" in _symbol_names(db)

    def test_unchanged_file_is_skipped(self, tmp_path):
        db = _init(tmp_path)
        f = tmp_path / "m.py"
        f.write_text("def hello():\n    pass\n")
        assert sylva.reindex_path(str(db), str(f)) is True
        # Second call: unchanged -> skipped (False), courtesy of hash tracking.
        assert sylva.reindex_path(str(db), str(f)) is False

    def test_modified_file_is_reindexed(self, tmp_path):
        db = _init(tmp_path)
        f = tmp_path / "m.py"
        f.write_text("def a():\n    pass\n")
        sylva.reindex_path(str(db), str(f))
        f.write_text("def a():\n    pass\n\ndef b():\n    pass\n")
        assert sylva.reindex_path(str(db), str(f)) is True
        assert {"a", "b"}.issubset(_symbol_names(db))

    def test_non_python_file_ignored(self, tmp_path):
        db = _init(tmp_path)
        f = tmp_path / "notes.txt"
        f.write_text("hello")
        assert sylva.reindex_path(str(db), str(f)) is False
        assert _symbol_names(db) == set()

    def test_missing_python_file_raises(self, tmp_path):
        db = _init(tmp_path)
        with pytest.raises(FileNotFoundError):
            sylva.reindex_path(str(db), str(tmp_path / "gone.py"))


class TestHandleDelete:
    def test_delete_removes_file_and_symbols(self, tmp_path):
        db = _init(tmp_path)
        f = tmp_path / "m.py"
        f.write_text("def hello():\n    pass\n")
        sylva.reindex_path(str(db), str(f))
        assert "hello" in _symbol_names(db)

        assert sylva.handle_delete(str(db), str(f)) is True
        # File row gone, and symbols cascaded away.
        assert _file_count(db, str(f)) == 0
        assert _symbol_names(db) == set()

    def test_delete_unknown_path_is_noop(self, tmp_path):
        db = _init(tmp_path)
        assert sylva.handle_delete(str(db), str(tmp_path / "never.py")) is False


# --------------------------------------------------------------------------- #
# Integration tests — the live watcher
# --------------------------------------------------------------------------- #

class TestWatcherNegative:
    def test_nonexistent_root_raises(self, tmp_path):
        db = _init(tmp_path)
        with pytest.raises(FileNotFoundError):
            sylva.start_watcher(str(tmp_path / "no_such_dir"), str(db))


class TestWatcherIntegration:
    def test_new_file_is_indexed(self, tmp_path):
        db = _init(tmp_path)
        watchdir = tmp_path / "src"
        watchdir.mkdir()
        handle = sylva.start_watcher(str(watchdir), str(db), 100)
        try:
            (watchdir / "new.py").write_text("def created():\n    pass\n")
            assert _wait_until(lambda: "created" in _symbol_names(db))
        finally:
            handle.stop()

    def test_modify_triggers_reindex(self, tmp_path):
        db = _init(tmp_path)
        watchdir = tmp_path / "src"
        watchdir.mkdir()
        handle = sylva.start_watcher(str(watchdir), str(db), 100)
        try:
            f = watchdir / "mod.py"
            f.write_text("def first():\n    pass\n")
            assert _wait_until(lambda: "first" in _symbol_names(db))
            f.write_text("def first():\n    pass\n\ndef second():\n    pass\n")
            assert _wait_until(lambda: "second" in _symbol_names(db))
        finally:
            handle.stop()

    def test_delete_marks_removed(self, tmp_path):
        db = _init(tmp_path)
        watchdir = tmp_path / "src"
        watchdir.mkdir()
        handle = sylva.start_watcher(str(watchdir), str(db), 100)
        try:
            f = watchdir / "doomed.py"
            f.write_text("def doomed():\n    pass\n")
            assert _wait_until(lambda: "doomed" in _symbol_names(db))
            os.remove(f)
            assert _wait_until(lambda: "doomed" not in _symbol_names(db))
        finally:
            handle.stop()

    def test_stop_is_idempotent(self, tmp_path):
        db = _init(tmp_path)
        watchdir = tmp_path / "src"
        watchdir.mkdir()
        handle = sylva.start_watcher(str(watchdir), str(db), 100)
        handle.stop()
        handle.stop()  # second stop must not raise
