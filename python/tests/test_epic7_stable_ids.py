"""Stable symbol ids across re-indexing (the real fix for lost logic paths).

`write_symbols` upserts by identity `(name, kind, line_start)` instead of
delete-then-insert, so an unchanged symbol keeps its id on re-analyze. That means
the rows referencing it by id — `test_covers` edges (logic paths), `call_trace`
(runtime traces), and `coverage_pct` — survive a re-analyze instead of being
cascade-deleted or orphaned.
"""

import sqlite3

import sylva


TWO = "def target():\n    return 1\n\ndef test_it():\n    return target()\n"


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _write(db, path, text, tmp_path):
    p = tmp_path / path
    p.write_text(text)
    sylva.write_symbols(str(db), str(p), sylva.extract_symbols(str(p)))
    return str(p)


def _ids(db):
    return dict(sqlite3.connect(str(db)).execute(
        "SELECT name, id FROM symbols").fetchall())


class TestIdStability:
    def test_ids_unchanged_on_reindex(self, tmp_path):
        db = _init(tmp_path)
        p = _write(db, "m.py", TWO, tmp_path)
        before = _ids(db)
        sylva.write_symbols(str(db), p, sylva.extract_symbols(p))  # re-index, same content
        assert _ids(db) == before                                  # same ids

    def test_test_covers_survives_reindex(self, tmp_path):
        db = _init(tmp_path)
        p = _write(db, "m.py", TWO, tmp_path)
        ids = _ids(db)
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO edges (src_id, dst_id, kind) VALUES (?, ?, 'test_covers')",
                     (ids["test_it"], ids["target"]))
        conn.commit(); conn.close()
        sylva.write_symbols(str(db), p, sylva.extract_symbols(p))   # re-analyze
        n = sqlite3.connect(str(db)).execute(
            "SELECT COUNT(*) FROM edges WHERE kind='test_covers'").fetchone()[0]
        assert n == 1                                                # logic path survived

    def test_coverage_preserved(self, tmp_path):
        db = _init(tmp_path)
        p = _write(db, "m.py", TWO, tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE symbols SET coverage_pct=100 WHERE name='target'")
        conn.commit(); conn.close()
        sylva.write_symbols(str(db), p, sylva.extract_symbols(p))
        cov = sqlite3.connect(str(db)).execute(
            "SELECT coverage_pct FROM symbols WHERE name='target'").fetchone()[0]
        assert cov == 100.0                                          # not wiped


class TestChanges:
    def test_removed_symbol_deleted_others_kept(self, tmp_path):
        db = _init(tmp_path)
        p = _write(db, "m.py", TWO, tmp_path)
        before = _ids(db)
        # Re-write with only `target` (test_it removed).
        _write(db, "m.py", "def target():\n    return 1\n", tmp_path)
        after = _ids(db)
        assert "test_it" not in after
        assert after["target"] == before["target"]                  # kept id stable

    def test_added_symbol_others_kept(self, tmp_path):
        db = _init(tmp_path)
        _write(db, "m.py", "def target():\n    return 1\n", tmp_path)
        before = _ids(db)
        _write(db, "m.py", TWO, tmp_path)                            # add test_it
        after = _ids(db)
        assert after["target"] == before["target"]                  # original preserved
        assert "test_it" in after                                    # new one added

    def test_same_name_methods_distinct(self, tmp_path):
        db = _init(tmp_path)
        # Two `__init__` methods in one file: distinct line_start -> distinct ids,
        # both stable across re-index (no collision from the identity key).
        src = ("class A:\n    def __init__(self):\n        pass\n\n"
               "class B:\n    def __init__(self):\n        pass\n")
        p = _write(db, "m.py", src, tmp_path)
        n_before = sqlite3.connect(str(db)).execute(
            "SELECT COUNT(*) FROM symbols WHERE name='__init__'").fetchone()[0]
        sylva.write_symbols(str(db), p, sylva.extract_symbols(p))
        n_after = sqlite3.connect(str(db)).execute(
            "SELECT COUNT(*) FROM symbols WHERE name='__init__'").fetchone()[0]
        assert n_before == 2 and n_after == 2                        # no dup, no loss
