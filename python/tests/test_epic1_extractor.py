"""Feature 1.4 — Python AST extractor.

Verifies `sylva.extract_symbols(path)` returns `{name, kind, line, docstring}`
dicts for functions, classes, and imports; handles empty/comment-only/nested
files; rejects missing and binary files; and returns partial results with an
error sentinel on parse failure instead of crashing.
"""

import pytest

import sylva


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def _by_name(symbols):
    # Later definitions with the same name would clobber; the sample files
    # below use unique names so this is unambiguous.
    return {s["name"]: s for s in symbols}


class TestGeneral:
    def test_extracts_function_class_import(self, tmp_path):
        src = (
            "import os\n"
            "from sys import argv\n"
            "\n"
            "def greet(name):\n"
            '    """Say hello."""\n'
            "    return name\n"
            "\n"
            "class Widget:\n"
            '    """A widget."""\n'
            "    def render(self):\n"
            "        pass\n"
        )
        p = _write(tmp_path, "sample.py", src)
        syms = sylva.extract_symbols(str(p))
        by = _by_name(syms)

        kinds = {s["name"]: s["kind"] for s in syms}
        assert kinds["greet"] == "function"
        assert kinds["Widget"] == "class"
        assert kinds["render"] == "function"
        assert kinds["os"] == "import"
        assert kinds["argv"] == "import"

        # No error sentinel on a clean parse.
        assert all(s["kind"] != "error" for s in syms)

    def test_dict_keys_and_types(self, tmp_path):
        p = _write(tmp_path, "k.py", "def f():\n    pass\n")
        syms = sylva.extract_symbols(str(p))
        assert len(syms) == 1
        s = syms[0]
        # `line_end` was added by Feature 7.1 (issue #28).
        assert set(s.keys()) == {"name", "kind", "line", "line_end", "docstring"}
        assert isinstance(s["line"], int)

    def test_line_numbers_are_one_based(self, tmp_path):
        # `def f` is on the third line (1-based).
        p = _write(tmp_path, "l.py", "\n\ndef f():\n    pass\n")
        s = _by_name(sylva.extract_symbols(str(p)))["f"]
        assert s["line"] == 3

    def test_docstring_extracted_and_absent(self, tmp_path):
        src = (
            "def documented():\n"
            '    """the doc"""\n'
            "    pass\n"
            "\n"
            "def bare():\n"
            "    pass\n"
        )
        p = _write(tmp_path, "d.py", src)
        by = _by_name(sylva.extract_symbols(str(p)))
        assert by["documented"]["docstring"].strip() == "the doc"
        assert by["bare"]["docstring"] is None


class TestEdge:
    def test_empty_file(self, tmp_path):
        p = _write(tmp_path, "empty.py", "")
        assert sylva.extract_symbols(str(p)) == []

    def test_comments_only(self, tmp_path):
        p = _write(tmp_path, "c.py", "# just a comment\n# another\n")
        assert sylva.extract_symbols(str(p)) == []

    def test_nested_classes_and_methods(self, tmp_path):
        src = (
            "class Outer:\n"
            "    class Inner:\n"
            "        def method(self):\n"
            "            pass\n"
        )
        p = _write(tmp_path, "n.py", src)
        syms = sylva.extract_symbols(str(p))
        by = {s["name"]: s["kind"] for s in syms}
        assert by["Outer"] == "class"
        assert by["Inner"] == "class"
        assert by["method"] == "function"

    def test_aliased_import_uses_bound_name(self, tmp_path):
        p = _write(tmp_path, "a.py", "import numpy as np\nfrom os import path as p\n")
        names = {s["name"] for s in sylva.extract_symbols(str(p)) if s["kind"] == "import"}
        assert "np" in names
        assert "p" in names


class TestNegative:
    def test_missing_file_raises_filenotfound(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sylva.extract_symbols(str(tmp_path / "nope.py"))

    def test_binary_file_raises_clear_error(self, tmp_path):
        p = tmp_path / "bin.py"
        # Invalid UTF-8 bytes — a stand-in for a binary file.
        p.write_bytes(b"\xff\xfe\x00\x01\x80\x81 def f(): pass")
        with pytest.raises(Exception) as exc:
            sylva.extract_symbols(str(p))
        # Not a FileNotFoundError — a distinct, clear decode error.
        assert not isinstance(exc.value, FileNotFoundError)


class TestErrorControl:
    def test_parse_error_returns_partial_with_flag(self, tmp_path):
        # `good` parses cleanly; `bad` has a syntax error.
        src = (
            "def good():\n"
            "    pass\n"
            "\n"
            "def bad(:\n"
            "    pass\n"
        )
        p = _write(tmp_path, "broken.py", src)
        syms = sylva.extract_symbols(str(p))

        names = {s["name"] for s in syms}
        assert "good" in names  # partial results preserved

        errors = [s for s in syms if s["kind"] == "error"]
        assert len(errors) == 1  # exactly one error sentinel
        assert errors[0]["line"] >= 1
