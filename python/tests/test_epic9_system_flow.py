"""Feature 9.4 — Global "system flow" view (whole-project block diagram) (#58).

`system_flow(db, root=None)` is the first-class global layered view: the
reachable outbound call subgraph from the program's entrypoint (default: Feature
9.1's inferred primary), with each node tagged `on_spine` from Feature 9.2's main
spine. Served at `/system-flow`; the UI exposes it as a top-level mode reachable
without selecting a node.
"""

import json
import os
import threading
import urllib.error
import urllib.request

import pytest

import sylva
from sylva.viz import make_server, system_flow
import sylva.viz.server as srv


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path, text):
    path.write_text(text)
    sylva.write_symbols(str(db), str(path), sylva.extract_symbols(str(path)))
    return path


# main -> a -> b (deep) and main -> c (shallow), guarded by __main__.
# Primary = main; spine = main, a, b; c is reachable but off-spine.
APP = (
    "def b():\n    return 1\n\n"
    "def c():\n    return 1\n\n"
    "def a():\n    return b()\n\n"
    "def main():\n    return a() + c()\n\n"
    "if __name__ == '__main__':\n    main()\n"
)


def _layers(result):
    return {n["name"]: n["layer"] for n in result["nodes"]}


class TestSystemFlow:
    def test_defaults_to_primary_and_layers(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "app.py", APP)
        sylva.build_edges(str(db))
        result = system_flow(str(db))
        assert result["root"] == "main"
        assert _layers(result) == {"main": 0, "a": 1, "c": 1, "b": 2}

    def test_spine_nodes_flagged(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "app.py", APP)
        sylva.build_edges(str(db))
        result = system_flow(str(db))
        on_spine = {n["name"] for n in result["nodes"] if n["on_spine"]}
        assert on_spine == {"main", "a", "b"}  # the deepest chain; c is off-spine

    def test_explicit_root_honoured(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "app.py", APP)
        sylva.build_edges(str(db))
        result = system_flow(str(db), "a")
        assert result["root"] == "a"
        assert set(_layers(result)) == {"a", "b"}  # subgraph rooted at a


class TestEdgeCases:
    def test_empty_graph(self, tmp_path):
        db = _init(tmp_path)
        result = system_flow(str(db))
        assert result == {"root": None, "nodes": [], "edges": []}

    def test_unknown_root_empty_nodes(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "app.py", APP)
        sylva.build_edges(str(db))
        result = system_flow(str(db), "nope")
        assert result["nodes"] == []

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            system_flow(str(tmp_path / "nope.db"))


class TestEndpoint:
    def _serve(self, db):
        httpd = make_server(str(db), 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, httpd.server_address[1]

    def test_endpoint_default_root(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "app.py", APP)
        sylva.build_edges(str(db))
        httpd, port = self._serve(db)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/system-flow", timeout=5
            ) as r:
                body = json.loads(r.read())
            assert body["root"] == "main"
            assert {n["name"] for n in body["nodes"]} == {"main", "a", "b", "c"}
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_endpoint_explicit_root(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "app.py", APP)
        sylva.build_edges(str(db))
        httpd, port = self._serve(db)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/system-flow?root=a", timeout=5
            ) as r:
                body = json.loads(r.read())
            assert body["root"] == "a"
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_endpoint_missing_db_500(self, tmp_path):
        httpd = make_server(str(tmp_path / "nope.db"), 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        try:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/system-flow", timeout=5)
                assert False, "expected 500"
            except urllib.error.HTTPError as e:
                assert e.code == 500
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestUiAsset:
    def test_system_flow_control_present(self):
        with open(os.path.join(srv.ASSETS_DIR, "index.html")) as f:
            html = f.read()
        # A top-level control + handler reachable without selecting a node:
        # the "Overview" entry in the view picker calls enterSystemFlow.
        assert 'data-view="overview"' in html
        assert "enterSystemFlow" in html
        assert "on_spine" in html  # spine emphasis wired in
