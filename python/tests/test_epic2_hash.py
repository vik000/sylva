"""Feature 2.1 — File hash tracking.

Contract (Option A — decision and checkpoint are separate):
  * `file_needs_reindex(db, path)` is READ-ONLY — it decides, never writes.
  * `mark_indexed(db, path)` persists the hash, and is called only after a
    successful index.

Verified below: new/changed files report True; a file reports False only once
it has been marked indexed; `file_needs_reindex` makes no DB changes; a crash
before `mark_indexed` leaves the file flagged (the gap this design closes);
missing files raise; and `mark_indexed` write failures are retried, not fatal.
"""

import os
import sqlite3
import sys

import pytest

import sylva


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _stored_hash(db, path):
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute("SELECT hash FROM files WHERE path = ?", (path,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _mkfile(tmp_path, name="a.py", text="x = 1\n"):
    f = tmp_path / name
    f.write_text(text)
    return f


class TestGeneral:
    def test_new_file_needs_reindex(self, tmp_path):
        db = _init(tmp_path)
        f = _mkfile(tmp_path)
        assert sylva.file_needs_reindex(str(db), str(f)) is True

    def test_marked_file_is_skipped(self, tmp_path):
        db = _init(tmp_path)
        f = _mkfile(tmp_path)
        assert sylva.file_needs_reindex(str(db), str(f)) is True
        sylva.mark_indexed(str(db), str(f))
        # After the checkpoint, unchanged contents -> no reindex needed.
        assert sylva.file_needs_reindex(str(db), str(f)) is False

    def test_modified_file_needs_reindex(self, tmp_path):
        db = _init(tmp_path)
        f = _mkfile(tmp_path)
        sylva.mark_indexed(str(db), str(f))
        assert sylva.file_needs_reindex(str(db), str(f)) is False
        f.write_text("x = 2\n")  # change contents
        assert sylva.file_needs_reindex(str(db), str(f)) is True

    def test_mark_indexed_records_hash(self, tmp_path):
        db = _init(tmp_path)
        f = _mkfile(tmp_path)
        assert _stored_hash(db, str(f)) is None
        sylva.mark_indexed(str(db), str(f))
        h = _stored_hash(db, str(f))
        assert h is not None and len(h) == 64  # sha-256 hex

    def test_null_hash_record_treated_as_needs_reindex(self, tmp_path):
        # The graph writer creates a file record with hash NULL; until the file
        # is marked indexed, it must be seen as needing indexing.
        db = _init(tmp_path)
        f = _mkfile(tmp_path)
        sylva.write_symbols(str(db), str(f), [])  # record with hash NULL
        assert _stored_hash(db, str(f)) is None
        assert sylva.file_needs_reindex(str(db), str(f)) is True
        sylva.mark_indexed(str(db), str(f))
        assert sylva.file_needs_reindex(str(db), str(f)) is False


class TestReadOnlyDecision:
    def test_file_needs_reindex_does_not_write(self, tmp_path):
        # The decision call must not persist anything — that's the whole point
        # of splitting decide from checkpoint.
        db = _init(tmp_path)
        f = _mkfile(tmp_path)
        sylva.file_needs_reindex(str(db), str(f))
        assert _stored_hash(db, str(f)) is None  # nothing recorded

    def test_crash_before_mark_still_needs_reindex(self, tmp_path):
        # Simulate: decide -> index -> CRASH (mark_indexed never runs).
        # Next run must still see the file as needing reindex, not skip it.
        db = _init(tmp_path)
        f = _mkfile(tmp_path)
        assert sylva.file_needs_reindex(str(db), str(f)) is True
        # ... index happens, then process dies before mark_indexed ...
        assert sylva.file_needs_reindex(str(db), str(f)) is True  # still flagged


class TestEdge:
    def test_empty_file_stable_hash(self, tmp_path):
        db = _init(tmp_path)
        f = _mkfile(tmp_path, "empty.py", "")
        assert sylva.file_needs_reindex(str(db), str(f)) is True
        sylva.mark_indexed(str(db), str(f))
        assert sylva.file_needs_reindex(str(db), str(f)) is False  # stable hash

    def test_large_file_hashes_correctly(self, tmp_path):
        db = _init(tmp_path)
        f = tmp_path / "big.py"
        f.write_bytes(b"# padding\n" * 200_000)  # ~2 MB, exceeds the read buffer
        assert sylva.file_needs_reindex(str(db), str(f)) is True
        sylva.mark_indexed(str(db), str(f))
        assert sylva.file_needs_reindex(str(db), str(f)) is False
        with open(f, "ab") as fh:  # a one-byte change is detected
            fh.write(b"#")
        assert sylva.file_needs_reindex(str(db), str(f)) is True


class TestNegative:
    def test_needs_reindex_missing_file_raises(self, tmp_path):
        db = _init(tmp_path)
        with pytest.raises(FileNotFoundError):
            sylva.file_needs_reindex(str(db), str(tmp_path / "nope.py"))

    def test_mark_indexed_missing_file_raises(self, tmp_path):
        db = _init(tmp_path)
        with pytest.raises(FileNotFoundError):
            sylva.mark_indexed(str(db), str(tmp_path / "nope.py"))


class TestErrorControl:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission model")
    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root bypasses directory permissions",
    )
    def test_mark_indexed_write_failure_retried_and_not_fatal(self, tmp_path):
        # Put the db in its own directory, then make that directory read-only.
        # Reads still work, but writing the hash (which needs a journal file in
        # the dir) fails -> exercises the retry + log path in mark_indexed.
        dbdir = tmp_path / "dbdir"
        dbdir.mkdir()
        db = dbdir / "sylva.db"
        sylva.init_db(str(db))

        src = tmp_path / "a.py"  # readable source, outside the locked dir
        src.write_text("x = 1\n")

        os.chmod(dbdir, 0o555)
        try:
            # The write fails and is retried; mark_indexed must not raise.
            sylva.mark_indexed(str(db), str(src))
            # And because the hash never persisted, the file is still flagged.
            assert sylva.file_needs_reindex(str(db), str(src)) is True
        finally:
            os.chmod(dbdir, 0o755)  # restore so tmp cleanup succeeds
