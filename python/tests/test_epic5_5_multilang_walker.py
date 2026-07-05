"""Feature 5.5 — Multi-language pipeline walker (issue #70).

`walk_source_files` finds every supported-language file; `index_codebase` routes
by extension — `.rs` black-boxed (5.0), everything else fully parsed (5.1 trait)
— so `analyze`/`onboard` now index Python, TypeScript, and JavaScript symbols
(not just `.py`). Non-Python symbols have no edges yet (that's 5.6).
"""

import sqlite3

import pytest

import sylva
from sylva.onboard import index_codebase
import sylva.__main__ as cli


def _symbols(db):
    conn = sqlite3.connect(str(db))
    try:
        return {(r[0], r[1]) for r in conn.execute("SELECT name, kind FROM symbols")}
    finally:
        conn.close()


class TestWalkSourceFiles:
    def test_finds_all_supported_languages(self, tmp_path):
        (tmp_path / "a.py").write_text("def f(): pass\n")
        (tmp_path / "b.ts").write_text("export function g() {}\n")
        (tmp_path / "c.js").write_text("function h() {}\n")
        (tmp_path / "d.rs").write_text("#[pyfunction]\nfn r() {}\n")
        (tmp_path / "readme.md").write_text("# not source\n")

        found = {p.rsplit("/", 1)[-1] for p in sylva.walk_source_files(str(tmp_path))}
        assert {"a.py", "b.ts", "c.js", "d.rs"} <= found
        assert "readme.md" not in found  # unsupported extension excluded

    def test_gitignore_respected(self, tmp_path):
        (tmp_path / "keep.ts").write_text("export function keep() {}\n")
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / "skip.ts").write_text("export function skip() {}\n")
        (tmp_path / ".gitignore").write_text("build/\n")
        found = {p.rsplit("/", 1)[-1] for p in sylva.walk_source_files(str(tmp_path))}
        assert "keep.ts" in found and "skip.ts" not in found

    def test_missing_root_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sylva.walk_source_files(str(tmp_path / "nope"))


class TestIndexCodebase:
    def test_indexes_typescript(self, tmp_path):
        (tmp_path / "app.ts").write_text(
            "export function greet() {}\nclass Widget {}\ninterface Shape {}\n"
        )
        db = tmp_path / ".codemcp" / "sylva.db"
        idx = index_codebase(str(tmp_path), str(db))
        syms = _symbols(db)
        assert ("greet", "function") in syms
        assert ("Widget", "class") in syms
        assert ("Shape", "interface") in syms
        assert idx["by_language"].get("typescript", 0) >= 3

    def test_mixed_repo_routing(self, tmp_path):
        (tmp_path / "svc.py").write_text("def py_fn():\n    return 1\n")
        (tmp_path / "web.ts").write_text("export function ts_fn() {}\n")
        (tmp_path / "native.rs").write_text(
            "#[pyfunction]\nfn accel() {}\n\n#[pymodule]\n"
            "fn native(m: &Bound<'_, PyModule>) -> PyResult<()> { Ok(()) }\n"
        )
        db = tmp_path / ".codemcp" / "sylva.db"
        idx = index_codebase(str(tmp_path), str(db))
        syms = _symbols(db)
        assert ("py_fn", "function") in syms       # Python full-parsed
        assert ("ts_fn", "function") in syms       # TS full-parsed
        assert ("accel", "foreign_export") in syms  # Rust black-boxed (not "function")
        assert ("native", "foreign_module") in syms
        assert idx["foreign"] >= 1
        assert {"python", "typescript"} <= set(idx["by_language"])

    def test_python_only_unchanged(self, tmp_path):
        # Regression: a Python repo indexes identically (with edges).
        (tmp_path / "m.py").write_text(
            "def helper():\n    return 1\n\ndef main():\n    return helper()\n"
        )
        db = tmp_path / ".codemcp" / "sylva.db"
        idx = index_codebase(str(tmp_path), str(db))
        assert idx["symbols"] == 2
        assert idx["edges"] >= 1  # Python call edge still built
        assert set(idx["by_language"]) == {"python"}


class TestCli:
    def test_analyze_reports_languages(self, tmp_path, capsys):
        (tmp_path / "a.py").write_text("def f():\n    return 1\n")
        (tmp_path / "b.ts").write_text("export function g() {}\n")
        rc = cli.main(["analyze", "--root", str(tmp_path), "--db", str(tmp_path / "g.db")])
        assert rc == 0
        out = capsys.readouterr().out
        assert "by language" in out
        assert "typescript" in out and "python" in out
