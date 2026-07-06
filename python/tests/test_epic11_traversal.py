"""Epic 11 — token-efficient traversal MCP tools: `get_outline` + `get_overview`.

`get_outline` returns a file's skeleton (symbols + signatures + docstrings +
spans, no bodies) so an agent reads a file's shape without its full source.
`get_overview` returns a one-call orientation map. Both are deterministic — Sylva
exposes the tree; the agent builds understanding on top.
"""

import json

import pytest

import sylva


def _rpc(db, method, params=None):
    req = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        req["params"] = params
    return json.loads(sylva.handle_request(str(db), json.dumps(req)))


APP = (
    "class Thing:\n"
    "    '''A thing.'''\n"
    "    def __init__(self, x):\n        self.x = x\n"
    "    def run(self):\n        '''Run it.'''\n        return self.x\n\n"
    "def main():\n    '''Entry.'''\n    return Thing(1).run()\n\n"
    "if __name__ == '__main__':\n    main()\n"
)


def _repo(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    p = tmp_path / "app.py"
    p.write_text(APP)  # absolute path stored → readable for signatures
    sylva.write_symbols(str(db), str(p), sylva.extract_symbols(str(p)))
    sylva.build_edges(str(db))
    return db, p


class TestOutline:
    def test_skeleton_signatures_no_bodies(self, tmp_path):
        db, _ = _repo(tmp_path)
        out = _rpc(db, "get_outline", {"file": "app.py"})["result"]
        by = {s["name"]: s for s in out["symbols"]}
        assert {"Thing", "__init__", "run", "main"} <= set(by)
        # signatures come from the source; docstrings from the graph; no bodies.
        assert by["Thing"]["signature"].startswith("class Thing")
        assert by["run"]["signature"].startswith("def run")
        assert by["Thing"]["docstring"] == "A thing."
        assert all("code" not in s and "body" not in s for s in out["symbols"])
        # spans present for span-based navigation
        assert by["main"]["line_start"] and by["main"]["line_end"]

    def test_unknown_file_errors(self, tmp_path):
        db, _ = _repo(tmp_path)
        r = _rpc(db, "get_outline", {"file": "nope.py"})
        assert "error" in r

    def test_missing_file_param(self, tmp_path):
        db, _ = _repo(tmp_path)
        r = _rpc(db, "get_outline", {})
        assert "error" in r


class TestOverview:
    def test_one_call_map(self, tmp_path):
        db, _ = _repo(tmp_path)
        ov = _rpc(db, "get_overview")["result"]
        assert {"archetype", "primary_entrypoint", "entrypoints",
                "layers", "modules", "hubs", "spine"} <= set(ov)
        # main() is the marker entrypoint here.
        names = {e["symbol"] for e in ov["entrypoints"]}
        assert "main" in names
        assert isinstance(ov["layers"], dict)  # per-layer counts, compact

    def test_empty_db(self, tmp_path):
        db = tmp_path / "s.db"
        sylva.init_db(str(db))
        ov = _rpc(db, "get_overview")["result"]
        assert "archetype" in ov  # minimal but valid, no crash


class TestAdvertised:
    def test_in_tools_list(self, tmp_path):
        db = tmp_path / "s.db"
        sylva.init_db(str(db))
        tools = _rpc(db, "tools/list")["result"]["tools"]
        names = {t["name"] for t in tools}
        assert {"get_outline", "get_overview"} <= names
        outline = next(t for t in tools if t["name"] == "get_outline")
        assert outline["inputSchema"]["required"] == ["file"]


class TestFraming:
    """Pillar 4 — graph-first framing so the agent navigates instead of reading."""

    def test_initialize_carries_instructions(self, tmp_path):
        db = tmp_path / "s.db"
        sylva.init_db(str(db))
        r = _rpc(db, "initialize", {"protocolVersion": "2024-11-05",
                                    "capabilities": {}, "clientInfo": {"name": "c", "version": "1"}})
        instr = r["result"]["instructions"]
        assert "get_overview" in instr and "get_source" in instr
        assert "whole files" in instr          # the "don't read files" nudge

    def test_brief_has_agent_section(self, tmp_path):
        from sylva.report import generate_brief

        db, _ = _repo(tmp_path)
        md = generate_brief(str(db))
        assert "Exploring this repo with an agent" in md
        assert "get_outline" in md and "get_source" in md
