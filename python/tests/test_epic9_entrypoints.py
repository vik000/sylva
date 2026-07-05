"""Feature 9.1 — Global entrypoint inference (markers + dominance) (issue #55).

Infers the real start(s) of a program (not just every uncalled leaf) by combining
convention markers (top-level `main`, `if __name__ == "__main__"` guards) with
topological ranking (SCC-condensed roots ranked by reachable-set size), and
designates a primary entrypoint. Exposed as `infer_entrypoints` + an MCP tool.
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


def _entries(db):
    return sylva.infer_entrypoints(str(db))


def _by_name(result):
    return {e["symbol"]: e for e in result}


# main() -> a() -> b() -> c(); a chain rooted at main, guarded by __main__.
APP = (
    "def c():\n    return 1\n\n"
    "def b():\n    return c()\n\n"
    "def a():\n    return b()\n\n"
    "def main():\n    return a()\n\n"
    "if __name__ == '__main__':\n    main()\n"
)


class TestMarkers:
    def test_top_level_main_flagged(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "app.py", APP)
        sylva.build_edges(str(db))
        entries = _by_name(_entries(db))
        assert "main" in entries
        assert entries["main"]["is_marker"] is True
        # `main` is both a top-level main and invoked in the guard.
        assert entries["main"]["marker_kind"] in ("main", "main_guard")

    def test_guard_target_flagged_when_not_named_main(self, tmp_path):
        db = _init(tmp_path)
        # The guard invokes `run`, not `main` -> run is a main_guard marker.
        src = (
            "def helper():\n    return 1\n\n"
            "def run():\n    return helper()\n\n"
            "if __name__ == '__main__':\n    run()\n"
        )
        _index(db, tmp_path / "cli.py", src)
        sylva.build_edges(str(db))
        entries = _by_name(_entries(db))
        assert entries["run"]["is_marker"] is True
        assert entries["run"]["marker_kind"] == "main_guard"


class TestRankingAndPrimary:
    def test_main_is_primary_by_reach_and_marker(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "app.py", APP)
        sylva.build_edges(str(db))
        result = _entries(db)
        # Exactly one primary, and it is main (marker + reaches a,b,c).
        primaries = [e for e in result if e["primary"]]
        assert len(primaries) == 1
        assert primaries[0]["symbol"] == "main"
        assert primaries[0]["rank"] == 1
        assert primaries[0]["reachable"] == 3  # a, b, c

    def test_reachability_ranking_without_markers(self, tmp_path):
        db = _init(tmp_path)
        # Two independent roots, no __main__/main markers: deeper one ranks first.
        src = (
            "def leaf():\n    return 0\n\n"
            "def mid():\n    return leaf()\n\n"
            "def big():\n    return mid()\n\n"       # reaches mid, leaf (2)
            "def small():\n    return 1\n"           # reaches nothing (0)
        )
        _index(db, tmp_path / "m.py", src)
        sylva.build_edges(str(db))
        result = _entries(db)
        assert all(e["is_marker"] is False for e in result)
        ranked = [e["symbol"] for e in result]
        assert ranked.index("big") < ranked.index("small")
        assert _by_name(result)["big"]["primary"] is True


class TestEdgeCases:
    def test_empty_graph(self, tmp_path):
        db = _init(tmp_path)
        assert _entries(db) == []

    def test_single_node(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", "def only():\n    return 1\n")
        sylva.build_edges(str(db))
        result = _entries(db)
        assert len(result) == 1
        assert result[0]["symbol"] == "only"
        assert result[0]["primary"] is True
        assert result[0]["reachable"] == 0

    def test_mutual_recursion_root_condensed(self, tmp_path):
        db = _init(tmp_path)
        # a <-> b mutual recursion with no external caller: an SCC root, so an
        # entrypoint is still inferred (no infinite traversal, not dropped).
        src = "def a():\n    return b()\n\ndef b():\n    return a()\n"
        _index(db, tmp_path / "m.py", src)
        sylva.build_edges(str(db))
        result = _entries(db)
        names = {e["symbol"] for e in result}
        assert {"a", "b"} <= names  # cycle members surface as candidates
        assert any(e["primary"] for e in result)

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sylva.infer_entrypoints(str(tmp_path / "nope.db"))


class TestMcpSurface:
    def _rpc(self, db, obj):
        return json.loads(sylva.handle_request(str(db), json.dumps(obj)))

    def test_in_tools_list(self, tmp_path):
        db = _init(tmp_path)
        resp = self._rpc(db, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert "infer_entrypoints" in {t["name"] for t in resp["result"]["tools"]}

    def test_tool_dispatch(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "app.py", APP)
        sylva.build_edges(str(db))
        resp = self._rpc(
            db,
            {"jsonrpc": "2.0", "id": 2, "method": "infer_entrypoints", "params": {}},
        )
        assert any(e["symbol"] == "main" and e["primary"] for e in resp["result"])
