"""View picker backend: the Classes view and the Layers (microservice) view.

`class_view` groups methods under their class + carries inheritance edges;
`layer_view` groups symbols by inferred architectural layer with cross-layer call
edges. Both are served as `/classes` and `/layers`.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

import sylva
from sylva.viz.export import class_view, layer_view


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, name, text, tmp_path):
    p = tmp_path / name
    p.write_text(text)
    sylva.write_symbols(str(db), str(p), sylva.extract_symbols(str(p)))
    return p


CLS = (
    "class Animal:\n"
    "    def speak(self):\n        return 1\n"
    "    def move(self):\n        return 2\n\n"
    "class Dog(Animal):\n"
    "    def speak(self):\n        return 3\n"
)


class TestClassView:
    def test_methods_grouped_and_inheritance(self, tmp_path):
        db = _init(tmp_path)
        _index(db, "m.py", CLS, tmp_path)
        sylva.build_edges(str(db))
        cv = class_view(str(db))
        by = {c["name"]: c for c in cv["classes"]}
        assert set(by) == {"Animal", "Dog"}
        assert {m["name"] for m in by["Animal"]["methods"]} == {"speak", "move"}
        assert {m["name"] for m in by["Dog"]["methods"]} == {"speak"}
        # Dog -> Animal inheritance edge (by id)
        aid, did = by["Animal"]["id"], by["Dog"]["id"]
        assert {"source": did, "target": aid} in cv["edges"]

    def test_no_classes_empty(self, tmp_path):
        db = _init(tmp_path)
        _index(db, "m.py", "def free():\n    return 1\n", tmp_path)
        sylva.build_edges(str(db))
        cv = class_view(str(db))
        assert cv["classes"] == [] and cv["edges"] == []

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            class_view(str(tmp_path / "nope.db"))


class TestLayerView:
    def test_library_single_tier(self, tmp_path):
        db = _init(tmp_path)
        _index(db, "m.py", CLS, tmp_path)
        sylva.build_edges(str(db))
        lv = layer_view(str(db))
        assert lv["archetype"] == "library"
        # a library collapses to the business tier
        assert [L["layer"] for L in lv["layers"]] == ["business"]
        assert all("endpoint" in s for L in lv["layers"] for s in L["symbols"])

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            layer_view(str(tmp_path / "nope.db"))


class _Server:
    def __init__(self, db):
        from sylva.viz.server import make_server
        self.httpd = make_server(db, 0)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class TestEndpoints:
    def test_classes_and_layers(self, tmp_path):
        db = _init(tmp_path)
        _index(db, "m.py", CLS, tmp_path)
        sylva.build_edges(str(db))
        with _Server(str(db)) as s:
            st, cv = s.get("/classes")
            assert st == 200 and {c["name"] for c in cv["classes"]} == {"Animal", "Dog"}
            st, lv = s.get("/layers")
            assert st == 200 and lv["archetype"] == "library"

    def test_missing_db_500(self, tmp_path):
        with _Server(str(tmp_path / "nope.db")) as s:
            st, body = s.get("/classes")
            assert st == 500 and "error" in body
