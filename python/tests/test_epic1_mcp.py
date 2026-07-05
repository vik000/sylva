"""Feature 1.6 — MCP server: 3 core tools.

Exercises the JSON-RPC handler (`sylva.handle_request`) for `search_symbol`,
`get_callers`, and `get_dependencies`, plus the `sylva serve` CLI startup path.

Note: the indexer does not yet populate the `edges` table (issue #27), so the
caller/dependency tests seed edge rows directly into a known graph to prove the
query logic. On a freshly-indexed repo these tools return an empty list.
"""

import json
import sqlite3

import pytest

import sylva
import sylva.__main__ as cli


def _build_graph(tmp_path):
    """Init a db, write a small symbol set, and seed some call/import edges."""
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))

    sylva.write_symbols(
        str(db),
        "app.py",
        [
            {"name": "main", "kind": "function", "line": 10, "docstring": "entry"},
            {"name": "helper", "kind": "function", "line": 20, "docstring": None},
            {"name": "os", "kind": "import", "line": 1, "docstring": None},
        ],
    )

    conn = sqlite3.connect(str(db))
    try:
        ids = dict(conn.execute("SELECT name, id FROM symbols").fetchall())
        # main -> helper (call), main -> os (import)
        conn.execute(
            "INSERT INTO edges (src_id, dst_id, kind) VALUES (?, ?, 'calls')",
            (ids["main"], ids["helper"]),
        )
        conn.execute(
            "INSERT INTO edges (src_id, dst_id, kind) VALUES (?, ?, 'imports')",
            (ids["main"], ids["os"]),
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _call(db, method, arg_key="name", arg="main", req_id=1):
    req = json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "method": method, "params": {arg_key: arg}}
    )
    return json.loads(sylva.handle_request(str(db), req))


class TestGeneral:
    def test_search_symbol_returns_match(self, tmp_path):
        db = _build_graph(tmp_path)
        resp = _call(db, "search_symbol", "name", "main")
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        assert "error" not in resp
        results = resp["result"]
        assert len(results) == 1
        r = results[0]
        assert r["name"] == "main"
        assert r["kind"] == "function"
        assert r["line"] == 10
        assert r["file"] == "app.py"
        assert r["docstring"] == "entry"

    def test_get_callers_returns_caller(self, tmp_path):
        db = _build_graph(tmp_path)
        # helper is called by main
        resp = _call(db, "get_callers", "symbol", "helper")
        names = {r["name"] for r in resp["result"]}
        assert names == {"main"}

    def test_get_dependencies_returns_targets(self, tmp_path):
        db = _build_graph(tmp_path)
        # main depends on helper (call) and os (import)
        resp = _call(db, "get_dependencies", "symbol", "main")
        names = {r["name"] for r in resp["result"]}
        assert names == {"helper", "os"}

    def test_accepts_either_name_or_symbol_key(self, tmp_path):
        db = _build_graph(tmp_path)
        by_name = _call(db, "get_callers", "name", "helper")["result"]
        by_symbol = _call(db, "get_callers", "symbol", "helper")["result"]
        assert by_name == by_symbol


class TestEdge:
    def test_unknown_symbol_returns_empty_not_error(self, tmp_path):
        db = _build_graph(tmp_path)
        for method in ("search_symbol", "get_callers", "get_dependencies"):
            resp = _call(db, method, "name", "does_not_exist")
            assert "error" not in resp
            assert resp["result"] == []

    def test_symbol_with_no_callers(self, tmp_path):
        db = _build_graph(tmp_path)
        # main is called by nobody in the seeded graph.
        resp = _call(db, "get_callers", "symbol", "main")
        assert resp["result"] == []


class TestNegative:
    def test_malformed_json_returns_parse_error(self, tmp_path):
        db = _build_graph(tmp_path)
        resp = json.loads(sylva.handle_request(str(db), "{ this is not json "))
        assert resp["error"]["code"] == -32700
        assert resp["id"] is None

    def test_unknown_method_returns_method_not_found(self, tmp_path):
        db = _build_graph(tmp_path)
        req = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "no_such_tool", "params": {}})
        resp = json.loads(sylva.handle_request(str(db), req))
        assert resp["error"]["code"] == -32601
        assert resp["id"] == 7

    def test_missing_params_returns_invalid_params(self, tmp_path):
        db = _build_graph(tmp_path)
        req = json.dumps({"jsonrpc": "2.0", "id": 8, "method": "search_symbol", "params": {}})
        resp = json.loads(sylva.handle_request(str(db), req))
        assert resp["error"]["code"] == -32602


class TestErrorControl:
    def test_missing_db_returns_error_response(self, tmp_path):
        missing = tmp_path / "nope.db"
        req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "search_symbol", "params": {"name": "x"}})
        resp = json.loads(sylva.handle_request(str(missing), req))
        assert resp["error"]["code"] == -32001
        assert "not found" in resp["error"]["message"].lower()

    def test_cli_serve_missing_db_exits_nonzero(self, tmp_path, capsys):
        # `sylva serve --db <missing>` must fail clearly at startup.
        rc = cli.main(["serve", "--db", str(tmp_path / "absent.db")])
        assert rc == 1
        err = capsys.readouterr().err
        assert "database not found" in err

    def test_bare_invocation_shows_help(self, tmp_path, capsys):
        # A bare `sylva` now prints a quickstart and exits 0 (see test_cli_help),
        # rather than erroring — an invocation with no work to do is not a failure.
        rc = cli.main([])
        assert rc == 0
        assert "analyze" in capsys.readouterr().out
