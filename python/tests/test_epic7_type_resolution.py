"""Feature 7.10 — Type-aware method call resolution (issue #45).

Extends name-based call resolution with light, conservative receiver type
inference: `self.method()` resolves to the enclosing class's method, and
`x = Foo(); x.method()` resolves to `Foo.method` — recovering edges that were
previously skipped as ambiguous when several classes share a method name. Never
invents a false edge: an unknowable receiver stays skipped.
"""

import sqlite3

import pytest

import sylva


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path, text):
    path.write_text(text)
    sylva.write_symbols(str(db), str(path), sylva.extract_symbols(str(path)))
    return path


def _call_edges(db):
    """Return {(src_name, dst_name)} for calls, disambiguating dst by file+line."""
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT s.name, d.name, d.line_start FROM edges e "
            "JOIN symbols s ON s.id = e.src_id JOIN symbols d ON d.id = e.dst_id "
            "WHERE e.kind = 'calls'"
        ).fetchall()
    finally:
        conn.close()
    return rows


def _dst_lines(db, src_name, dst_name):
    """The line_start(s) of the resolved dst for a given src->dst call."""
    return sorted(
        line for (s, d, line) in _call_edges(db) if s == src_name and d == dst_name
    )


class TestSelfResolution:
    def test_self_method_resolves_to_enclosing_class(self, tmp_path):
        db = _init(tmp_path)
        # Two classes define `run`; A.caller calls self.run() -> must be A.run,
        # not B.run, and must NOT be dropped as ambiguous.
        src = (
            "class A:\n"
            "    def run(self):\n"          # line 2
            "        return 1\n\n"
            "    def caller(self):\n"
            "        return self.run()\n\n"  # -> A.run (line 2)
            "class B:\n"
            "    def run(self):\n"          # line 9
            "        return 2\n"
        )
        _index(db, tmp_path / "m.py", src)
        sylva.build_edges(str(db))
        # caller -> run resolved (was ambiguous under name-only) and points at A.run (line 2).
        assert _dst_lines(db, "caller", "run") == [2]

    def test_cls_method_resolves(self, tmp_path):
        db = _init(tmp_path)
        src = (
            "class A:\n"
            "    def make(cls):\n"
            "        return 1\n\n"
            "    def factory(cls):\n"
            "        return cls.make()\n\n"
            "class B:\n"
            "    def make(self):\n"
            "        return 2\n"
        )
        _index(db, tmp_path / "m.py", src)
        sylva.build_edges(str(db))
        assert _dst_lines(db, "factory", "make") == [2]  # A.make, not B.make


class TestLocalBinding:
    def test_local_instance_resolves(self, tmp_path):
        db = _init(tmp_path)
        # x = Foo(); x.bar() -> Foo.bar, even though Baz also defines bar.
        src = (
            "class Foo:\n"
            "    def bar(self):\n"          # line 2
            "        return 1\n\n"
            "class Baz:\n"
            "    def bar(self):\n"          # line 6
            "        return 2\n\n"
            "def use():\n"
            "    x = Foo()\n"
            "    return x.bar()\n"          # -> Foo.bar (line 2)
        )
        _index(db, tmp_path / "m.py", src)
        sylva.build_edges(str(db))
        assert _dst_lines(db, "use", "bar") == [2]

    def test_conflicting_bindings_not_guessed(self, tmp_path):
        db = _init(tmp_path)
        # x bound to two different classes -> ambiguous, no false edge.
        src = (
            "class Foo:\n"
            "    def bar(self):\n"
            "        return 1\n\n"
            "class Baz:\n"
            "    def bar(self):\n"
            "        return 2\n\n"
            "def use(flag):\n"
            "    x = Foo()\n"
            "    x = Baz()\n"
            "    return x.bar()\n"
        )
        _index(db, tmp_path / "m.py", src)
        sylva.build_edges(str(db))
        assert _dst_lines(db, "use", "bar") == []  # not guessed


class TestNoFalseEdges:
    def test_unknown_receiver_stays_skipped(self, tmp_path):
        db = _init(tmp_path)
        # obj is a parameter of unknown type; obj.bar() must NOT resolve.
        src = (
            "class Foo:\n"
            "    def bar(self):\n"
            "        return 1\n\n"
            "class Baz:\n"
            "    def bar(self):\n"
            "        return 2\n\n"
            "def use(obj):\n"
            "    return obj.bar()\n"
        )
        _index(db, tmp_path / "m.py", src)
        sylva.build_edges(str(db))
        assert _dst_lines(db, "use", "bar") == []  # receiver type unknown -> skip

    def test_self_method_not_in_class_falls_through(self, tmp_path):
        db = _init(tmp_path)
        # self.helper() where the class has no `helper`, but a unique module
        # function `helper` exists -> name-based fallback still resolves it.
        src = (
            "def helper():\n"               # line 1
            "    return 0\n\n"
            "class A:\n"
            "    def go(self):\n"
            "        return self.helper()\n"  # falls back to module helper (line 1)
        )
        _index(db, tmp_path / "m.py", src)
        sylva.build_edges(str(db))
        assert _dst_lines(db, "go", "helper") == [1]


class TestRegression:
    def test_plain_calls_unaffected(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db,
            tmp_path / "m.py",
            "def c():\n    return 1\n\ndef b():\n    return c()\n\ndef a():\n    return b()\n",
        )
        sylva.build_edges(str(db))
        pairs = {(s, d) for (s, d, _l) in _call_edges(db)}
        assert ("a", "b") in pairs and ("b", "c") in pairs

    def test_reindex_idempotent(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db,
            tmp_path / "m.py",
            "class A:\n    def run(self):\n        return 1\n\n"
            "    def go(self):\n        return self.run()\n",
        )
        n1 = sylva.build_edges(str(db))
        n2 = sylva.build_edges(str(db))
        assert n1 == n2  # no duplicate edges on re-run
