"""Feature 8.2 — Per-project MCP scaffold (agent access) (issue #41).

Interpretation (A): generate a project-local MCP config that wires Sylva's
existing query tools to this repo's db, and put the remaining graph-traversal
tools (trace_calls, blast_radius, get_architecture) on the MCP surface so the
scaffold advertises the full toolset.
"""

import json
import os

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


CHAIN = "def helper():\n    return 1\n\ndef main():\n    return helper()\n"


class TestScaffold:
    def test_writes_valid_config_with_absolute_db(self, tmp_path):
        db = _init(tmp_path)
        out = tmp_path / ".codemcp"
        cfg_path = sylva.init_mcp(str(db), str(out))

        assert os.path.isfile(cfg_path)
        with open(cfg_path) as f:
            cfg = json.load(f)  # valid JSON
        server = cfg["mcpServers"]["sylva"]
        assert server["command"] == "sylva"
        assert server["args"][:2] == ["serve", "--db"]
        db_arg = server["args"][2]
        assert os.path.isabs(db_arg)  # absolute so it works from any client cwd
        assert os.path.samefile(db_arg, str(db))

    def test_relative_db_is_absolutised(self, tmp_path, monkeypatch):
        db = _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        cfg_path = sylva.init_mcp("sylva.db", ".codemcp")
        with open(cfg_path) as f:
            cfg = json.load(f)
        db_arg = cfg["mcpServers"]["sylva"]["args"][2]
        assert os.path.isabs(db_arg)
        assert os.path.samefile(db_arg, str(db))

    def test_emits_tool_manifest(self, tmp_path):
        db = _init(tmp_path)
        out = tmp_path / ".codemcp"
        sylva.init_mcp(str(db), str(out))
        with open(out / "tools.json") as f:
            manifest = json.load(f)
        names = {t["name"] for t in manifest["tools"]}
        # Full toolset advertised, including the newly-surfaced traversal tools.
        assert {"search_symbol", "trace_calls", "blast_radius", "get_architecture"} <= names

    def test_idempotent(self, tmp_path):
        db = _init(tmp_path)
        out = tmp_path / ".codemcp"
        a = sylva.init_mcp(str(db), str(out))
        first = open(a).read()
        b = sylva.init_mcp(str(db), str(out))
        assert a == b
        assert open(b).read() == first  # re-run overwrites with identical content

    def test_custom_out_respected(self, tmp_path):
        db = _init(tmp_path)
        out = tmp_path / "agent" / "mcp"
        cfg_path = sylva.init_mcp(str(db), str(out))
        assert str(out) in cfg_path
        assert os.path.isfile(out / "mcp.json")


class TestTraversalToolsOnSurface:
    def test_tools_list_advertises_new_tools(self, tmp_path):
        db = _init(tmp_path)
        resp = _rpc(db, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        by_name = {t["name"]: t for t in resp["result"]["tools"]}
        assert {"trace_calls", "blast_radius", "get_architecture"} <= set(by_name)
        # trace_calls advertises its richer argument schema.
        assert "direction" in by_name["trace_calls"]["inputSchema"]["properties"]

    def test_trace_calls_plain_and_mcp(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        sylva.build_edges(str(db))

        # Plain JSON-RPC.
        plain = _rpc(
            db,
            {
                "jsonrpc": "2.0", "id": 2, "method": "trace_calls",
                "params": {"symbol": "main", "direction": "outbound", "depth": 3},
            },
        )
        names = {row["name"] for row in plain["result"]}
        assert {"main", "helper"} <= names

        # Via tools/call — same result, wrapped as MCP text content.
        wrapped = _rpc(
            db,
            {
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {
                    "name": "trace_calls",
                    "arguments": {"symbol": "main", "direction": "outbound", "depth": 3},
                },
            },
        )
        payload = json.loads(wrapped["result"]["content"][0]["text"])
        assert {row["name"] for row in payload} >= {"main", "helper"}

    def test_blast_radius(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        sylva.build_edges(str(db))
        resp = _rpc(
            db,
            {"jsonrpc": "2.0", "id": 4, "method": "blast_radius", "params": {"symbol": "helper"}},
        )
        assert {row["name"] for row in resp["result"]} == {"main"}

    def test_get_architecture(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        sylva.build_edges(str(db))
        resp = _rpc(db, {"jsonrpc": "2.0", "id": 5, "method": "get_architecture", "params": {}})
        result = resp["result"]
        assert {"modules", "hubs", "entry_points"} <= set(result)

    def test_get_architecture_via_tools_call(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        sylva.build_edges(str(db))
        resp = _rpc(
            db,
            {
                "jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "get_architecture", "arguments": {"hub_limit": 5}},
            },
        )
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert "entry_points" in payload

    def test_trace_calls_missing_symbol_is_error(self, tmp_path):
        db = _init(tmp_path)
        resp = _rpc(
            db,
            {"jsonrpc": "2.0", "id": 7, "method": "trace_calls", "params": {"depth": 2}},
        )
        assert resp["error"]["code"] == -32602  # INVALID_PARAMS

    def test_trace_calls_bad_direction_is_error(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        sylva.build_edges(str(db))
        resp = _rpc(
            db,
            {
                "jsonrpc": "2.0", "id": 8, "method": "trace_calls",
                "params": {"symbol": "main", "direction": "sideways"},
            },
        )
        # trace_calls raises ValueError -> mapped to INVALID_PARAMS.
        assert resp["error"]["code"] == -32602
