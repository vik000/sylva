"""Feature 9.7 — Entrypoint inference for libraries (no single main) (issue #67).

Libraries have no single `main`, so 9.1 would crown an arbitrary primary. 9.7
detects the library archetype (no run/web marker) and marks the **public API**
surface — public, non-`_`, top-level functions/classes — as the entrypoints,
flagging every entry `is_library`. Applications are unchanged.
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
    return {e["symbol"]: e for e in result}


class TestLibrary:
    def test_public_api_is_the_entrypoints(self, tmp_path):
        db = _init(tmp_path)
        # A library: public functions/classes, a private, a method — no main.
        _index(
            db,
            tmp_path / "lib.py",
            "def public_api():\n    return 1\n\n"
            "class Client:\n    def method(self):\n        return 2\n\n"
            "def _private_helper():\n    return 3\n",
        )
        sylva.build_edges(str(db))
        entries = _by_name(sylva.infer_entrypoints(str(db)))

        assert entries["public_api"]["marker_kind"] == "public_api"
        assert entries["public_api"]["is_library"] is True
        assert entries["Client"]["marker_kind"] == "public_api"  # a public class is API
        # A private and a method are NOT public API entrypoints.
        assert entries.get("_private_helper", {}).get("marker_kind") != "public_api"
        assert "method" not in entries or entries["method"]["marker_kind"] != "public_api"

    def test_primary_is_a_public_api_symbol(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db,
            tmp_path / "lib.py",
            "def leaf():\n    return 1\n\n"
            "def api():\n    return leaf()\n",   # api reaches more -> ranks first
        )
        sylva.build_edges(str(db))
        result = sylva.infer_entrypoints(str(db))
        primary = next(e for e in result if e["primary"])
        assert primary["marker_kind"] == "public_api"
        assert all(e["is_library"] for e in result)


class TestApplicationUnchanged:
    def test_app_with_main_not_library(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db,
            tmp_path / "app.py",
            "def work():\n    return 1\n\ndef main():\n    return work()\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        sylva.build_edges(str(db))
        result = sylva.infer_entrypoints(str(db))
        assert all(e["is_library"] is False for e in result)
        primary = next(e for e in result if e["primary"])
        assert primary["symbol"] == "main"  # 9.1 behaviour unchanged
        # Nothing is marked public_api (this is an application, not a library).
        assert all(e.get("marker_kind") != "public_api" for e in result)

    def test_web_app_not_library(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db,
            tmp_path / "api.py",
            "from flask import Flask\napp = Flask(__name__)\n\n"
            "@app.route('/')\ndef home():\n    return 'hi'\n",
        )
        sylva.build_edges(str(db))
        result = sylva.infer_entrypoints(str(db))
        assert all(e["is_library"] is False for e in result)  # web_route marker -> app


class TestEdges:
    def test_empty_graph(self, tmp_path):
        db = _init(tmp_path)
        assert sylva.infer_entrypoints(str(db)) == []

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sylva.infer_entrypoints(str(tmp_path / "nope.db"))
