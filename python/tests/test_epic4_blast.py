"""Feature 4.2 — Blast radius analysis.

`sylva.blast_radius(db_path, symbol)` returns everything that depends on the
symbol (transitively), via inbound calls + imports edges. Each affected symbol:
{name, kind, file, line, distance, via}. The target itself is excluded.

Note: import edges are seeded directly here to exercise blast_radius's traversal
over the `imports` kind, independent of Feature 4.0's import-edge generation
(which is name-conflated for real imports — tracked in issue #37).
"""

import sqlite3

import pytest

import sylva


# util is called by a and b; main calls a; `importer` is a standalone symbol we
# link to util via a seeded imports edge.
LIB = (
    "def util():\n    return 1\n\n"
    "def a():\n    return util()\n\n"
    "def b():\n    return util()\n\n"
    "def main():\n    return a()\n\n"
    "def importer():\n    return 0\n"
)


def _build(tmp_path, files):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    for name, text in files.items():
        p = tmp_path / name
        p.write_text(text)
        sylva.write_symbols(str(db), str(p), sylva.extract_symbols(str(p)))
    sylva.build_edges(str(db))
    return db


def _seed_import_edge(db, src_name, dst_name):
    conn = sqlite3.connect(str(db))
    try:
        ids = dict(conn.execute("SELECT name, id FROM symbols").fetchall())
        conn.execute(
            "INSERT OR IGNORE INTO edges (src_id, dst_id, kind) VALUES (?, ?, 'imports')",
            (ids[src_name], ids[dst_name]),
        )
        conn.commit()
    finally:
        conn.close()


def _by_name(result):
    return {r["name"]: r for r in result}


class TestGeneral:
    def test_widely_used_returns_all_dependents(self, tmp_path):
        db = _build(tmp_path, {"lib.py": LIB})
        _seed_import_edge(db, "importer", "util")
        names = {r["name"] for r in sylva.blast_radius(str(db), "util")}
        # a, b call util; main calls a; importer imports util. util excluded.
        assert names == {"a", "b", "main", "importer"}
        assert "util" not in names

    def test_distances(self, tmp_path):
        db = _build(tmp_path, {"lib.py": LIB})
        _seed_import_edge(db, "importer", "util")
        by = _by_name(sylva.blast_radius(str(db), "util"))
        assert by["a"]["distance"] == 1
        assert by["b"]["distance"] == 1
        assert by["importer"]["distance"] == 1
        assert by["main"]["distance"] == 2  # main -> a -> util

    def test_via_edge_kind(self, tmp_path):
        db = _build(tmp_path, {"lib.py": LIB})
        _seed_import_edge(db, "importer", "util")
        by = _by_name(sylva.blast_radius(str(db), "util"))
        assert by["a"]["via"] == "calls"
        assert by["importer"]["via"] == "imports"

    def test_result_shape(self, tmp_path):
        db = _build(tmp_path, {"lib.py": LIB})
        result = sylva.blast_radius(str(db), "util")
        assert set(result[0].keys()) == {"name", "kind", "file", "line", "distance", "via"}


class TestEdge:
    def test_leaf_symbol_returns_empty(self, tmp_path):
        db = _build(tmp_path, {"lib.py": LIB})
        # Nothing calls main -> empty blast radius.
        assert sylva.blast_radius(str(db), "main") == []

    def test_self_recursion_returns_empty(self, tmp_path):
        # r calls itself; the only dependent is r (the target) -> excluded.
        db = _build(tmp_path, {"r.py": "def r():\n    return r()\n"})
        assert sylva.blast_radius(str(db), "r") == []

    def test_mutual_recursion_terminates(self, tmp_path):
        db = _build(
            tmp_path,
            {"cyc.py": "def x():\n    return y()\n\ndef y():\n    return x()\n"},
        )
        result = sylva.blast_radius(str(db), "x")
        # y depends on x; x depends on y but x is the target -> excluded. Finite.
        assert {r["name"] for r in result} == {"y"}


class TestNegative:
    def test_unknown_symbol_returns_empty(self, tmp_path):
        db = _build(tmp_path, {"lib.py": LIB})
        assert sylva.blast_radius(str(db), "ghost") == []
