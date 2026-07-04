"""Feature 4.10 — Per-entry-point call flowchart (issue #51).

`flow_layout(db, entry)` returns the reachable outbound call subgraph with a
`layer` per node (BFS depth from the entry) plus the call edges among the
reached set — the data that powers the UI's layered flowchart. Data-driven, so
the layering is testable without rendering.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

import sylva
from sylva.viz import flow_layout, make_server


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path, text):
    path.write_text(text)
    sylva.write_symbols(str(db), str(path), sylva.extract_symbols(str(path)))


def _layers(result):
    return {n["name"]: n["layer"] for n in result["nodes"]}


class TestLayering:
    def test_linear_chain(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db, tmp_path / "m.py",
            "def c():\n    return 1\n\ndef b():\n    return c()\n\ndef a():\n    return b()\n",
        )
        sylva.build_edges(str(db))
        assert _layers(flow_layout(str(db), "a")) == {"a": 0, "b": 1, "c": 2}

    def test_branch(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db, tmp_path / "m.py",
            "def b():\n    return 1\n\ndef c():\n    return 2\n\n"
            "def a():\n    b()\n    return c()\n",
        )
        sylva.build_edges(str(db))
        layers = _layers(flow_layout(str(db), "a"))
        assert layers == {"a": 0, "b": 1, "c": 1}

    def test_diamond_deepest_layer(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db, tmp_path / "m.py",
            "def d():\n    return 1\n\n"
            "def b():\n    return d()\n\ndef c():\n    return d()\n\n"
            "def a():\n    b()\n    return c()\n",
        )
        sylva.build_edges(str(db))
        layers = _layers(flow_layout(str(db), "a"))
        assert layers == {"a": 0, "b": 1, "c": 1, "d": 2}
        # Edges among the reached set are included (a->b, a->c, b->d, c->d).
        assert len(flow_layout(str(db), "a")["edges"]) == 4

    def test_recursion_terminates(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "r.py", "def r():\n    return r()\n")
        sylva.build_edges(str(db))
        result = flow_layout(str(db), "r")
        assert _layers(result) == {"r": 0}  # self-call, single node, no hang

    def test_mutual_recursion_terminates(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db, tmp_path / "c.py",
            "def x():\n    return y()\n\ndef y():\n    return x()\n",
        )
        sylva.build_edges(str(db))
        assert _layers(flow_layout(str(db), "x")) == {"x": 0, "y": 1}


class TestEdgeCases:
    def test_unknown_entry(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", "def a():\n    return 1\n")
        assert flow_layout(str(db), "ghost") == {"entry": "ghost", "nodes": [], "edges": []}

    def test_leaf_entry_just_itself(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", "def a():\n    return 1\n")
        sylva.build_edges(str(db))
        result = flow_layout(str(db), "a")
        assert _layers(result) == {"a": 0}
        assert result["edges"] == []

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            flow_layout(str(tmp_path / "nope.db"), "a")


class TestFlowEndpoint:
    def _serve(self, db):
        httpd = make_server(str(db), 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, httpd.server_address[1]

    def _get(self, port, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_flow_endpoint(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db, tmp_path / "m.py",
            "def b():\n    return 1\n\ndef a():\n    return b()\n",
        )
        sylva.build_edges(str(db))
        httpd, port = self._serve(db)
        try:
            status, body = self._get(port, "/flow?entry=a")
            assert status == 200
            assert {n["name"] for n in body["nodes"]} == {"a", "b"}
        finally:
            httpd.shutdown(); httpd.server_close()

    def test_flow_missing_entry_param(self, tmp_path):
        db = _init(tmp_path)
        httpd, port = self._serve(db)
        try:
            status, body = self._get(port, "/flow")
            assert status == 400
            assert "error" in body
        finally:
            httpd.shutdown(); httpd.server_close()


class TestUiAsset:
    def test_flow_controls_present(self):
        import os
        import sylva.viz.server as srv

        with open(os.path.join(srv.ASSETS_DIR, "index.html")) as f:
            html = f.read()
        assert "enterFlow" in html and 'id="flow-back"' in html and "Flow from here" in html
