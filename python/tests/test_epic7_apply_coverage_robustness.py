"""Feature 7.6 — apply_coverage robustness (issue #34).

(A) Two report paths reconciling to one graph file are merged (OR),
    deterministically, instead of a nondeterministic last-wins.
(B) By default apply_coverage resets stale coverage first, so a file dropped
    from a later report no longer keeps its old percentage; reset=False applies
    additively.
"""

import sqlite3

import pytest

import sylva


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _sym(name, line, line_end):
    return {"name": name, "kind": "function", "line": line, "line_end": line_end}


def _pct(db, name):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT coverage_pct FROM symbols WHERE name = ?", (name,)
        ).fetchone()[0]
    finally:
        conn.close()


class TestMergeSameFile:
    def test_two_report_paths_merge_deterministically(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "/repo/foo.py", [_sym("f", 1, 2)])
        # Both keys reconcile to /repo/foo.py: relative (suffix) and absolute.
        # Line 1 covered from one, line 2 uncovered from the other.
        cov = {"foo.py": {1: True}, "/repo/foo.py": {2: False}}
        sylva.apply_coverage(str(db), cov)
        # Merged span 1-2 -> 1 of 2 covered = 50%. (last-wins would give 100 or 0)
        assert _pct(db, "f") == 50.0


class TestReset:
    def test_stale_coverage_reset_by_default(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "/a/foo.py", [_sym("f", 1, 1)])
        sylva.write_symbols(str(db), "/a/bar.py", [_sym("g", 1, 1)])

        sylva.apply_coverage(str(db), {"foo.py": {1: True}, "bar.py": {1: True}})
        assert _pct(db, "f") == 100.0 and _pct(db, "g") == 100.0

        # Re-apply a report that no longer includes bar.py -> g is reset to NULL.
        sylva.apply_coverage(str(db), {"foo.py": {1: False}})
        assert _pct(db, "f") == 0.0
        assert _pct(db, "g") is None  # stale value cleared, not left at 100

    def test_reset_false_is_additive(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "/a/foo.py", [_sym("f", 1, 1)])
        sylva.write_symbols(str(db), "/a/bar.py", [_sym("g", 1, 1)])

        sylva.apply_coverage(str(db), {"foo.py": {1: True}})
        assert _pct(db, "f") == 100.0

        # Additive: applying bar's coverage must not wipe foo's.
        sylva.apply_coverage(str(db), {"bar.py": {1: True}}, reset=False)
        assert _pct(db, "g") == 100.0
        assert _pct(db, "f") == 100.0  # retained

    def test_default_reset_is_idempotent(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "/a/foo.py", [_sym("f", 1, 1)])
        cov = {"foo.py": {1: True}}
        sylva.apply_coverage(str(db), cov)
        sylva.apply_coverage(str(db), cov)  # re-apply
        assert _pct(db, "f") == 100.0
