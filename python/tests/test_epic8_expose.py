"""Feature 8.5 — expose selected repo functions as an executable MCP server (#69).

`generate_server` reads a language-neutral allowlist and emits a standalone,
dependency-free MCP server exposing ONLY the listed functions (Python execution
backend: import + live signature). Sylva generates; the user runs it.
"""

import json
import sys

import pytest

import sylva
from sylva.expose import generate_server, parse_allowlist
import sylva.__main__ as cli


MYMOD = (
    "def add(a: int, b: int) -> int:\n    '''Add two numbers.'''\n    return a + b\n\n"
    "def secret():\n    return 'nope'\n"
)
UTILPKG = (
    "def helper():\n    '''A helper.'''\n    return 1\n\n"
    "def _private():\n    return 2\n\n"
    "class Thing:\n    def method(self):\n        return 3\n"
)


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    sylva.write_symbols(str(db), str(path), sylva.extract_symbols(str(path)))


def _exec_server(code, root):
    """Exec generated server code with `root` importable; return its namespace.
    Cleans up sys.path and the imported target modules."""
    added = str(root)
    sys.path.insert(0, added)
    dirty = set(sys.modules)
    ns = {}
    try:
        exec(compile(code, "<generated>", "exec"), ns)
    finally:
        if added in sys.path:
            sys.path.remove(added)
        for m in set(sys.modules) - dirty:
            sys.modules.pop(m, None)
    return ns


class TestGenerate:
    def test_only_allowlisted_exposed(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "mymod.py", MYMOD)
        _index(db, tmp_path / "utilpkg.py", UTILPKG)
        allowlist = {"functions": ["mymod:add"], "modules": ["utilpkg"]}
        code, count, warnings = generate_server(str(db), str(tmp_path), allowlist)

        assert count == 2 and not warnings
        assert "from mymod import add as" in code
        assert "from utilpkg import helper as" in code
        # The exposed set is exactly {add, helper} — not the unlisted `secret`,
        # the private `_private`, or the class method `method`.
        ns = _exec_server(code, tmp_path)
        assert set(ns["TOOLS"]) == {"add", "helper"}
        assert "from mymod import secret" not in code
        assert "_private" not in code

    def test_generated_server_is_valid_and_callable(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "mymod.py", MYMOD)
        code, _, _ = generate_server(str(db), str(tmp_path), {"functions": ["mymod:add"]})
        compile(code, "<gen>", "exec")  # valid Python

        ns = _exec_server(code, tmp_path)
        assert set(ns["TOOLS"]) == {"add"}
        # tools/call actually executes the function.
        resp = json.loads(ns["handle"](
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "add", "arguments": {"a": 2, "b": 3}}}
        ))
        assert resp["result"]["isError"] is False
        assert json.loads(resp["result"]["content"][0]["text"]) == 5

    def test_schema_from_signature(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "mymod.py", MYMOD)
        code, _, _ = generate_server(str(db), str(tmp_path), {"functions": ["mymod:add"]})
        ns = _exec_server(code, tmp_path)
        tl = json.loads(ns["handle"]({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        add_tool = next(t for t in tl["result"]["tools"] if t["name"] == "add")
        assert set(add_tool["inputSchema"]["properties"]) == {"a", "b"}
        assert add_tool["inputSchema"]["properties"]["a"]["type"] == "integer"  # from annotation
        assert set(add_tool["inputSchema"]["required"]) == {"a", "b"}

    def test_nested_package_import_path(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "pkg" / "sub" / "mod.py", "def deep():\n    return 9\n")
        code, count, _ = generate_server(str(db), str(tmp_path), {"functions": ["pkg.sub.mod:deep"]})
        assert count == 1
        assert "from pkg.sub.mod import deep as" in code


class TestAllowlistParsing:
    def test_parse_toml_arrays(self):
        text = 'functions = [\n  "a.b:c",\n  "d:e",\n]\nmodules = ["m.n"]\n'
        al = parse_allowlist(text)
        assert al["functions"] == ["a.b:c", "d:e"]
        assert al["modules"] == ["m.n"]

    def test_missing_keys(self):
        assert parse_allowlist("") == {"functions": [], "modules": []}


class TestEdgeCases:
    def test_empty_allowlist_safe_server(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "mymod.py", MYMOD)
        code, count, _ = generate_server(str(db), str(tmp_path), {"functions": [], "modules": []})
        assert count == 0
        ns = _exec_server(code, tmp_path)
        assert ns["TOOLS"] == {}  # a safe, empty server — no crash

    def test_unknown_function_warned_not_fatal(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "mymod.py", MYMOD)
        code, count, warnings = generate_server(
            str(db), str(tmp_path), {"functions": ["mymod:nope", "mymod:add"]}
        )
        assert count == 1  # add exposed
        assert any("nope" in w for w in warnings)  # missing one warned

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            generate_server(str(tmp_path / "nope.db"), str(tmp_path), {"functions": []})


class TestCli:
    def test_expose_command(self, tmp_path, capsys):
        db = tmp_path / ".codemcp" / "sylva.db"
        db.parent.mkdir(parents=True)
        sylva.init_db(str(db))
        _index(db, tmp_path / "mymod.py", MYMOD)
        (tmp_path / ".codemcp" / "expose.toml").write_text('functions = ["mymod:add"]\n')

        rc = cli.main(["expose", "--root", str(tmp_path)])
        assert rc == 0
        server = tmp_path / ".codemcp" / "functions_server.py"
        assert server.is_file()
        assert "from mymod import add as" in server.read_text()
        assert "1 tool" in capsys.readouterr().out

    def test_expose_no_allowlist_empty_server(self, tmp_path, capsys):
        db = tmp_path / ".codemcp" / "sylva.db"
        db.parent.mkdir(parents=True)
        sylva.init_db(str(db))
        _index(db, tmp_path / "mymod.py", MYMOD)
        rc = cli.main(["expose", "--root", str(tmp_path)])  # no expose.toml
        assert rc == 0
        assert (tmp_path / ".codemcp" / "functions_server.py").is_file()
