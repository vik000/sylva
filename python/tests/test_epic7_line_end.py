"""Feature 7.1 — Populate symbols.line_end (tracks issue #28).

The extractor now emits `line_end` (1-based end line) alongside `line` (start),
and the graph writer persists it to `symbols.line_end`. This gives span-based
features (Feature 3.2 coverage correlation, visualisation) a real boundary.
"""

import sqlite3

import pytest

import sylva


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def _by_name(symbols):
    return {s["name"]: s for s in symbols}


# --- extractor emits line_end ---------------------------------------------- #

class TestExtractor:
    def test_multiline_function_span(self, tmp_path):
        src = (
            "def wide():\n"      # line 1
            "    x = 1\n"        # line 2
            "    return x\n"     # line 3
        )
        f = _write(tmp_path, "m.py", src)
        s = _by_name(sylva.extract_symbols(str(f)))["wide"]
        assert s["line"] == 1
        assert s["line_end"] == 3

    def test_single_line_def_start_equals_end(self, tmp_path):
        f = _write(tmp_path, "s.py", "def tiny(): return 1\n")
        s = _by_name(sylva.extract_symbols(str(f)))["tiny"]
        assert s["line"] == s["line_end"] == 1

    def test_nested_defs_get_own_spans(self, tmp_path):
        src = (
            "class Outer:\n"        # 1
            "    def method(self):\n"  # 2
            "        pass\n"        # 3
            "    x = 0\n"           # 4
        )
        f = _write(tmp_path, "n.py", src)
        by = _by_name(sylva.extract_symbols(str(f)))
        assert by["Outer"]["line"] == 1 and by["Outer"]["line_end"] == 4
        assert by["method"]["line"] == 2 and by["method"]["line_end"] == 3

    def test_import_line_end_present(self, tmp_path):
        f = _write(tmp_path, "i.py", "import os\n")
        s = _by_name(sylva.extract_symbols(str(f)))["os"]
        assert s["line"] == 1 and s["line_end"] == 1

    def test_all_symbols_have_line_end_key(self, tmp_path):
        src = "import os\n\ndef f():\n    pass\n\nclass C:\n    pass\n"
        f = _write(tmp_path, "a.py", src)
        for s in sylva.extract_symbols(str(f)):
            assert "line_end" in s


# --- writer persists line_end ---------------------------------------------- #

class TestWriter:
    def test_line_end_persisted(self, tmp_path):
        db = tmp_path / "sylva.db"
        sylva.init_db(str(db))
        symbols = [
            {"name": "wide", "kind": "function", "line": 1, "line_end": 3, "docstring": None},
        ]
        sylva.write_symbols(str(db), "m.py", symbols)

        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT line_start, line_end FROM symbols WHERE name = 'wide'"
            ).fetchone()
        finally:
            conn.close()
        assert row == (1, 3)

    def test_missing_line_end_stored_null(self, tmp_path):
        # Backward compatibility: a dict without line_end still writes (NULL).
        db = tmp_path / "sylva.db"
        sylva.init_db(str(db))
        sylva.write_symbols(str(db), "x.py", [{"name": "f", "kind": "function", "line": 5}])

        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT line_start, line_end FROM symbols WHERE name = 'f'"
            ).fetchone()
        finally:
            conn.close()
        assert row == (5, None)

    def test_end_to_end_extract_then_write(self, tmp_path):
        # Full path: extract a real file, write it, and confirm the span landed.
        db = tmp_path / "sylva.db"
        sylva.init_db(str(db))
        src = "def go():\n    a = 1\n    return a\n"
        f = _write(tmp_path, "go.py", src)
        sylva.write_symbols(str(db), str(f), sylva.extract_symbols(str(f)))

        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT line_start, line_end FROM symbols WHERE name = 'go'"
            ).fetchone()
        finally:
            conn.close()
        assert row == (1, 3)
