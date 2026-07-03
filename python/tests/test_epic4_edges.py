"""Feature 4.0 — Populate the edges table (call/import relationships).

`sylva.build_edges(db_path)` is a post-indexing pass: it clears existing
calls/imports edges, then writes `calls` edges (caller symbol -> callee
definition) and `imports` edges (import binding -> definition), resolving names
across the whole graph with ambiguous-skip.
"""

import json
import sqlite3

import pytest

import sylva


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path):
    """Index a real file on disk (walk->extract->write for that one file)."""
    sylva.write_symbols(str(db), str(path), sylva.extract_symbols(str(path)))


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _edges(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT s.name, s.kind, d.name, d.kind, e.kind FROM edges e "
            "JOIN symbols s ON s.id = e.src_id "
            "JOIN symbols d ON d.id = e.dst_id"
        ).fetchall()
    finally:
        conn.close()


def _call_edges(db):
    return {(s, d) for (s, sk, d, dk, ek) in _edges(db) if ek == "calls"}


class TestCallEdges:
    def test_simple_call(self, tmp_path):
        db = _init(tmp_path)
        f = _write(tmp_path, "mod.py", "def b():\n    return 1\n\ndef a():\n    return b()\n")
        _index(db, f)
        n = sylva.build_edges(str(db))
        assert ("a", "b") in _call_edges(db)
        assert n >= 1

    def test_recursion_self_edge(self, tmp_path):
        db = _init(tmp_path)
        f = _write(tmp_path, "r.py", "def rec(n):\n    return rec(n - 1)\n")
        _index(db, f)
        sylva.build_edges(str(db))
        assert ("rec", "rec") in _call_edges(db)

    def test_cross_file_call(self, tmp_path):
        db = _init(tmp_path)
        bar = _write(tmp_path, "bar.py", "def helper():\n    return 1\n")
        foo = _write(tmp_path, "foo.py", "def use():\n    return helper()\n")
        _index(db, bar)
        _index(db, foo)
        sylva.build_edges(str(db))
        assert ("use", "helper") in _call_edges(db)

    def test_method_call_by_name(self, tmp_path):
        db = _init(tmp_path)
        f = _write(
            tmp_path, "m.py",
            "def process():\n    return 1\n\ndef run(obj):\n    return obj.process()\n",
        )
        _index(db, f)
        sylva.build_edges(str(db))
        # obj.process() resolves to the process definition by attribute name.
        assert ("run", "process") in _call_edges(db)


class TestImportEdges:
    def test_import_binding_links_to_definition(self, tmp_path):
        db = _init(tmp_path)
        bar = _write(tmp_path, "bar.py", "def helper():\n    return 1\n")
        foo = _write(tmp_path, "foo.py", "from bar import helper\n")
        _index(db, bar)
        _index(db, foo)
        sylva.build_edges(str(db))
        imports = [(s, sk, d, dk) for (s, sk, d, dk, ek) in _edges(db) if ek == "imports"]
        # import binding 'helper' -> function definition 'helper'
        assert ("helper", "import", "helper", "function") in imports

    def test_import_of_external_module_skipped(self, tmp_path):
        db = _init(tmp_path)
        foo = _write(tmp_path, "foo.py", "import os\n")
        _index(db, foo)
        sylva.build_edges(str(db))
        # 'os' has no definition in the graph -> no import edge.
        assert all(ek != "imports" for (_, _, _, _, ek) in _edges(db))


class TestResolution:
    def test_unresolved_call_skipped(self, tmp_path):
        db = _init(tmp_path)
        # print() is a builtin, not an indexed symbol -> no edge.
        f = _write(tmp_path, "p.py", "def a():\n    print('hi')\n")
        _index(db, f)
        assert sylva.build_edges(str(db)) == 0
        assert _call_edges(db) == set()

    def test_ambiguous_call_skipped(self, tmp_path):
        db = _init(tmp_path)
        # Two definitions named 'dup' in different files -> a call to dup() is
        # ambiguous and skipped.
        a = _write(tmp_path, "a.py", "def dup():\n    return 1\n")
        b = _write(tmp_path, "b.py", "def dup():\n    return 2\n")
        caller = _write(tmp_path, "c.py", "def go():\n    return dup()\n")
        for f in (a, b, caller):
            _index(db, f)
        sylva.build_edges(str(db))
        assert ("go", "dup") not in _call_edges(db)


class TestIdempotencyAndIsolation:
    def test_rebuild_is_idempotent(self, tmp_path):
        db = _init(tmp_path)
        f = _write(tmp_path, "mod.py", "def b():\n    return 1\n\ndef a():\n    return b()\n")
        _index(db, f)
        first = sylva.build_edges(str(db))
        conn = sqlite3.connect(str(db))
        before = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        conn.close()
        sylva.build_edges(str(db))
        conn = sqlite3.connect(str(db))
        after = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        conn.close()
        assert before == after  # no duplicate edges on rebuild
        assert first >= 1

    def test_test_covers_edges_preserved(self, tmp_path):
        db = _init(tmp_path)
        f = _write(tmp_path, "mod.py", "def b():\n    return 1\n\ndef a():\n    return b()\n")
        _index(db, f)
        # Seed a test_covers edge; build_edges must not touch it.
        conn = sqlite3.connect(str(db))
        ids = dict(conn.execute("SELECT name, id FROM symbols").fetchall())
        conn.execute(
            "INSERT INTO edges (src_id, dst_id, kind) VALUES (?, ?, 'test_covers')",
            (ids["a"], ids["b"]),
        )
        conn.commit()
        conn.close()

        sylva.build_edges(str(db))

        kinds = {ek for (_, _, _, _, ek) in _edges(db)}
        assert "test_covers" in kinds
        assert "calls" in kinds


class TestIntegrationWithMcp:
    def test_get_callers_and_dependencies_return_real_data(self, tmp_path):
        # The whole point of 4.0: 1.6's get_callers/get_dependencies now work.
        db = _init(tmp_path)
        f = _write(tmp_path, "mod.py", "def b():\n    return 1\n\ndef a():\n    return b()\n")
        _index(db, f)
        sylva.build_edges(str(db))

        def call(method, name):
            req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {"name": name}}
            return json.loads(sylva.handle_request(str(db), json.dumps(req)))["result"]

        callers = {r["name"] for r in call("get_callers", "b")}
        deps = {r["name"] for r in call("get_dependencies", "a")}
        assert "a" in callers
        assert "b" in deps


class TestNegative:
    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(Exception):
            sylva.build_edges(str(tmp_path / "missing.db"))
