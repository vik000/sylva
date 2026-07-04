"""Feature 8.4 — get_source MCP tool (fetch a symbol's code) (issue #46).

`get_source` returns the *current* source code of a symbol (read from its
line_start..line_end span at query time), so an agent can fetch code precisely
via the graph instead of opening whole files. A range form reads an arbitrary
file slice. Rides on the existing JSON-RPC dispatch (1.6 / 3.5 / 7.4 / 8.2).
"""

import json

import pytest

import sylva


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path, text):
    path.write_text(text)
    sylva.write_symbols(str(db), str(path), sylva.extract_symbols(str(path)))
    return path


def _rpc(db, obj):
    return json.loads(sylva.handle_request(str(db), json.dumps(obj)))


# A multi-line function whose span (lines 1-3) we can assert exactly.
SRC = "def greet(name):\n    msg = 'hi ' + name\n    return msg\n\ndef other():\n    return 0\n"


class TestNameForm:
    def test_returns_exact_source_of_symbol(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", SRC)
        resp = _rpc(
            db,
            {"jsonrpc": "2.0", "id": 1, "method": "get_source", "params": {"name": "greet"}},
        )
        result = resp["result"]
        assert len(result) == 1
        entry = result[0]
        assert entry["name"] == "greet"
        assert entry["kind"] == "function"
        assert entry["line_start"] == 1 and entry["line_end"] == 3
        assert entry["code"] == "def greet(name):\n    msg = 'hi ' + name\n    return msg"

    def test_via_tools_call(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", SRC)
        resp = _rpc(
            db,
            {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "get_source", "arguments": {"name": "greet"}},
            },
        )
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert payload[0]["code"].startswith("def greet(name):")

    def test_reads_current_file_not_stale(self, tmp_path):
        db = _init(tmp_path)
        path = _index(db, tmp_path / "m.py", SRC)
        # Edit the file body WITHOUT re-indexing: get_source must reflect the edit.
        path.write_text("def greet(name):\n    return 'EDITED ' + name\n")
        resp = _rpc(
            db,
            {"jsonrpc": "2.0", "id": 3, "method": "get_source", "params": {"name": "greet"}},
        )
        assert "EDITED" in resp["result"][0]["code"]

    def test_in_tools_list(self, tmp_path):
        db = _init(tmp_path)
        resp = _rpc(db, {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        assert "get_source" in names


class TestRangeForm:
    def test_arbitrary_range(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", SRC)
        resp = _rpc(
            db,
            {
                "jsonrpc": "2.0", "id": 5, "method": "get_source",
                "params": {"file": str(tmp_path / "m.py"), "start": 5, "end": 6},
            },
        )
        result = resp["result"]
        assert len(result) == 1
        assert result[0]["code"] == "def other():\n    return 0"

    def test_range_end_beyond_eof_clamped(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", SRC)
        resp = _rpc(
            db,
            {
                "jsonrpc": "2.0", "id": 6, "method": "get_source",
                "params": {"file": str(tmp_path / "m.py"), "start": 5, "end": 999},
            },
        )
        assert resp["result"][0]["code"].startswith("def other():")

    def test_range_missing_bounds_is_error(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", SRC)
        resp = _rpc(
            db,
            {
                "jsonrpc": "2.0", "id": 7, "method": "get_source",
                "params": {"file": str(tmp_path / "m.py"), "start": 5},
            },
        )
        assert resp["error"]["code"] == -32602  # INVALID_PARAMS


class TestNegative:
    def test_unknown_symbol_empty_not_error(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", SRC)
        resp = _rpc(
            db,
            {"jsonrpc": "2.0", "id": 8, "method": "get_source", "params": {"name": "nope"}},
        )
        assert resp["result"] == []  # empty list, not an error

    def test_missing_file_is_error_not_crash(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", SRC)
        # Delete the file after indexing; the symbol still points at it.
        (tmp_path / "m.py").unlink()
        resp = _rpc(
            db,
            {"jsonrpc": "2.0", "id": 9, "method": "get_source", "params": {"name": "greet"}},
        )
        assert "error" in resp  # clear error response, not a crash
        assert resp["error"]["code"] == -32001  # SERVER_ERROR

    def test_missing_arg_is_error(self, tmp_path):
        db = _init(tmp_path)
        resp = _rpc(db, {"jsonrpc": "2.0", "id": 10, "method": "get_source", "params": {}})
        assert resp["error"]["code"] == -32602
