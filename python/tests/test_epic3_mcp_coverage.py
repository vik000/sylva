"""Feature 3.5 — MCP tools for coverage queries.

Three new JSON-RPC methods on the Feature 1.6 `handle_request` router:
  * get_coverage {name}        -> symbol's coverage_pct, or null
  * get_uncovered_paths {}     -> [{name, file, line}] for 0%-covered symbols
  * get_test_coverage {name}   -> test functions that exercise the symbol
"""

import json
import sqlite3

import pytest

import sylva


def _build(tmp_path):
    """A source file with symbols, coverage set, and a test_covers edge."""
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    sylva.write_symbols(
        str(db), "src/foo.py",
        [
            {"name": "greet", "kind": "function", "line": 1, "line_end": 3},
            {"name": "bar", "kind": "function", "line": 5, "line_end": 8},
            {"name": "untested", "kind": "function", "line": 10, "line_end": 12},
        ],
    )
    sylva.write_symbols(
        str(db), "tests/test_foo.py",
        [{"name": "test_greet", "kind": "function", "line": 1, "line_end": 4}],
    )

    conn = sqlite3.connect(str(db))
    try:
        # greet 82.5%, bar 0% (untested-known), untested stays NULL (no data).
        conn.execute("UPDATE symbols SET coverage_pct = 82.5 WHERE name = 'greet'")
        conn.execute("UPDATE symbols SET coverage_pct = 0.0 WHERE name = 'bar'")
        # test_greet -> greet edge
        ids = dict(conn.execute("SELECT name, id FROM symbols").fetchall())
        conn.execute(
            "INSERT INTO edges (src_id, dst_id, kind) VALUES (?, ?, 'test_covers')",
            (ids["test_greet"], ids["greet"]),
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _call(db, method, params=None, req_id=1):
    req = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
    return json.loads(sylva.handle_request(str(db), json.dumps(req)))


class TestGetCoverage:
    def test_known_symbol_returns_pct(self, tmp_path):
        db = _build(tmp_path)
        resp = _call(db, "get_coverage", {"name": "greet"})
        assert "error" not in resp
        assert resp["result"] == 82.5

    def test_unknown_symbol_returns_null(self, tmp_path):
        db = _build(tmp_path)
        resp = _call(db, "get_coverage", {"name": "ghost"})
        assert resp["result"] is None  # null, not an error

    def test_no_data_symbol_returns_null(self, tmp_path):
        db = _build(tmp_path)
        resp = _call(db, "get_coverage", {"name": "untested"})
        assert resp["result"] is None  # NULL coverage -> null


class TestGetUncoveredPaths:
    def test_lists_zero_percent_symbols(self, tmp_path):
        db = _build(tmp_path)
        resp = _call(db, "get_uncovered_paths")
        results = resp["result"]
        assert len(results) == 1
        r = results[0]
        assert r == {"name": "bar", "file": "src/foo.py", "line": 5}

    def test_excludes_null_coverage(self, tmp_path):
        db = _build(tmp_path)
        names = {r["name"] for r in _call(db, "get_uncovered_paths")["result"]}
        assert "untested" not in names  # NULL is not "uncovered"

    def test_empty_when_none_zero(self, tmp_path):
        db = tmp_path / "sylva.db"
        sylva.init_db(str(db))
        sylva.write_symbols(
            str(db), "src/foo.py",
            [{"name": "greet", "kind": "function", "line": 1, "line_end": 3}],
        )
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE symbols SET coverage_pct = 100.0 WHERE name = 'greet'")
        conn.commit()
        conn.close()
        assert _call(db, "get_uncovered_paths")["result"] == []


class TestGetTestCoverage:
    def test_returns_covering_tests(self, tmp_path):
        db = _build(tmp_path)
        resp = _call(db, "get_test_coverage", {"name": "greet"})
        assert resp["result"] == ["test_greet"]

    def test_symbol_with_no_tests_returns_empty(self, tmp_path):
        db = _build(tmp_path)
        assert _call(db, "get_test_coverage", {"name": "bar"})["result"] == []

    def test_unknown_symbol_returns_empty(self, tmp_path):
        db = _build(tmp_path)
        assert _call(db, "get_test_coverage", {"name": "ghost"})["result"] == []


class TestErrorControl:
    def test_db_unavailable_returns_error_response(self, tmp_path):
        missing = tmp_path / "nope.db"
        for method in ("get_coverage", "get_test_coverage"):
            resp = _call(missing, method, {"name": "x"})
            assert resp["error"]["code"] == -32001
            assert "not found" in resp["error"]["message"].lower()
        resp = _call(missing, "get_uncovered_paths")
        assert resp["error"]["code"] == -32001

    def test_missing_name_param_returns_invalid_params(self, tmp_path):
        db = _build(tmp_path)
        resp = _call(db, "get_coverage", {})
        assert resp["error"]["code"] == -32602


class TestNoRegression:
    def test_original_tools_still_work(self, tmp_path):
        # The 1.6 tools must be unaffected by the router refactor.
        db = _build(tmp_path)
        resp = _call(db, "search_symbol", {"name": "greet"})
        assert resp["result"][0]["name"] == "greet"
        resp = _call(db, "no_such_tool", {"name": "x"})
        assert resp["error"]["code"] == -32601
