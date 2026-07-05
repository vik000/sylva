"""Feature 5.1 — Pluggable language extractor trait (issue #20).

`extract_symbols` now dispatches by file extension through a language-extractor
registry; `list_languages()` reports what's registered. Adding a language is
implementing one extractor. The Python extractor works exactly as before; an
unsupported extension returns [] (not an error); the dispatch is panic-safe.
"""

import pytest

import sylva


def _write(tmp_path, name, text, binary=False):
    p = tmp_path / name
    p.write_bytes(text) if binary else p.write_text(text)
    return str(p)


class TestPythonUnchanged:
    def test_python_extractor_still_works(self, tmp_path):
        path = _write(
            tmp_path,
            "m.py",
            "import os\n\nclass A:\n    def m(self):\n        '''doc'''\n        return 1\n\n"
            "def top():\n    return 2\n",
        )
        syms = sylva.extract_symbols(path)
        kinds = {(s["name"], s["kind"]) for s in syms}
        assert ("A", "class") in kinds
        assert ("m", "function") in kinds
        assert ("top", "function") in kinds
        assert ("os", "import") in kinds
        # The shared dict shape is preserved.
        assert set(syms[0]) == {
            "name", "kind", "line", "line_end", "docstring", "import_module", "import_name",
        }

    def test_pyi_extension_supported(self, tmp_path):
        path = _write(tmp_path, "stubs.pyi", "def f() -> int: ...\n")
        assert any(s["name"] == "f" for s in sylva.extract_symbols(path))


class TestRegistry:
    def test_list_languages(self):
        langs = sylva.list_languages()
        assert "python" in langs

    def test_registration_idempotent(self):
        # A fixed registry: each language appears exactly once (no duplicates).
        langs = sylva.list_languages()
        assert len(langs) == len(set(langs))
        assert langs.count("python") == 1


class TestUnsupportedExtension:
    def test_unknown_extension_returns_empty(self, tmp_path):
        # A real file of an unsupported language → [] (not parsed as Python).
        path = _write(tmp_path, "notes.txt", "def looks_like_python(): pass\n")
        assert sylva.extract_symbols(path) == []

    def test_unregistered_language_returns_empty(self, tmp_path):
        # A language with no registered extractor (e.g. Go) → [] (not an error).
        # (Python is 5.1; Rust was added in 5.2.)
        path = _write(tmp_path, "main.go", "package main\nfunc main() {}\n")
        assert sylva.extract_symbols(path) == []
        assert "go" not in sylva.list_languages()


class TestRobustness:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sylva.extract_symbols(str(tmp_path / "nope.py"))

    def test_binary_python_file_raises(self, tmp_path):
        path = _write(tmp_path, "bin.py", b"\x00\x01\x02\xff", binary=True)
        with pytest.raises(ValueError):
            sylva.extract_symbols(path)

    def test_parse_errors_return_sentinel_not_crash(self, tmp_path):
        # Malformed Python → partial results + an error sentinel, never a crash.
        path = _write(tmp_path, "bad.py", "def ok():\n    return 1\n\ndef broken(:\n")
        syms = sylva.extract_symbols(path)
        assert any(s["kind"] == "error" for s in syms)
        assert any(s["name"] == "ok" for s in syms)  # partial results preserved
