"""Feature 7.2 — Checkpoint the exact indexed hash (issue #29).

`file_hash` lets a caller hash a file once and thread that exact digest through
`file_needs_reindex` and `mark_indexed`, so the decision and the checkpoint
agree on one value even if the file changes underneath — closing the race where
`mark_indexed` re-hashed and stored the hash of content that was never indexed.
"""

import pytest

import sylva


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


class TestFileHash:
    def test_stable_and_hex(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        h1 = sylva.file_hash(str(f))
        assert h1 == sylva.file_hash(str(f))
        assert len(h1) == 64

    def test_changes_with_content(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        h1 = sylva.file_hash(str(f))
        f.write_text("x = 2\n")
        assert sylva.file_hash(str(f)) != h1

    def test_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sylva.file_hash(str(tmp_path / "nope.py"))


class TestThreadedHash:
    def test_provided_hash_not_reread(self, tmp_path):
        # A supplied hash is used directly — no file read (so a deleted file with
        # a matching supplied hash is 'unchanged', not FileNotFoundError).
        db = _init(tmp_path)
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        h = sylva.file_hash(str(f))
        sylva.mark_indexed(str(db), str(f), h)
        f.unlink()
        assert sylva.file_needs_reindex(str(db), str(f), h) is False

    def test_threaded_hash_closes_race(self, tmp_path):
        db = _init(tmp_path)
        f = tmp_path / "a.py"
        f.write_text("VERSION A\n")

        # Decision uses the hash of content A.
        h_a = sylva.file_hash(str(f))
        assert sylva.file_needs_reindex(str(db), str(f), h_a) is True

        # ... we index content A ... but the file changes to B before checkpoint.
        f.write_text("VERSION B\n")

        # Checkpoint stores the hash of what we actually indexed (A), not B.
        sylva.mark_indexed(str(db), str(f), h_a)

        # Next run: file is B, stored is A -> reindex needed. The B content is
        # NOT silently skipped (which is what re-hashing at checkpoint caused).
        assert sylva.file_needs_reindex(str(db), str(f)) is True

    def test_matching_threaded_hash_marks_done(self, tmp_path):
        # The normal race-free loop: same hash for decision and checkpoint.
        db = _init(tmp_path)
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        h = sylva.file_hash(str(f))
        assert sylva.file_needs_reindex(str(db), str(f), h) is True
        sylva.mark_indexed(str(db), str(f), h)
        # Unchanged file (same hash) -> no reindex.
        assert sylva.file_needs_reindex(str(db), str(f), h) is False


class TestBackwardCompatible:
    def test_no_hash_arg_still_works(self, tmp_path):
        db = _init(tmp_path)
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        assert sylva.file_needs_reindex(str(db), str(f)) is True
        sylva.mark_indexed(str(db), str(f))  # re-hashes current contents
        assert sylva.file_needs_reindex(str(db), str(f)) is False
