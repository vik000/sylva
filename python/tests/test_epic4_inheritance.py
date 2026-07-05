"""Inheritance edges — `class X(Base)` produces an `inherits` edge X → Base.

Extends the edge model (previously calls/imports/test_covers) with class
hierarchies, resolved like calls/imports (same-file, import-aware, dotted → last
segment) and never inventing a false edge. Inheritance feeds degree/hubs,
blast_radius (a subclass depends on its base), and renders in the viz.
"""

import sqlite3

import pytest

import sylva


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, name, text, tmp_path):
    p = tmp_path / name
    p.write_text(text)
    sylva.write_symbols(str(db), str(p), sylva.extract_symbols(str(p)))
    return p


def _inherits(db):
    """Return the set of (subclass_name, base_name) inheritance edges."""
    return set(
        sqlite3.connect(str(db)).execute(
            "SELECT s1.name, s2.name FROM edges e "
            "JOIN symbols s1 ON e.src_id = s1.id "
            "JOIN symbols s2 ON e.dst_id = s2.id "
            "WHERE e.kind = 'inherits'"
        ).fetchall()
    )


class TestBasic:
    def test_single_base(self, tmp_path):
        db = _init(tmp_path)
        _index(db, "m.py", "class A:\n    pass\n\nclass B(A):\n    pass\n", tmp_path)
        sylva.build_edges(str(db))
        assert ("B", "A") in _inherits(db)

    def test_multiple_bases(self, tmp_path):
        db = _init(tmp_path)
        _index(db, "m.py",
               "class A:\n    pass\n\nclass B:\n    pass\n\nclass C(A, B):\n    pass\n",
               tmp_path)
        sylva.build_edges(str(db))
        inh = _inherits(db)
        assert ("C", "A") in inh and ("C", "B") in inh

    def test_no_base_no_edge(self, tmp_path):
        db = _init(tmp_path)
        _index(db, "m.py", "class Standalone:\n    pass\n", tmp_path)
        sylva.build_edges(str(db))
        assert _inherits(db) == set()

    def test_dotted_base_last_segment(self, tmp_path):
        db = _init(tmp_path)
        # `class Sub(pkg.Base)` resolves by the last segment, like imports.
        _index(db, "m.py", "class Base:\n    pass\n\nclass Sub(mod.Base):\n    pass\n", tmp_path)
        sylva.build_edges(str(db))
        assert ("Sub", "Base") in _inherits(db)

    def test_metaclass_kwarg_ignored(self, tmp_path):
        db = _init(tmp_path)
        _index(db, "m.py", "class Meta(type):\n    pass\n\nclass A(metaclass=Meta):\n    pass\n", tmp_path)
        sylva.build_edges(str(db))
        # `metaclass=Meta` is a keyword arg, not a base — no A->Meta inherit edge.
        assert ("A", "Meta") not in _inherits(db)


class TestCrossFile:
    def test_imported_base(self, tmp_path):
        db = _init(tmp_path)
        _index(db, "base.py", "class Animal:\n    pass\n", tmp_path)
        _index(db, "dog.py", "from base import Animal\n\nclass Dog(Animal):\n    pass\n", tmp_path)
        sylva.build_edges(str(db))
        assert ("Dog", "Animal") in _inherits(db)

    def test_idempotent_reindex(self, tmp_path):
        db = _init(tmp_path)
        _index(db, "m.py", "class A:\n    pass\n\nclass B(A):\n    pass\n", tmp_path)
        sylva.build_edges(str(db))
        sylva.build_edges(str(db))                 # rebuild
        n = sqlite3.connect(str(db)).execute(
            "SELECT COUNT(*) FROM edges WHERE kind='inherits'").fetchone()[0]
        assert n == 1                               # not duplicated


class TestFeeds:
    def test_blast_radius_includes_subclass(self, tmp_path):
        db = _init(tmp_path)
        _index(db, "m.py", "class Base:\n    pass\n\nclass Sub(Base):\n    pass\n", tmp_path)
        sylva.build_edges(str(db))
        affected = sylva.blast_radius(str(db), "Base")
        hit = {(a["name"], a["via"]) for a in affected}
        assert ("Sub", "inherits") in hit           # changing Base affects Sub

    def test_degree_counts_inheritance(self, tmp_path):
        db = _init(tmp_path)
        _index(db, "m.py",
               "class Base:\n    pass\n\nclass A(Base):\n    pass\n\nclass B(Base):\n    pass\n",
               tmp_path)
        sylva.build_edges(str(db))
        hubs = {h["name"]: h["degree"] for h in sylva.get_architecture(str(db))["hubs"]}
        assert hubs.get("Base", 0) >= 2             # two subclasses inbound

    def test_viz_link_kind(self, tmp_path):
        from sylva.viz import build_graph
        db = _init(tmp_path)
        _index(db, "m.py", "class A:\n    pass\n\nclass B(A):\n    pass\n", tmp_path)
        sylva.build_edges(str(db))
        kinds = {l["kind"] for l in build_graph(str(db))["links"]}
        assert "inherits" in kinds                  # renders as a distinct link
