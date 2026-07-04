"""Feature 7.8 — Import edge modeling (issue #37).

The extractor now retains each import's original name + source module, so:
  * aliased imports still resolve to the real definition,
  * import resolution prefers the named module when a name collides,
  * blast_radius starts from definitions (importing bindings show as dependents,
    not conflated start nodes),
  * call resolution is disambiguated by the caller file's imports.
"""

import sqlite3

import sylva


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path, text):
    path.write_text(text)
    sylva.write_symbols(str(db), str(path), sylva.extract_symbols(str(path)))
    return path


def _edges(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT s.name, s.kind, sf.path, d.name, d.kind, df.path, e.kind "
            "FROM edges e "
            "JOIN symbols s ON s.id = e.src_id JOIN files sf ON sf.id = s.file_id "
            "JOIN symbols d ON d.id = e.dst_id JOIN files df ON df.id = d.file_id"
        ).fetchall()
    finally:
        conn.close()


class TestExtractorImportFields:
    def test_aliased_import_retains_original_and_module(self, tmp_path):
        p = tmp_path / "m.py"
        p.write_text("from lib import util as u\n")
        syms = sylva.extract_symbols(str(p))
        imp = next(s for s in syms if s["kind"] == "import")
        assert imp["name"] == "u"            # bound name
        assert imp["import_name"] == "util"  # original name (would have been lost)
        assert imp["import_module"] == "lib"

    def test_plain_import_has_no_module(self, tmp_path):
        p = tmp_path / "m.py"
        p.write_text("import numpy as np\n")
        imp = next(s for s in sylva.extract_symbols(str(p)) if s["kind"] == "import")
        assert imp["name"] == "np" and imp["import_name"] == "numpy"
        assert imp["import_module"] is None


class TestAliasedImportEdges:
    def test_aliased_import_resolves_to_definition(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "lib.py", "def util():\n    return 1\n")
        _index(db, tmp_path / "app.py", "from lib import util as u\n")
        sylva.build_edges(str(db))
        imports = [
            (s, sk, d, dk) for (s, sk, sp, d, dk, dp, ek) in _edges(db) if ek == "imports"
        ]
        # Previously lost (binding 'u' != def 'util'); now resolved via original name.
        assert ("u", "import", "util", "function") in imports


class TestModuleDisambiguation:
    def test_prefers_named_module(self, tmp_path):
        db = _init(tmp_path)
        # `helper` defined in two packages; the import names one.
        _index(db, tmp_path / "pkg_a.py", "def helper():\n    return 1\n")
        _index(db, tmp_path / "pkg_b.py", "def helper():\n    return 2\n")
        _index(db, tmp_path / "app.py", "from pkg_a import helper as h\n")
        sylva.build_edges(str(db))
        imp = [
            dp for (s, sk, sp, d, dk, dp, ek) in _edges(db) if ek == "imports" and s == "h"
        ]
        assert len(imp) == 1
        assert imp[0] == str(tmp_path / "pkg_a.py")  # resolved to pkg_a, not pkg_b


class TestBlastRadiusConflation:
    def test_importer_appears_as_dependent(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "lib.py", "def util():\n    return 1\n")
        # Non-aliased: binding is also named 'util' (the conflation case).
        _index(db, tmp_path / "app.py", "from lib import util\n")
        sylva.build_edges(str(db))
        result = sylva.blast_radius(str(db), "util")
        # The importing binding shows up as an affected dependent, via imports —
        # not swallowed as a conflated start node.
        via_imports = [r for r in result if r["via"] == "imports"]
        assert len(via_imports) == 1
        assert via_imports[0]["file"] == str(tmp_path / "app.py")


class TestCallDisambiguation:
    def test_import_disambiguates_ambiguous_call(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "bar.py", "def b():\n    return 1\n")
        _index(db, tmp_path / "other.py", "def b():\n    return 2\n")  # global collision
        _index(
            db, tmp_path / "app.py",
            "from bar import b\n\ndef use():\n    return b()\n",
        )
        sylva.build_edges(str(db))
        calls = [
            (s, d, dp)
            for (s, sk, sp, d, dk, dp, ek) in _edges(db)
            if ek == "calls" and s == "use" and d == "b"
        ]
        # Without imports this would be ambiguous (skipped); the import resolves it.
        assert len(calls) == 1
        assert calls[0][2] == str(tmp_path / "bar.py")  # -> bar.b specifically
