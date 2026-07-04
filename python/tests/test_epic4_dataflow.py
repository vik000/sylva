"""Feature 4.12 — Data-flow (data-path) view, tier 1: static parameter flow (#53).

Tier 1 tracks parameter pass-through: when function f passes its parameter p as
an argument into a call to g, `build_dataflow` records (f, g, p) in the
`dataflow` table. `data_flow(db, symbol)` lays out the reachable parameter-flow
subgraph as a layered flowchart (reusing 4.10). Deliberately approximate:
name-based, no aliasing/reassignment, no closure capture.
"""

import json
import os
import threading
import urllib.error
import urllib.request

import pytest

import sylva
from sylva.viz import data_flow, make_server
import sylva.viz.server as srv


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path, text):
    path.write_text(text)
    sylva.write_symbols(str(db), str(path), sylva.extract_symbols(str(path)))
    return path


def _flows(db):
    """Read the raw dataflow table as {(src_name, dst_name, param)}."""
    import sqlite3

    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT s.name, d.name, df.param "
            "FROM dataflow df JOIN symbols s ON s.id = df.src_id "
            "JOIN symbols d ON d.id = df.dst_id"
        ).fetchall()
    finally:
        conn.close()
    return {(a, b, p) for (a, b, p) in rows}


def _layers(result):
    return {n["name"]: n["layer"] for n in result["nodes"]}


class TestBuildDataflow:
    def test_param_passthrough_edge(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", "def g(v):\n    return v\n\ndef f(x):\n    return g(x)\n")
        n = sylva.build_dataflow(str(db))
        assert n == 1
        assert _flows(db) == {("f", "g", "x")}

    def test_chain_layers_via_view(self, tmp_path):
        db = _init(tmp_path)
        # h(x) <- g(x) <- f(x): each passes its own param x onward.
        _index(
            db,
            tmp_path / "m.py",
            "def h(x):\n    return x\n\n"
            "def g(x):\n    return h(x)\n\n"
            "def f(x):\n    return g(x)\n",
        )
        sylva.build_dataflow(str(db))
        result = data_flow(str(db), "f")
        assert _layers(result) == {"f": 0, "g": 1, "h": 2}
        assert len(result["edges"]) == 2
        assert all("param" in e for e in result["edges"])

    def test_param_not_passed_no_edge(self, tmp_path):
        db = _init(tmp_path)
        # f receives x but passes a literal, not x -> no data flow.
        _index(db, tmp_path / "m.py", "def g(v):\n    return v\n\ndef f(x):\n    return g(1)\n")
        assert sylva.build_dataflow(str(db)) == 0
        assert _flows(db) == set()

    def test_keyword_argument_passthrough(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", "def g(v):\n    return v\n\ndef f(x):\n    return g(v=x)\n")
        sylva.build_dataflow(str(db))
        assert _flows(db) == {("f", "g", "x")}


class TestApproximationLimits:
    def test_reassignment_not_followed(self, tmp_path):
        db = _init(tmp_path)
        # y = x; g(y) -- tier 1 is name-based and does NOT follow the alias.
        _index(
            db,
            tmp_path / "m.py",
            "def g(v):\n    return v\n\ndef f(x):\n    y = x\n    return g(y)\n",
        )
        assert sylva.build_dataflow(str(db)) == 0  # documented limitation

    def test_recursion_terminates(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", "def f(x):\n    return f(x)\n")
        sylva.build_dataflow(str(db))
        # Self data-flow edge f->f on param x; the view must not loop forever.
        assert _flows(db) == {("f", "f", "x")}
        result = data_flow(str(db), "f")
        assert _layers(result) == {"f": 0}


class TestEdgeCases:
    def test_idempotent_reindex(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", "def g(v):\n    return v\n\ndef f(x):\n    return g(x)\n")
        assert sylva.build_dataflow(str(db)) == 1
        assert sylva.build_dataflow(str(db)) == 1  # no duplicate rows
        assert len(_flows(db)) == 1

    def test_unknown_symbol_empty(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", "def f(x):\n    return x\n")
        sylva.build_dataflow(str(db))
        assert data_flow(str(db), "nope") == {"symbol": "nope", "nodes": [], "edges": []}

    def test_view_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            data_flow(str(tmp_path / "nope.db"), "f")

    def test_build_missing_db_raises(self, tmp_path):
        with pytest.raises(Exception):
            sylva.build_dataflow(str(tmp_path / "nope.db"))

    def test_parse_error_partial_not_crash(self, tmp_path):
        db = _init(tmp_path)
        # Valid function + trailing garbage: extraction is partial, build survives.
        _index(db, tmp_path / "m.py", "def g(v):\n    return v\n\ndef f(x):\n    return g(x)\n@@@\n")
        # Should not raise; the valid flow is still captured.
        sylva.build_dataflow(str(db))
        assert ("f", "g", "x") in _flows(db)


class TestDataflowEndpoint:
    def _serve(self, db):
        httpd = make_server(str(db), 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, httpd.server_address[1]

    def test_endpoint(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db,
            tmp_path / "m.py",
            "def h(x):\n    return x\n\ndef g(x):\n    return h(x)\n\ndef f(x):\n    return g(x)\n",
        )
        sylva.build_dataflow(str(db))
        httpd, port = self._serve(db)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/dataflow?symbol=f", timeout=5
            ) as r:
                body = json.loads(r.read())
            assert {n["name"] for n in body["nodes"]} == {"f", "g", "h"}
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_endpoint_missing_symbol_400(self, tmp_path):
        db = _init(tmp_path)
        httpd, port = self._serve(db)
        try:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/dataflow", timeout=5)
                assert False, "expected 400"
            except urllib.error.HTTPError as e:
                assert e.code == 400
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestUiAsset:
    def test_dataflow_controls_present(self):
        with open(os.path.join(srv.ASSETS_DIR, "index.html")) as f:
            html = f.read()
        assert "enterData" in html and "Data flow" in html and "dataflow" in html
