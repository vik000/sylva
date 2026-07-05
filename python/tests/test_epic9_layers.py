"""Feature 9.8 — Project archetype + architectural layer inference (issue #68).

Infers the project archetype (library / application / service) and tags each
symbol with an architectural layer (interface / transport / data / business)
from the module families its file imports (+ 9.1.1 route/CLI markers).
Deterministic, cheap curated subset; ambiguous → business default.
"""

import pytest

import sylva


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    sylva.write_symbols(str(db), str(path), sylva.extract_symbols(str(path)))
    return path


def _layers(result):
    return {e["symbol"]: e["layer"] for e in result["layers"]}


class TestServiceLayers:
    def test_layers_by_import_family(self, tmp_path):
        db = _init(tmp_path)
        # A Flask app: routes (interface), a model (data), an http client (transport),
        # and a plain helper (business).
        _index(
            db,
            tmp_path / "api.py",
            "from flask import Flask\napp = Flask(__name__)\n\n"
            "@app.route('/')\ndef home():\n    return 'hi'\n",
        )
        _index(
            db,
            tmp_path / "models.py",
            "import sqlalchemy\n\ndef load_user():\n    return 1\n",
        )
        _index(
            db,
            tmp_path / "client.py",
            "import httpx\n\ndef fetch():\n    return 2\n",
        )
        _index(db, tmp_path / "util.py", "def compute():\n    return 3\n")
        sylva.build_edges(str(db))

        result = sylva.infer_layers(str(db))
        assert result["archetype"] == "service"
        layers = _layers(result)
        assert layers["home"] == "interface"
        assert layers["load_user"] == "data"
        assert layers["fetch"] == "transport"
        assert layers["compute"] == "business"

    def test_route_marker_makes_interface_without_direct_framework_import(self, tmp_path):
        db = _init(tmp_path)
        # The route decorator marks the file as interface even though the app is
        # imported indirectly (no top-level flask/fastapi import here).
        _index(
            db,
            tmp_path / "routes.py",
            "from .app import app\n\n@app.route('/x')\ndef handler():\n    return 1\n",
        )
        # Make it a service overall via a framework import elsewhere.
        _index(db, tmp_path / "app.py", "from fastapi import FastAPI\napp = FastAPI()\n")
        sylva.build_edges(str(db))
        result = sylva.infer_layers(str(db))
        assert result["archetype"] == "service"
        assert _layers(result)["handler"] == "interface"


class TestArchetype:
    def test_library(self, tmp_path):
        db = _init(tmp_path)
        # Plain functions, no framework, nothing runs it -> library.
        _index(db, tmp_path / "lib.py", "def api_a():\n    return 1\n\ndef api_b():\n    return 2\n")
        sylva.build_edges(str(db))
        result = sylva.infer_layers(str(db))
        assert result["archetype"] == "library"
        # No forced tiers: everything defaults to business.
        assert set(_layers(result).values()) == {"business"}

    def test_application(self, tmp_path):
        db = _init(tmp_path)
        # A CLI/main app with no web/queue framework -> application.
        _index(
            db,
            tmp_path / "cli.py",
            "def work():\n    return 1\n\ndef main():\n    return work()\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        sylva.build_edges(str(db))
        assert sylva.infer_layers(str(db))["archetype"] == "application"


class TestEdgeCases:
    def test_ambiguous_defaults_to_business(self, tmp_path):
        db = _init(tmp_path)
        # Imports an unknown third-party pkg -> not classified, business default.
        _index(db, tmp_path / "m.py", "import some_unknown_pkg\n\ndef go():\n    return 1\n")
        sylva.build_edges(str(db))
        assert _layers(sylva.infer_layers(str(db)))["go"] == "business"

    def test_empty_graph(self, tmp_path):
        db = _init(tmp_path)
        result = sylva.infer_layers(str(db))
        assert result["layers"] == []
        assert result["archetype"] == "library"  # nothing => library

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sylva.infer_layers(str(tmp_path / "nope.db"))


class TestMcpSurface:
    def _rpc(self, db, obj):
        import json

        return json.loads(sylva.handle_request(str(db), json.dumps(obj)))

    def test_in_tools_list(self, tmp_path):
        db = _init(tmp_path)
        resp = self._rpc(db, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert "infer_layers" in {t["name"] for t in resp["result"]["tools"]}

    def test_tool_dispatch(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "api.py", "from flask import Flask\n\ndef home():\n    return 1\n")
        sylva.build_edges(str(db))
        resp = self._rpc(db, {"jsonrpc": "2.0", "id": 2, "method": "infer_layers", "params": {}})
        assert resp["result"]["archetype"] == "service"
