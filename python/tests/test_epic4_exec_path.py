"""Feature 4.11 — Execution-path view from coverage contexts (issue #52).

`exec_path(db, test)` returns the part of the call graph a test actually
exercised (targets of its `test_covers` edges), laid out like the 4.10
flowchart. Reuses Feature 3.3 (test_covers edges) + the shared layering.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

import sylva
from sylva.viz import exec_path, make_server


# c (1-2) <- b (4-5) <- a (7-8); a calls b, b calls c.
CHAIN = (
    "def c():\n    return 1\n\n"
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


def _layers(result):
    return {n["name"]: n["layer"] for n in result["nodes"]}


class TestExecPath:
    def test_covered_call_chain_is_layered(self, tmp_path):
        db = _init(tmp_path)
        src = _index(db, tmp_path / "m.py", CHAIN)
        _index(db, tmp_path / "t.py", "def test_x():\n    return 1\n")
        sylva.build_edges(str(db))
        # test_x exercised lines inside a (7), b (4), c (1).
        sylva.map_tests_to_symbols(str(db), {"test_x": {str(src): [7, 4, 1]}})

        result = exec_path(str(db), "test_x")
        assert _layers(result) == {"a": 0, "b": 1, "c": 2}
        assert len(result["edges"]) == 2  # a->b, b->c among the covered set

    def test_isolated_covered_symbols_all_layer_zero(self, tmp_path):
        db = _init(tmp_path)
        # Two unrelated functions (no calls between them).
        src = _index(db, tmp_path / "m.py", "def p():\n    return 1\n\ndef q():\n    return 2\n")
        _index(db, tmp_path / "t.py", "def test_y():\n    return 1\n")
        sylva.build_edges(str(db))
        sylva.map_tests_to_symbols(str(db), {"test_y": {str(src): [1, 4]}})  # p and q

        result = exec_path(str(db), "test_y")
        assert _layers(result) == {"p": 0, "q": 0}
        assert result["edges"] == []

    def test_partial_coverage_only_shows_touched(self, tmp_path):
        db = _init(tmp_path)
        src = _index(db, tmp_path / "m.py", CHAIN)
        _index(db, tmp_path / "t.py", "def test_z():\n    return 1\n")
        sylva.build_edges(str(db))
        # Only b and c exercised (not a).
        sylva.map_tests_to_symbols(str(db), {"test_z": {str(src): [4, 1]}})

        result = exec_path(str(db), "test_z")
        assert set(_layers(result)) == {"b", "c"}  # a not included
        assert _layers(result)["b"] == 0 and _layers(result)["c"] == 1


class TestEdgeCases:
    def test_unknown_test(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        assert exec_path(str(db), "test_missing") == {"test": "test_missing", "nodes": [], "edges": []}

    def test_test_with_no_coverage(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        _index(db, tmp_path / "t.py", "def test_x():\n    return 1\n")
        sylva.build_edges(str(db))
        # No map_tests_to_symbols run -> no test_covers edges.
        assert exec_path(str(db), "test_x")["nodes"] == []

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            exec_path(str(tmp_path / "nope.db"), "test_x")


class TestExecEndpoint:
    def _serve(self, db):
        httpd = make_server(str(db), 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, httpd.server_address[1]

    def test_exec_endpoint(self, tmp_path):
        db = _init(tmp_path)
        src = _index(db, tmp_path / "m.py", CHAIN)
        _index(db, tmp_path / "t.py", "def test_x():\n    return 1\n")
        sylva.build_edges(str(db))
        sylva.map_tests_to_symbols(str(db), {"test_x": {str(src): [7, 4, 1]}})

        httpd, port = self._serve(db)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/exec?test=test_x", timeout=5) as r:
                body = json.loads(r.read())
            assert {n["name"] for n in body["nodes"]} == {"a", "b", "c"}
        finally:
            httpd.shutdown(); httpd.server_close()

    def test_exec_missing_param(self, tmp_path):
        db = _init(tmp_path)
        httpd, port = self._serve(db)
        try:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/exec", timeout=5)
                assert False, "expected 400"
            except urllib.error.HTTPError as e:
                assert e.code == 400
        finally:
            httpd.shutdown(); httpd.server_close()


class TestUiAsset:
    def test_exec_controls_present(self):
        import os
        import sylva.viz.server as srv

        with open(os.path.join(srv.ASSETS_DIR, "index.html")) as f:
            html = f.read()
        assert "enterExec" in html and "Execution path" in html
