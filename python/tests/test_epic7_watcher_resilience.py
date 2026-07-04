"""Feature 7.3 — Watcher loop resilience to mid-loop errors (issue #30).

The watcher's "errors logged, loop recovers" guarantee was structural
(`process_path` catches and continues) but untested end-to-end. Here we induce a
real per-event failure — an unreadable .py file, which makes reindex fail — and
prove a subsequent valid event is still processed (the loop survived).
"""

import os
import sqlite3
import sys
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


def _wait_until(pred, timeout=10.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


class TestReindexPathErrors:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses perms"
    )
    def test_reindex_path_raises_on_unreadable(self, tmp_path):
        # The pure function surfaces the error (which the loop then swallows).
        db = _init(tmp_path)
        f = tmp_path / "bad.py"
        f.write_text("def x():\n    return 1\n")
        os.chmod(f, 0)
        try:
            with pytest.raises(Exception):
                sylva.reindex_path(str(db), str(f))
        finally:
            os.chmod(f, 0o644)


class TestWatcherSurvivesError:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses perms"
    )
    def test_loop_continues_after_failed_event(self, tmp_path):
        db = _init(tmp_path)
        watchdir = tmp_path / "src"
        watchdir.mkdir()
        handle = sylva.start_watcher(str(watchdir), str(db), 100)
        bad = watchdir / "bad.py"
        try:
            # Create an unreadable .py. The debounce means the watcher reads it
            # after the chmod, so reindex fails inside the loop.
            bad.write_text("def poison():\n    return 1\n")
            os.chmod(bad, 0)
            time.sleep(0.4)  # let the (failing) event be processed

            # A subsequent valid file must still be indexed -> the loop lived.
            (watchdir / "good.py").write_text("def good():\n    return 1\n")
            assert _wait_until(lambda: "good" in _symbol_names(db))

            # And the poison file's symbol was not indexed (its event errored).
            assert "poison" not in _symbol_names(db)
        finally:
            os.chmod(bad, 0o644)  # restore so tmp cleanup succeeds
            handle.stop()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses perms"
    )
    def test_watcher_still_stops_cleanly_after_error(self, tmp_path):
        db = _init(tmp_path)
        watchdir = tmp_path / "src"
        watchdir.mkdir()
        handle = sylva.start_watcher(str(watchdir), str(db), 100)
        bad = watchdir / "bad.py"
        try:
            bad.write_text("def x():\n    return 1\n")
            os.chmod(bad, 0)
            time.sleep(0.4)
        finally:
            os.chmod(bad, 0o644)
        handle.stop()  # must not hang or raise despite the earlier error
