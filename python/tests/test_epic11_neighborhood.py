"""Epic 11 — `neighborhood` + `list_symbols` MCP traversal tools.

`neighborhood` returns the N-hop undirected local structure of a symbol over
calls/imports/inherits; `list_symbols` is a cheap graph-only inventory
(optionally filtered by file/kind), no source read. Both let an agent traverse
the tree without pulling bodies.
"""

import json

import pytest

import sylva


def _rpc(db, method, params=None):
    req = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        req["params"] = params
    return json.loads(sylva.handle_request(str(db), json.dumps(req)))


# a -> b -> c chain, plus a class D; a is called by main.
SRC = (
    "def c():\n    return 1\n\n"
    "def b():\n    return c()\n\n"
    "def a():\n    return b()\n\n"
    "def main():\n    return a()\n\n"
    "class D:\n    def m(self):\n        return 1\n"
)


def _repo(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    p = tmp_path / "m.py"
    p.write_text(SRC)
    sylva.write_symbols(str(db), str(p), sylva.extract_symbols(str(p)))
    sylva.build_edges(str(db))
    return db


class TestNeighborhood:
    def test_one_hop(self, tmp_path):
        db = _repo(tmp_path)
        n = _rpc(db, "neighborhood", {"name": "b", "depth": 1})["result"]
        # b's neighbours: a (calls b) and c (b calls c); plus b itself at dist 0.
        by = {x["name"]: x["dist"] for x in n["nodes"]}
        assert by["b"] == 0
        assert by["a"] == 1 and by["c"] == 1
        assert "kind" in n["nodes"][0]  # disambiguates same-named symbols

    def test_depth_expands(self, tmp_path):
        db = _repo(tmp_path)
        n1 = {x["name"] for x in _rpc(db, "neighborhood", {"name": "b", "depth": 1})["result"]["nodes"]}
        n2 = {x["name"] for x in _rpc(db, "neighborhood", {"name": "b", "depth": 2})["result"]["nodes"]}
        assert "main" not in n1 and "main" in n2   # main is 2 hops from b (via a)

    def test_unknown_symbol_empty(self, tmp_path):
        db = _repo(tmp_path)
        n = _rpc(db, "neighborhood", {"name": "nope"})["result"]
        assert n["nodes"] == [] and n["edges"] == []

    def test_missing_name(self, tmp_path):
        db = _repo(tmp_path)
        assert "error" in _rpc(db, "neighborhood", {})


class TestListSymbols:
    def test_all(self, tmp_path):
        db = _repo(tmp_path)
        syms = _rpc(db, "list_symbols")["result"]
        names = {s["name"] for s in syms}
        assert {"a", "b", "c", "main", "D", "m"} <= names
        assert all("code" not in s for s in syms)   # no bodies

    def test_filter_kind(self, tmp_path):
        db = _repo(tmp_path)
        classes = _rpc(db, "list_symbols", {"kind": "class"})["result"]
        assert {s["name"] for s in classes} == {"D"}

    def test_filter_file(self, tmp_path):
        db = _repo(tmp_path)
        syms = _rpc(db, "list_symbols", {"file": "m.py"})["result"]
        assert {"a", "b", "c"} <= {s["name"] for s in syms}


class TestAdvertised:
    def test_in_tools_list(self, tmp_path):
        db = tmp_path / "s.db"
        sylva.init_db(str(db))
        names = {t["name"] for t in _rpc(db, "tools/list")["result"]["tools"]}
        assert {"neighborhood", "list_symbols"} <= names
