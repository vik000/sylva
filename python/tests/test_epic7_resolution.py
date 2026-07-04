"""Feature 7.7 — Type-aware-ish edge resolution: same-file preference (#36).

When a call name is globally ambiguous but exactly one definition lives in the
caller's own file, resolve to that one (Python module-scope semantics) instead
of skipping. Recovers local calls without inventing false edges.
"""

import sqlite3

import sylva


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path, text):
    p = path
    p.write_text(text)
    sylva.write_symbols(str(db), str(p), sylva.extract_symbols(str(p)))
    return p


def _call_edges_with_dst_file(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT s.name, d.name, df.path FROM edges e "
            "JOIN symbols s ON s.id = e.src_id "
            "JOIN symbols d ON d.id = e.dst_id "
            "JOIN files df ON df.id = d.file_id "
            "WHERE e.kind = 'calls'"
        ).fetchall()
    finally:
        conn.close()


class TestSameFilePreference:
    def test_resolves_to_same_file_definition(self, tmp_path):
        db = _init(tmp_path)
        # `get` is defined in two files -> globally ambiguous.
        _index(db, tmp_path / "other.py", "def get():\n    return 1\n")
        main = _index(
            db, tmp_path / "main.py",
            "def get():\n    return 0\n\ndef caller():\n    return get()\n",
        )
        sylva.build_edges(str(db))

        # caller's get() must resolve to main.py's get (same file), not other.py's.
        calls = [
            (s, d, path)
            for (s, d, path) in _call_edges_with_dst_file(db)
            if s == "caller" and d == "get"
        ]
        assert len(calls) == 1
        assert calls[0][2] == str(main)

    def test_no_same_file_stays_ambiguous(self, tmp_path):
        db = _init(tmp_path)
        # `dup` defined in two files; caller's file defines neither -> ambiguous.
        _index(db, tmp_path / "a.py", "def dup():\n    return 1\n")
        _index(db, tmp_path / "b.py", "def dup():\n    return 2\n")
        _index(db, tmp_path / "c.py", "def go():\n    return dup()\n")
        sylva.build_edges(str(db))
        assert not any(
            s == "go" and d == "dup" for (s, d, _p) in _call_edges_with_dst_file(db)
        )

    def test_unique_global_still_resolves(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "lib.py", "def helper():\n    return 1\n")
        _index(db, tmp_path / "use.py", "def u():\n    return helper()\n")
        sylva.build_edges(str(db))
        assert any(
            s == "u" and d == "helper" for (s, d, _p) in _call_edges_with_dst_file(db)
        )

    def test_local_call_recovered_despite_global_collision(self, tmp_path):
        db = _init(tmp_path)
        # Many other files define `run`; the caller file has its own `run`.
        for i in range(3):
            _index(db, tmp_path / f"m{i}.py", "def run():\n    return 0\n")
        local = _index(
            db, tmp_path / "app.py",
            "def run():\n    return 1\n\ndef start():\n    return run()\n",
        )
        sylva.build_edges(str(db))
        calls = [
            (s, d, path)
            for (s, d, path) in _call_edges_with_dst_file(db)
            if s == "start" and d == "run"
        ]
        assert len(calls) == 1
        assert calls[0][2] == str(local)  # resolved locally, not skipped
