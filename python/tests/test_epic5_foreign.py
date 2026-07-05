"""Feature 5.0 — Black-box foreign modules from the export surface (issue #43).

A foreign (Rust/PyO3) module is represented by its export surface only: a
`foreign_module` node + a `foreign_export` per `#[pyfunction]`. Internals are
not parsed. A Python call to an exported name resolves across the boundary to
its black-box entrypoint, so the cross-language edge is visible instead of a
dropped reference.
"""

import sqlite3

import pytest

import sylva


RS = (
    "use pyo3::prelude::*;\n\n"
    "#[pyfunction]\n"
    "fn accelerate(x: i64) -> i64 { x * 2 }\n\n"
    "#[pyfunction]\n"
    "fn crunch(data: &str) -> usize { data.len() }\n\n"
    "#[pymodule]\n"
    "fn native(m: &Bound<'_, PyModule>) -> PyResult<()> {\n"
    "    m.add_function(wrap_pyfunction!(accelerate, m)?)?;\n"
    "    m.add_function(wrap_pyfunction!(crunch, m)?)?;\n"
    "    Ok(())\n"
    "}\n"
)


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _symbols(db):
    conn = sqlite3.connect(str(db))
    try:
        return {(r[0], r[1]) for r in conn.execute("SELECT name, kind FROM symbols")}
    finally:
        conn.close()


def _call_edges(db):
    conn = sqlite3.connect(str(db))
    try:
        return {
            (s, d, dk)
            for (s, d, dk) in conn.execute(
                "SELECT s.name, d.name, d.kind FROM edges e "
                "JOIN symbols s ON s.id = e.src_id JOIN symbols d ON d.id = e.dst_id "
                "WHERE e.kind = 'calls'"
            )
        }
    finally:
        conn.close()


class TestExtractor:
    def test_extracts_module_and_exports(self, tmp_path):
        rs = tmp_path / "native.rs"
        rs.write_text(RS)
        syms = sylva.extract_foreign_exports(str(rs))
        kinds = {(s["name"], s["kind"]) for s in syms}
        assert ("native", "foreign_module") in kinds
        assert ("accelerate", "foreign_export") in kinds
        assert ("crunch", "foreign_export") in kinds

    def test_no_exports_is_empty_not_error(self, tmp_path):
        rs = tmp_path / "plain.rs"
        rs.write_text("fn helper() -> i64 { 42 }\n")  # no PyO3 attributes
        assert sylva.extract_foreign_exports(str(rs)) == []

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sylva.extract_foreign_exports(str(tmp_path / "nope.rs"))


class TestCrossBoundaryResolution:
    def _index(self, db, path, text, foreign=False):
        path.write_text(text)
        syms = (
            sylva.extract_foreign_exports(str(path))
            if foreign
            else sylva.extract_symbols(str(path))
        )
        sylva.write_symbols(str(db), str(path), syms)

    def test_python_call_resolves_to_foreign_export(self, tmp_path):
        db = _init(tmp_path)
        self._index(db, tmp_path / "native.rs", RS, foreign=True)
        self._index(
            db,
            tmp_path / "app.py",
            "def run(x):\n    return accelerate(x)\n",  # calls the exported Rust fn
        )
        sylva.build_edges(str(db))
        edges = _call_edges(db)
        # run -> accelerate, and the target is a black-box foreign_export.
        assert ("run", "accelerate", "foreign_export") in edges

    def test_foreign_symbols_present_not_dropped(self, tmp_path):
        db = _init(tmp_path)
        self._index(db, tmp_path / "native.rs", RS, foreign=True)
        syms = _symbols(db)
        assert ("native", "foreign_module") in syms
        assert ("accelerate", "foreign_export") in syms

    def test_foreign_file_not_parsed_for_calls(self, tmp_path):
        db = _init(tmp_path)
        # The .rs body references `wrap_pyfunction` etc.; none of that must
        # become call edges — foreign internals are opaque.
        self._index(db, tmp_path / "native.rs", RS, foreign=True)
        sylva.build_edges(str(db))
        srcs = {s for (s, _d, _k) in _call_edges(db)}
        assert "native" not in srcs and "accelerate" not in srcs  # no edges from Rust


class TestAnalyzePipeline:
    def test_analyze_black_boxes_rust(self, tmp_path, capsys):
        import sylva.__main__ as cli

        (tmp_path / "native.rs").write_text(RS)
        (tmp_path / "app.py").write_text("def run(x):\n    return accelerate(x)\n")
        rc = cli.main(["analyze", "--root", str(tmp_path), "--db", str(tmp_path / "g.db")])
        assert rc == 0
        out = capsys.readouterr().out
        assert "foreign export" in out
        # The cross-boundary edge is present after a full analyze.
        assert ("run", "accelerate", "foreign_export") in _call_edges(tmp_path / "g.db")

    def test_target_dir_skipped(self, tmp_path):
        # A .rs under target/ (build artefact) must be ignored by the walk.
        import sylva.__main__ as cli

        (tmp_path / "target").mkdir()
        (tmp_path / "target" / "build.rs").write_text(RS)
        (tmp_path / "app.py").write_text("def f():\n    return 1\n")
        cli.main(["analyze", "--root", str(tmp_path), "--db", str(tmp_path / "g.db")])
        assert ("native", "foreign_module") not in _symbols(tmp_path / "g.db")
