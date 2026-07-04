"""Feature 7.4 — Real MCP protocol handshake (issue #31).

The server speaks MCP framing on top of the Feature 1.6 plain JSON-RPC dispatch:
`initialize` (handshake), `tools/list` (discovery with input schemas), and
`tools/call` ({name, arguments}) routing to the existing tool handlers. A
`notifications/initialized` notification yields no response. The original
one-method-per-tool calls keep working (additive, not a replacement).
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


def _call(db, obj):
    return json.loads(sylva.handle_request(str(db), json.dumps(obj)))


CHAIN = "def helper():\n    return 1\n\ndef main():\n    return helper()\n"


class TestInitialize:
    def test_initialize_returns_capabilities(self, tmp_path):
        db = _init(tmp_path)
        resp = _call(db, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert resp["id"] == 1
        result = resp["result"]
        assert "protocolVersion" in result
        assert "tools" in result["capabilities"]
        assert result["serverInfo"]["name"] == "sylva"
        assert result["serverInfo"]["version"]  # non-empty version string

    def test_initialized_notification_yields_no_response(self, tmp_path):
        db = _init(tmp_path)
        # A notification (no id) must produce no reply: handle_request returns "".
        raw = sylva.handle_request(
            str(db), json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )
        assert raw == ""


class TestToolsList:
    def test_lists_tools_with_schemas(self, tmp_path):
        db = _init(tmp_path)
        resp = _call(db, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}
        # The three core tools (1.6) must be advertised; coverage tools (3.5) too.
        assert {"search_symbol", "get_callers", "get_dependencies"} <= names
        for t in tools:
            assert "description" in t
            assert t["inputSchema"]["type"] == "object"


class TestToolsCall:
    def test_call_dispatches_to_handler(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        resp = _call(
            db,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "search_symbol", "arguments": {"name": "helper"}},
            },
        )
        result = resp["result"]
        assert result["isError"] is False
        # MCP wraps the tool result as text content; it parses back to the rows.
        payload = json.loads(result["content"][0]["text"])
        assert any(row["name"] == "helper" for row in payload)

    def test_call_get_callers_via_edges(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        sylva.build_edges(str(db))
        resp = _call(
            db,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "get_callers", "arguments": {"symbol": "helper"}},
            },
        )
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert {row["name"] for row in payload} == {"main"}

    def test_unknown_tool_returns_error(self, tmp_path):
        db = _init(tmp_path)
        resp = _call(
            db,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "no_such_tool", "arguments": {}},
            },
        )
        assert "error" in resp
        assert resp["error"]["code"] == -32601  # METHOD_NOT_FOUND
        assert "no_such_tool" in resp["error"]["message"]

    def test_call_missing_tool_name(self, tmp_path):
        db = _init(tmp_path)
        resp = _call(
            db,
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"arguments": {}}},
        )
        assert resp["error"]["code"] == -32602  # INVALID_PARAMS

    def test_call_missing_argument(self, tmp_path):
        db = _init(tmp_path)
        resp = _call(
            db,
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "search_symbol", "arguments": {}},
            },
        )
        assert resp["error"]["code"] == -32602  # INVALID_PARAMS


class TestBackwardCompatible:
    def test_plain_jsonrpc_still_works(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        # The original 1.6 one-method-per-tool call is unchanged (no content wrapper).
        resp = _call(
            db,
            {"jsonrpc": "2.0", "id": 8, "method": "search_symbol", "params": {"name": "helper"}},
        )
        assert any(row["name"] == "helper" for row in resp["result"])

    def test_plain_unknown_method_still_method_not_found(self, tmp_path):
        db = _init(tmp_path)
        resp = _call(db, {"jsonrpc": "2.0", "id": 9, "method": "bogus", "params": {}})
        assert resp["error"]["code"] == -32601
        assert "Method not found" in resp["error"]["message"]
