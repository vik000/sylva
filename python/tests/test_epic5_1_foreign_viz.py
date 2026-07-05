"""Feature 5.0.1 — Represent black-box foreign modules in the visualisation (#44).

The viz export tags foreign (Rust/PyO3) black-box nodes with a `foreign` flag
(data-layer, testable without rendering, like 4.5's `coverage_state`); the UI
renders them distinctly (amber squares) with a show/hide filter, so the
cross-language boundary from Feature 5.0 is visible and obviously opaque.
"""

import os

import pytest

import sylva
from sylva.viz import build_graph
import sylva.viz.server as srv


RS = (
    "use pyo3::prelude::*;\n\n"
    "#[pyfunction]\nfn accelerate(x: i64) -> i64 { x * 2 }\n\n"
    "#[pymodule]\nfn native(m: &Bound<'_, PyModule>) -> PyResult<()> {\n"
    "    m.add_function(wrap_pyfunction!(accelerate, m)?)?;\n    Ok(())\n}\n"
)


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


class TestForeignFlagInGraph:
    def test_foreign_nodes_flagged(self, tmp_path):
        db = _init(tmp_path)
        rs = tmp_path / "native.rs"
        rs.write_text(RS)
        sylva.write_symbols(str(db), str(rs), sylva.extract_foreign_exports(str(rs)))
        py = tmp_path / "app.py"
        py.write_text("def run(x):\n    return accelerate(x)\n")
        sylva.write_symbols(str(db), str(py), sylva.extract_symbols(str(py)))
        sylva.build_edges(str(db))

        nodes = {n["name"]: n for n in build_graph(str(db))["nodes"]}
        assert nodes["native"]["foreign"] is True
        assert nodes["accelerate"]["foreign"] is True
        assert nodes["run"]["foreign"] is False  # a real Python symbol

    def test_pure_python_graph_all_non_foreign(self, tmp_path):
        db = _init(tmp_path)
        py = tmp_path / "m.py"
        py.write_text("def a():\n    return 1\n")
        sylva.write_symbols(str(db), str(py), sylva.extract_symbols(str(py)))
        sylva.build_edges(str(db))
        assert all(n["foreign"] is False for n in build_graph(str(db))["nodes"])

    def test_cross_language_edge_in_graph(self, tmp_path):
        db = _init(tmp_path)
        rs = tmp_path / "native.rs"
        rs.write_text(RS)
        sylva.write_symbols(str(db), str(rs), sylva.extract_foreign_exports(str(rs)))
        py = tmp_path / "app.py"
        py.write_text("def run(x):\n    return accelerate(x)\n")
        sylva.write_symbols(str(db), str(py), sylva.extract_symbols(str(py)))
        sylva.build_edges(str(db))

        g = build_graph(str(db))
        ids = {n["id"]: n["name"] for n in g["nodes"]}
        pairs = {(ids[l["source"]], ids[l["target"]]) for l in g["links"]}
        assert ("run", "accelerate") in pairs  # the boundary edge is present


class TestUiAsset:
    def _html(self):
        with open(os.path.join(srv.ASSETS_DIR, "index.html")) as f:
            return f.read()

    def test_foreign_filter_control_present(self):
        html = self._html()
        assert 'id="k-foreign"' in html
        assert "showForeign" in html

    def test_distinct_foreign_styling(self):
        html = self._html()
        # A distinct colour for foreign kinds and a distinct (square) shape.
        assert "foreign_export" in html and "foreign_module" in html
        assert "n.foreign" in html  # shape/visibility branches on the flag
