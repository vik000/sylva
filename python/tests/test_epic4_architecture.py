"""Feature 4.3 — Architecture summary tool.

`sylva.get_architecture(db_path) -> {modules, hubs, entry_points}`:
  * modules      = [{path, symbols}] per file
  * hubs         = top-N symbols by total degree (in+out, calls+imports)
  * entry_points = function/class symbols nothing calls (excl. test_)
"""

import sqlite3

import pytest

import sylva


# util is called by a, b, c (hub). a is called by main. b, c, main call things
# but nothing calls them -> entry points.
LIB = (
    "def util():\n    return 1\n\n"
    "def a():\n    return util()\n\n"
    "def b():\n    return util()\n\n"
    "def c():\n    return util()\n\n"
    "def main():\n    return a()\n"
)


def _build(tmp_path, files):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        sylva.write_symbols(str(db), str(p), sylva.extract_symbols(str(p)))
    sylva.build_edges(str(db))
    return db


class TestGeneral:
    def test_top_hub(self, tmp_path):
        db = _build(tmp_path, {"lib.py": LIB})
        arch = sylva.get_architecture(str(db))
        top = arch["hubs"][0]
        assert top["name"] == "util"
        # util: inbound 3 (a,b,c) -> degree 3 (no outbound).
        assert top["degree"] == 3

    def test_entry_points(self, tmp_path):
        db = _build(tmp_path, {"lib.py": LIB})
        arch = sylva.get_architecture(str(db))
        names = {e["name"] for e in arch["entry_points"]}
        # Nothing calls b, c, main. util & a are called -> excluded.
        assert names == {"b", "c", "main"}
        assert "util" not in names and "a" not in names

    def test_modules(self, tmp_path):
        db = _build(tmp_path, {"lib.py": LIB})
        arch = sylva.get_architecture(str(db))
        mods = {m["path"]: m["symbols"] for m in arch["modules"]}
        assert len(mods) == 1
        assert next(iter(mods.values())) == 5  # util, a, b, c, main

    def test_shape(self, tmp_path):
        db = _build(tmp_path, {"lib.py": LIB})
        arch = sylva.get_architecture(str(db))
        assert set(arch.keys()) == {"modules", "hubs", "entry_points"}
        assert set(arch["hubs"][0].keys()) == {"name", "kind", "file", "degree"}
        assert set(arch["entry_points"][0].keys()) == {"name", "kind", "file"}

    def test_test_symbols_excluded_from_entry_points(self, tmp_path):
        db = _build(
            tmp_path,
            {"lib.py": LIB, "tests/test_it.py": "def test_x():\n    return 1\n"},
        )
        arch = sylva.get_architecture(str(db))
        names = {e["name"] for e in arch["entry_points"]}
        assert "test_x" not in names  # test_ symbols are not entry points

    def test_hub_limit(self, tmp_path):
        db = _build(tmp_path, {"lib.py": LIB})
        arch = sylva.get_architecture(str(db), 1)
        assert len(arch["hubs"]) == 1


class TestDegreeIncludesImports:
    def test_import_edge_counts_toward_degree(self, tmp_path):
        db = _build(tmp_path, {"lib.py": "def util():\n    return 1\n"})
        # Seed an import edge into util (a distinct symbol importing it).
        sylva.write_symbols(
            str(db), "other.py",
            [{"name": "consumer", "kind": "function", "line": 1, "line_end": 1}],
        )
        conn = sqlite3.connect(str(db))
        ids = dict(conn.execute("SELECT name, id FROM symbols").fetchall())
        conn.execute(
            "INSERT INTO edges (src_id, dst_id, kind) VALUES (?, ?, 'imports')",
            (ids["consumer"], ids["util"]),
        )
        conn.commit()
        conn.close()

        arch = sylva.get_architecture(str(db))
        by = {h["name"]: h["degree"] for h in arch["hubs"]}
        assert by["util"] == 1  # the imports edge counts


class TestEdge:
    def test_single_file_no_calls(self, tmp_path):
        db = _build(tmp_path, {"m.py": "def x():\n    return 1\n\ndef y():\n    return 2\n"})
        arch = sylva.get_architecture(str(db))
        assert arch["hubs"] == []  # no edges -> no hubs
        assert {e["name"] for e in arch["entry_points"]} == {"x", "y"}
        assert len(arch["modules"]) == 1

    def test_empty_db_returns_empty_sections(self, tmp_path):
        db = tmp_path / "sylva.db"
        sylva.init_db(str(db))
        arch = sylva.get_architecture(str(db))
        assert arch == {"modules": [], "hubs": [], "entry_points": []}


class TestNegative:
    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(Exception):
            sylva.get_architecture(str(tmp_path / "missing.db"))
