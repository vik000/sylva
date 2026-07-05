"""Feature 9.3 — Centrality: dominators + betweenness (issue #57).

Ranks symbols by structural importance on paths: betweenness (Brandes') finds
chokepoints many shortest paths run through; dominators (from a super-source
over the inferred entrypoints) find gateways that gate a subtree (`dominates`).
Completes Epic 9.
"""

import pytest

import sylva


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path, text):
    path.write_text(text)
    sylva.write_symbols(str(db), str(path), sylva.extract_symbols(str(path)))
    return db


def _by_name(result):
    return {r["symbol"]: r for r in result}


class TestBetweenness:
    def test_articulation_point_scores_highest(self, tmp_path):
        db = _init(tmp_path)
        # a -> mid -> z, b -> mid -> z: `mid` is the chokepoint every path crosses.
        _index(
            db,
            tmp_path / "m.py",
            "def z():\n    return 0\n\n"
            "def mid():\n    return z()\n\n"
            "def a():\n    return mid()\n\n"
            "def b():\n    return mid()\n",
        )
        sylva.build_edges(str(db))
        c = _by_name(sylva.centrality(str(db)))
        # mid lies on a->z and b->z shortest paths; the leaves lie on none.
        assert c["mid"]["betweenness"] > c["a"]["betweenness"]
        assert c["mid"]["betweenness"] > c["z"]["betweenness"]
        assert c["a"]["betweenness"] == 0.0  # a source is on no intermediate path

    def test_ranked_by_betweenness(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db,
            tmp_path / "m.py",
            "def z():\n    return 0\n\ndef mid():\n    return z()\n\n"
            "def a():\n    return mid()\n\ndef b():\n    return mid()\n",
        )
        sylva.build_edges(str(db))
        result = sylva.centrality(str(db))
        assert result[0]["symbol"] == "mid"  # highest betweenness ranks first


class TestDominators:
    def test_gateway_dominates_subtree(self, tmp_path):
        db = _init(tmp_path)
        # main -> gate -> {x, y}: every path to x/y goes through gate, so gate
        # dominates x and y (dominates == 2). main dominates gate,x,y (== 3).
        _index(
            db,
            tmp_path / "app.py",
            "def x():\n    return 1\n\ndef y():\n    return 2\n\n"
            "def gate():\n    return x() + y()\n\n"
            "def main():\n    return gate()\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        sylva.build_edges(str(db))
        c = _by_name(sylva.centrality(str(db)))
        assert c["gate"]["dominates"] == 2   # x, y
        assert c["main"]["dominates"] == 3   # gate, x, y
        assert c["x"]["dominates"] == 0      # a leaf dominates nothing

    def test_shared_child_not_dominated(self, tmp_path):
        db = _init(tmp_path)
        # main -> a -> shared, main -> b -> shared: `shared` is reachable via two
        # routes, so neither a nor b dominates it (only main does).
        _index(
            db,
            tmp_path / "app.py",
            "def shared():\n    return 0\n\n"
            "def a():\n    return shared()\n\ndef b():\n    return shared()\n\n"
            "def main():\n    return a() + b()\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        sylva.build_edges(str(db))
        c = _by_name(sylva.centrality(str(db)))
        assert c["a"]["dominates"] == 0  # doesn't solely gate `shared`
        assert c["b"]["dominates"] == 0
        assert c["main"]["dominates"] == 3  # a, b, shared


class TestEdgeCases:
    def test_single_node(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", "def only():\n    return 1\n")
        sylva.build_edges(str(db))
        c = sylva.centrality(str(db))
        assert len(c) == 1
        assert c[0]["betweenness"] == 0.0 and c[0]["dominates"] == 0

    def test_cycle_terminates(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", "def a():\n    return b()\n\ndef b():\n    return a()\n")
        sylva.build_edges(str(db))
        c = sylva.centrality(str(db))  # must return, not hang
        assert {r["symbol"] for r in c} == {"a", "b"}

    def test_empty_graph(self, tmp_path):
        db = _init(tmp_path)
        assert sylva.centrality(str(db)) == []

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sylva.centrality(str(tmp_path / "nope.db"))


class TestMcpSurface:
    def _rpc(self, db, obj):
        import json

        return json.loads(sylva.handle_request(str(db), json.dumps(obj)))

    def test_in_tools_list(self, tmp_path):
        db = _init(tmp_path)
        resp = self._rpc(db, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert "centrality" in {t["name"] for t in resp["result"]["tools"]}

    def test_tool_dispatch(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db,
            tmp_path / "m.py",
            "def z():\n    return 0\n\ndef mid():\n    return z()\n\n"
            "def a():\n    return mid()\n\ndef b():\n    return mid()\n",
        )
        sylva.build_edges(str(db))
        resp = self._rpc(db, {"jsonrpc": "2.0", "id": 2, "method": "centrality", "params": {}})
        assert resp["result"][0]["symbol"] == "mid"
