"""Feature 9.1.1 — Framework/entrypoint marker detection (issue #65).

Extends 9.1's entrypoint inference to recognise framework entrypoints that have
no `main()` / `__main__` guard: decorator-based routes (Flask/FastAPI) and CLI
commands (click/typer) via a generic decorator-method pattern, plus declarative
`console_scripts` in pyproject.toml / setup.cfg. Django URLconf and setup.py are
out of scope (deferred). Non-framework repos keep 9.1 behaviour.
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


def _markers(db):
    return {e["symbol"]: e["marker_kind"] for e in sylva.infer_entrypoints(str(db)) if e["is_marker"]}


class TestWebRoutes:
    def test_flask_route_flagged(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db,
            tmp_path / "app.py",
            "app = Flask(__name__)\n\n"
            "@app.route('/')\ndef index():\n    return 'hi'\n\n"
            "@app.route('/health')\ndef health():\n    return 'ok'\n",
        )
        sylva.build_edges(str(db))
        m = _markers(db)
        assert m.get("index") == "web_route"
        assert m.get("health") == "web_route"

    def test_fastapi_verbs_flagged(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db,
            tmp_path / "api.py",
            "app = FastAPI()\n\n"
            "@app.get('/items')\ndef list_items():\n    return []\n\n"
            "@app.post('/items')\ndef create_item():\n    return {}\n",
        )
        sylva.build_edges(str(db))
        m = _markers(db)
        assert m.get("list_items") == "web_route"
        assert m.get("create_item") == "web_route"


class TestCliCommands:
    def test_click_command_flagged(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db,
            tmp_path / "cli.py",
            "@click.command()\ndef run():\n    return 1\n\n"
            "@cli.group()\ndef top():\n    return 0\n",
        )
        sylva.build_edges(str(db))
        m = _markers(db)
        assert m.get("run") == "cli"
        assert m.get("top") == "cli"

    def test_bare_attribute_decorator(self, tmp_path):
        db = _init(tmp_path)
        # `@app.command` without a call still resolves the method name.
        _index(db, tmp_path / "t.py", "@app.command\ndef serve():\n    return 1\n")
        sylva.build_edges(str(db))
        assert _markers(db).get("serve") == "cli"


class TestConsoleScripts:
    def test_pyproject_project_scripts(self, tmp_path):
        db = _init(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'demo'\n\n[project.scripts]\ndemo = 'demo.cli:main_entry'\n"
        )
        _index(db, tmp_path / "demo" / "cli.py", "def main_entry():\n    return 1\n")
        sylva.build_edges(str(db))
        assert _markers(db).get("main_entry") == "console_script"

    def test_poetry_scripts(self, tmp_path):
        db = _init(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            "[tool.poetry.scripts]\ndemo = 'demo.app:start'\n"
        )
        _index(db, tmp_path / "demo" / "app.py", "def start():\n    return 1\n")
        sylva.build_edges(str(db))
        assert _markers(db).get("start") == "console_script"

    def test_setup_cfg_console_scripts(self, tmp_path):
        db = _init(tmp_path)
        (tmp_path / "setup.cfg").write_text(
            "[options.entry_points]\nconsole_scripts =\n    demo = demo.run:go\n"
        )
        _index(db, tmp_path / "demo" / "run.py", "def go():\n    return 1\n")
        sylva.build_edges(str(db))
        assert _markers(db).get("go") == "console_script"

    def test_console_script_disambiguated_by_module(self, tmp_path):
        db = _init(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            "[project.scripts]\ndemo = 'demo.real:handler'\n"
        )
        # Two `handler` defs; only the one in demo/real.py should be flagged.
        _index(db, tmp_path / "demo" / "real.py", "def handler():\n    return 1\n")
        _index(db, tmp_path / "demo" / "other.py", "def handler():\n    return 2\n")
        sylva.build_edges(str(db))
        entries = {(e["symbol"], e["file"]): e for e in sylva.infer_entrypoints(str(db))}
        flagged = [k for k, e in entries.items() if e["marker_kind"] == "console_script"]
        assert len(flagged) == 1
        assert flagged[0][1].endswith("real.py")


class TestPreserves91:
    def test_non_framework_repo_unchanged(self, tmp_path):
        db = _init(tmp_path)
        # No decorators, no config: only the 9.1 main-guard marker applies.
        _index(
            db,
            tmp_path / "app.py",
            "def work():\n    return 1\n\n"
            "def main():\n    return work()\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        sylva.build_edges(str(db))
        m = _markers(db)
        assert m.get("main") in ("main", "main_guard")
        assert "work" not in m  # a plain function is not misflagged

    def test_unrecognised_decorator_ignored(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db,
            tmp_path / "m.py",
            "@staticmethod\ndef util():\n    return 1\n\n"
            "@functools.cache\ndef memoized():\n    return 2\n",
        )
        sylva.build_edges(str(db))
        m = _markers(db)
        assert "util" not in m and "memoized" not in m  # not entrypoints


class TestPrimaryOnFrameworkApp:
    def test_route_can_be_primary_without_main(self, tmp_path):
        db = _init(tmp_path)
        # A Flask-style app with no main/__main__: a route is marker-boosted, so
        # the primary is a real entrypoint rather than an arbitrary leaf.
        _index(
            db,
            tmp_path / "app.py",
            "app = Flask(__name__)\n\n"
            "def _helper():\n    return 1\n\n"
            "@app.route('/')\ndef home():\n    return _helper()\n",
        )
        sylva.build_edges(str(db))
        result = sylva.infer_entrypoints(str(db))
        primary = next(e for e in result if e["primary"])
        assert primary["symbol"] == "home"
        assert primary["marker_kind"] == "web_route"
