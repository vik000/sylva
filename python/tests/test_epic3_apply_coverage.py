"""Feature 3.2 — Correlate coverage to symbols.

`sylva.apply_coverage(db_path, coverage)` matches report files to graph files by
path suffix (issue #33), computes each symbol's coverage over its
line_start..line_end span (Feature 7.1), and writes `symbols.coverage_pct` —
distinguishing NULL (no reported lines in span) from 0.0 (reported, none hit).
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


def _pct(db, name):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT coverage_pct FROM symbols WHERE name = ?", (name,)
        ).fetchone()[0]
    finally:
        conn.close()


class TestGeneral:
    def test_partial_coverage_percentage(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "foo.py", [_sym("f", "function", 1, 4)])
        # 3 of 4 reported lines covered -> 75%.
        cov = {"foo.py": {1: True, 2: True, 3: True, 4: False}}
        n = sylva.apply_coverage(str(db), cov)
        assert n == 1
        assert _pct(db, "f") == 75.0

    def test_fully_covered(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "foo.py", [_sym("f", "function", 1, 2)])
        sylva.apply_coverage(str(db), {"foo.py": {1: True, 2: True}})
        assert _pct(db, "f") == 100.0

    def test_uncovered_symbol_is_zero(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "foo.py", [_sym("f", "function", 1, 2)])
        sylva.apply_coverage(str(db), {"foo.py": {1: False, 2: False}})
        assert _pct(db, "f") == 0.0

    def test_only_lines_in_span_count(self, tmp_path):
        db = _init(tmp_path)
        # Two functions in one file; coverage must attach per-span.
        sylva.write_symbols(
            str(db), "foo.py",
            [_sym("a", "function", 1, 2), _sym("b", "function", 4, 5)],
        )
        cov = {"foo.py": {1: True, 2: True, 4: False, 5: True}}
        sylva.apply_coverage(str(db), cov)
        assert _pct(db, "a") == 100.0
        assert _pct(db, "b") == 50.0


class TestEdge:
    def test_no_reported_lines_in_span_is_null(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "foo.py", [_sym("f", "function", 10, 12)])
        # Report has lines, but none within the symbol's span -> NULL, not 0.
        sylva.apply_coverage(str(db), {"foo.py": {1: True, 2: False}})
        assert _pct(db, "f") is None

    def test_null_distinct_from_zero(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(
            str(db), "foo.py",
            [_sym("hit0", "function", 1, 1), _sym("blank", "function", 50, 51)],
        )
        cov = {"foo.py": {1: False}}  # line 1 reported-uncovered; 50-51 absent
        n = sylva.apply_coverage(str(db), cov)
        assert _pct(db, "hit0") == 0.0    # reported, uncovered
        assert _pct(db, "blank") is None  # no reported lines in span
        assert n == 1                     # only hit0 got a numeric pct

    def test_file_absent_from_report_untouched(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "foo.py", [_sym("f", "function", 1, 2)])
        sylva.apply_coverage(str(db), {"other.py": {1: True}})
        assert _pct(db, "f") is None  # never touched


class TestPathReconciliation:
    def test_relative_report_matches_absolute_graph(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "/repo/src/foo.py", [_sym("f", "function", 1, 2)])
        # Report uses a relative path; must still reconcile by suffix.
        sylva.apply_coverage(str(db), {"src/foo.py": {1: True, 2: True}})
        assert _pct(db, "f") == 100.0

    def test_differing_roots(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "/home/ci/build/pkg/mod.py", [_sym("f", "function", 1, 1)])
        sylva.apply_coverage(str(db), {"pkg/mod.py": {1: True}})
        assert _pct(db, "f") == 100.0

    def test_ambiguous_match_skipped(self, tmp_path):
        db = _init(tmp_path)
        # Two graph files share the suffix a/util.py; neither is an exact match
        # for the report path -> ambiguous -> skipped, nothing updated.
        sylva.write_symbols(str(db), "/x/a/util.py", [_sym("ax", "function", 1, 1)])
        sylva.write_symbols(str(db), "/y/a/util.py", [_sym("ay", "function", 1, 1)])
        n = sylva.apply_coverage(str(db), {"a/util.py": {1: True}})
        assert n == 0
        assert _pct(db, "ax") is None
        assert _pct(db, "ay") is None

    def test_exact_match_wins_over_suffix(self, tmp_path):
        db = _init(tmp_path)
        # An exact path plus a deeper suffix candidate: exact should be chosen.
        sylva.write_symbols(str(db), "src/foo.py", [_sym("exact", "function", 1, 1)])
        sylva.write_symbols(str(db), "/vendor/src/foo.py", [_sym("deep", "function", 1, 1)])
        sylva.apply_coverage(str(db), {"src/foo.py": {1: True}})
        assert _pct(db, "exact") == 100.0
        assert _pct(db, "deep") is None  # not the chosen match


class TestNegativeAndErrorControl:
    def test_unknown_file_skipped_without_error(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "foo.py", [_sym("f", "function", 1, 1)])
        # Coverage references a file not in the graph -> skipped, no raise.
        n = sylva.apply_coverage(str(db), {"nowhere/ghost.py": {1: True}})
        assert n == 0

    def test_partial_batch_applies_matched_files(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "good.py", [_sym("g", "function", 1, 2)])
        # One matched file + one unmatched: matched still applied, no abort.
        cov = {"good.py": {1: True, 2: True}, "unmatched/x.py": {1: True}}
        n = sylva.apply_coverage(str(db), cov)
        assert n == 1
        assert _pct(db, "g") == 100.0

    def test_empty_coverage_is_noop(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "foo.py", [_sym("f", "function", 1, 1)])
        assert sylva.apply_coverage(str(db), {}) == 0
        assert _pct(db, "f") is None
