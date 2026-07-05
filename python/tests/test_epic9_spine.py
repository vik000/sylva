"""Feature 9.2 — SCC condensation + longest-path "main spine" (issue #56).

`main_spine(db, entry=None)` returns the longest execution path from an
entrypoint (default: Feature 9.1's primary), SCC-condensed so cycles collapse
and the DP terminates. Nodes carry a `layer` (depth) and `in_cycle`, in the 4.10
layered shape so 9.4 can render it directly.
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
    return path


def _layers(result):
    return {n["symbol"]: n["layer"] for n in result["nodes"]}


class TestSpine:
    def test_linear_chain(self, tmp_path):
        db = _init(tmp_path)
        # a -> b -> c -> d, entry a.
        _index(
            db,
            tmp_path / "m.py",
            "def d():\n    return 1\n\n"
            "def c():\n    return d()\n\n"
            "def b():\n    return c()\n\n"
            "def a():\n    return b()\n",
        )
        sylva.build_edges(str(db))
        result = sylva.main_spine(str(db), "a")
        assert _layers(result) == {"a": 0, "b": 1, "c": 2, "d": 3}
        assert result["entry"] == "a"
        # a->b, b->c, c->d among the spine set.
        assert len(result["edges"]) == 3

    def test_longest_branch_chosen(self, tmp_path):
        db = _init(tmp_path)
        # a -> b (shallow), a -> c -> d (deeper): spine = a, c, d.
        _index(
            db,
            tmp_path / "m.py",
            "def b():\n    return 1\n\n"
            "def d():\n    return 1\n\n"
            "def c():\n    return d()\n\n"
            "def a():\n    return b() + c()\n",
        )
        sylva.build_edges(str(db))
        result = sylva.main_spine(str(db), "a")
        names = set(_layers(result))
        assert names == {"a", "c", "d"}  # b (shallow branch) is not on the spine
        assert _layers(result) == {"a": 0, "c": 1, "d": 2}

    def test_cycle_condensed_and_flagged(self, tmp_path):
        db = _init(tmp_path)
        # a -> b -> c -> b (b,c form a cycle). Spine: a(layer0), {b,c}(layer1).
        _index(
            db,
            tmp_path / "m.py",
            "def b():\n    return c()\n\n"
            "def c():\n    return b()\n\n"
            "def a():\n    return b()\n",
        )
        sylva.build_edges(str(db))
        result = sylva.main_spine(str(db), "a")
        layers = _layers(result)
        assert layers["a"] == 0
        assert layers["b"] == 1 and layers["c"] == 1  # collapsed component
        cyc = {n["symbol"]: n["in_cycle"] for n in result["nodes"]}
        assert cyc["a"] is False
        assert cyc["b"] is True and cyc["c"] is True

    def test_self_recursion_flagged_in_cycle(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", "def loop():\n    return loop()\n")
        sylva.build_edges(str(db))
        result = sylva.main_spine(str(db), "loop")
        node = result["nodes"][0]
        assert node["symbol"] == "loop"
        assert node["in_cycle"] is True  # self-loop counts as a cycle


class TestDefaultEntry:
    def test_defaults_to_inferred_primary(self, tmp_path):
        db = _init(tmp_path)
        # main is marked (guard) and reaches the chain -> the primary entry.
        _index(
            db,
            tmp_path / "app.py",
            "def c():\n    return 1\n\n"
            "def b():\n    return c()\n\n"
            "def main():\n    return b()\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        sylva.build_edges(str(db))
        result = sylva.main_spine(str(db))  # no entry arg
        assert result["entry"] == "main"
        assert _layers(result) == {"main": 0, "b": 1, "c": 2}


class TestEdgeCases:
    def test_unknown_entry_empty(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", "def a():\n    return 1\n")
        sylva.build_edges(str(db))
        result = sylva.main_spine(str(db), "nope")
        assert result == {"entry": "nope", "nodes": [], "edges": []}

    def test_single_node(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", "def only():\n    return 1\n")
        sylva.build_edges(str(db))
        result = sylva.main_spine(str(db), "only")
        assert _layers(result) == {"only": 0}
        assert result["edges"] == []

    def test_empty_graph(self, tmp_path):
        db = _init(tmp_path)
        result = sylva.main_spine(str(db))
        assert result["nodes"] == [] and result["edges"] == []

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sylva.main_spine(str(tmp_path / "nope.db"))


class TestMcpSurface:
    def _rpc(self, db, obj):
        import json

        return json.loads(sylva.handle_request(str(db), json.dumps(obj)))

    def test_in_tools_list(self, tmp_path):
        db = _init(tmp_path)
        resp = self._rpc(db, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert "main_spine" in {t["name"] for t in resp["result"]["tools"]}

    def test_tool_dispatch(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db,
            tmp_path / "m.py",
            "def c():\n    return 1\n\ndef b():\n    return c()\n\ndef a():\n    return b()\n",
        )
        sylva.build_edges(str(db))
        resp = self._rpc(
            db,
            {"jsonrpc": "2.0", "id": 2, "method": "main_spine", "params": {"entry": "a"}},
        )
        assert {n["symbol"] for n in resp["result"]["nodes"]} == {"a", "b", "c"}
