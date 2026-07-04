"""Feature 4.9 — Focus / neighborhood mode with depth control (issue #50).

`neighborhood(db, symbol, depth)` returns the induced subgraph of every symbol
within N *undirected* hops of the centre over calls/imports edges, each node
carrying its hop `dist`. It drives the UI's focus mode (depth slider) — the
cheapest cure for the force-graph hairball. Served over `/neighborhood`.
"""

import json
import os
import threading
import urllib.error
import urllib.request

import pytest

import sylva
from sylva.viz import make_server, neighborhood
import sylva.viz.server as srv


# A linear chain d <- c <- b <- a : a calls b, b calls c, c calls d.
# Lines: d(1) c(4) b(7) a(10).
CHAIN = (
    "def d():\n    return 0\n\n"
    "def c():\n    return d()\n\n"
    "def b():\n    return c()\n\n"
    "def a():\n    return b()\n"
)


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path, text):
    path.write_text(text)
    sylva.write_symbols(str(db), str(path), sylva.extract_symbols(str(path)))
    return path


def _dist(result):
    return {n["name"]: n["dist"] for n in result["nodes"]}


class TestNeighborhood:
    def test_depth_one_both_directions(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        sylva.build_edges(str(db))
        # b's 1-hop undirected neighbourhood = a (caller) + c (callee) + b.
        result = neighborhood(str(db), "b", 1)
        assert _dist(result) == {"b": 0, "a": 1, "c": 1}

    def test_depth_two_reaches_further(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        sylva.build_edges(str(db))
        # depth 2 from b reaches d (b->c->d) on the callee side.
        result = neighborhood(str(db), "b", 2)
        got = _dist(result)
        assert got == {"b": 0, "a": 1, "c": 1, "d": 2}

    def test_edges_restricted_to_reached_set(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        sylva.build_edges(str(db))
        result = neighborhood(str(db), "b", 1)
        # Among {a, b, c}: edges a->b and b->c are present; c->d is not (d excluded).
        pairs = {(e["source"], e["target"]) for e in result["edges"]}
        names = {n["id"]: n["name"] for n in result["nodes"]}
        named = {(names[s], names[t]) for (s, t) in pairs}
        assert named == {("a", "b"), ("b", "c")}


class TestEdgeCases:
    def test_depth_zero_is_just_center(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        sylva.build_edges(str(db))
        result = neighborhood(str(db), "b", 0)
        assert _dist(result) == {"b": 0}
        assert result["edges"] == []

    def test_isolated_symbol(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", "def lonely():\n    return 1\n")
        sylva.build_edges(str(db))
        result = neighborhood(str(db), "lonely", 3)
        assert _dist(result) == {"lonely": 0}

    def test_cycle_terminates(self, tmp_path):
        db = _init(tmp_path)
        # Mutual recursion p <-> q; a deep depth must still terminate.
        _index(
            db,
            tmp_path / "m.py",
            "def p():\n    return q()\n\ndef q():\n    return p()\n",
        )
        sylva.build_edges(str(db))
        result = neighborhood(str(db), "p", 9)
        assert _dist(result) == {"p": 0, "q": 1}

    def test_unknown_symbol_empty(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        result = neighborhood(str(db), "nope", 2)
        assert result == {"center": "nope", "depth": 2, "nodes": [], "edges": []}

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            neighborhood(str(tmp_path / "nope.db"), "b", 1)


class TestNeighborhoodEndpoint:
    def _serve(self, db):
        httpd = make_server(str(db), 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, httpd.server_address[1]

    def test_endpoint_returns_neighborhood(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        sylva.build_edges(str(db))
        httpd, port = self._serve(db)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/neighborhood?symbol=b&depth=1", timeout=5
            ) as r:
                body = json.loads(r.read())
            assert {n["name"] for n in body["nodes"]} == {"a", "b", "c"}
            assert body["depth"] == 1
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_endpoint_missing_symbol_400(self, tmp_path):
        db = _init(tmp_path)
        httpd, port = self._serve(db)
        try:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/neighborhood", timeout=5
                )
                assert False, "expected 400"
            except urllib.error.HTTPError as e:
                assert e.code == 400
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_endpoint_bad_depth_400(self, tmp_path):
        db = _init(tmp_path)
        httpd, port = self._serve(db)
        try:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/neighborhood?symbol=b&depth=abc", timeout=5
                )
                assert False, "expected 400"
            except urllib.error.HTTPError as e:
                assert e.code == 400
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestUiAsset:
    def test_focus_controls_present(self):
        with open(os.path.join(srv.ASSETS_DIR, "index.html")) as f:
            html = f.read()
        assert 'id="focus-depth"' in html and 'id="focus-panel"' in html
        assert "enterFocus" in html and "neighborhood" in html
