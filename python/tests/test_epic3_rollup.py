"""Feature 3.4 — Module-level coverage rollup.

`sylva.get_module_coverage(db_path) -> {file_path: pct_or_None}`. Each file's
percentage is the line-span-weighted average of its symbols' coverage_pct, with
NULL-coverage symbols excluded; a file with no coverage data maps to None.
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


def _set_cov(db, name, pct):
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE symbols SET coverage_pct = ? WHERE name = ?", (pct, name))
        conn.commit()
    finally:
        conn.close()


class TestGeneral:
    def test_line_span_weighted_average(self, tmp_path):
        db = _init(tmp_path)
        # f1: 1 line @ 100%, f2: 4 lines @ 50% -> (100*1 + 50*4)/5 = 60.
        sylva.write_symbols(str(db), "a.py", [_sym("f1", 1, 1), _sym("f2", 2, 5)])
        _set_cov(db, "f1", 100.0)
        _set_cov(db, "f2", 50.0)
        assert sylva.get_module_coverage(str(db))["a.py"] == 60.0

    def test_equal_spans(self, tmp_path):
        db = _init(tmp_path)
        # Same-size symbols -> simple mean of 100 and 0 = 50.
        sylva.write_symbols(str(db), "a.py", [_sym("x", 1, 2), _sym("y", 4, 5)])
        _set_cov(db, "x", 100.0)
        _set_cov(db, "y", 0.0)
        assert sylva.get_module_coverage(str(db))["a.py"] == 50.0

    def test_multiple_files_keyed_by_path(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "a.py", [_sym("f", 1, 1)])
        sylva.write_symbols(str(db), "b.py", [_sym("g", 1, 1)])
        _set_cov(db, "f", 90.0)
        _set_cov(db, "g", 30.0)
        result = sylva.get_module_coverage(str(db))
        assert result == {"a.py": 90.0, "b.py": 30.0}

    def test_value_type_is_float_or_none(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "a.py", [_sym("f", 1, 1)])
        _set_cov(db, "f", 75.0)
        assert isinstance(sylva.get_module_coverage(str(db))["a.py"], float)


class TestEdge:
    def test_all_null_file_is_none(self, tmp_path):
        db = _init(tmp_path)
        # Symbols exist but none have coverage data -> None, not 0.0.
        sylva.write_symbols(str(db), "a.py", [_sym("f", 1, 3)])
        assert sylva.get_module_coverage(str(db))["a.py"] is None

    def test_file_with_no_symbols_is_none(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "empty.py", [])  # file record, no symbols
        assert sylva.get_module_coverage(str(db))["empty.py"] is None

    def test_empty_db_returns_empty_dict(self, tmp_path):
        db = _init(tmp_path)
        assert sylva.get_module_coverage(str(db)) == {}


class TestErrorControl:
    def test_null_symbols_excluded_from_average(self, tmp_path):
        db = _init(tmp_path)
        # hit: 1 line @ 80%; nocov: 10 lines, NULL coverage.
        # If nocov counted as 0 -> (80*1)/(11) ~= 7.3; excluded -> 80.0.
        sylva.write_symbols(str(db), "a.py", [_sym("hit", 1, 1), _sym("nocov", 2, 11)])
        _set_cov(db, "hit", 80.0)  # nocov stays NULL
        assert sylva.get_module_coverage(str(db))["a.py"] == 80.0

    def test_partial_null_still_computes(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "a.py", [_sym("a", 1, 1), _sym("b", 2, 2), _sym("c", 3, 3)])
        _set_cov(db, "a", 100.0)
        _set_cov(db, "c", 0.0)  # b stays NULL -> excluded
        assert sylva.get_module_coverage(str(db))["a.py"] == 50.0


class TestNegative:
    def test_db_not_found_raises(self, tmp_path):
        with pytest.raises(Exception):
            sylva.get_module_coverage(str(tmp_path / "missing.db"))
