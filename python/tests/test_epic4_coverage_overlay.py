"""Feature 4.5 — Coverage overlay in visualisation.

The overlay colours nodes red->green by coverage, grey when there's no data.
Because Canvas can't be rendered in pytest, the colouring *decision* lives in
the data as a per-node `coverage_state` (from `coverage_state()` / build_graph),
which is what these tests assert. index.html just maps state -> colour.
"""

import sqlite3

import pytest

import sylva
from sylva.viz import build_graph, coverage_state


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _sym(name, line=1, line_end=1):
    return {"name": name, "kind": "function", "line": line, "line_end": line_end}


def _set_cov(db, name, pct):
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE symbols SET coverage_pct = ? WHERE name = ?", (pct, name))
        conn.commit()
    finally:
        conn.close()


class TestCoverageState:
    @pytest.mark.parametrize(
        "pct, state",
        [
            (None, "none"),
            (0.0, "uncovered"),
            (0, "uncovered"),
            (100.0, "covered"),
            (100, "covered"),
            (30.0, "low"),
            (49.9, "low"),
            (50.0, "partial"),
            (79.9, "partial"),
            (80.0, "high"),
            (99.9, "high"),
        ],
    )
    def test_banding(self, pct, state):
        assert coverage_state(pct) == state


class TestBuildGraphOverlay:
    def test_nodes_carry_coverage_state(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "m.py", [_sym("a"), _sym("b"), _sym("c")])
        _set_cov(db, "a", 100.0)  # covered
        _set_cov(db, "b", 0.0)    # uncovered
        # c stays NULL -> none
        by = {n["name"]: n for n in build_graph(str(db))["nodes"]}
        assert by["a"]["coverage_state"] == "covered"
        assert by["b"]["coverage_state"] == "uncovered"
        assert by["c"]["coverage_state"] == "none"

    def test_state_key_always_present(self, tmp_path):
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "m.py", [_sym("a")])
        node = build_graph(str(db))["nodes"][0]
        assert "coverage_state" in node

    def test_all_null_is_all_none(self, tmp_path):
        # The UI's "no coverage data" empty state triggers when every node is
        # 'none' — verify the data produces that condition.
        db = _init(tmp_path)
        sylva.write_symbols(str(db), "m.py", [_sym("a"), _sym("b")])
        states = {n["coverage_state"] for n in build_graph(str(db))["nodes"]}
        assert states == {"none"}

    def test_partial_data_mix_builds(self, tmp_path):
        # Some symbols measured, some null -> must build cleanly with mixed states.
        db = _init(tmp_path)
        sylva.write_symbols(
            str(db), "m.py",
            [_sym("a"), _sym("b"), _sym("c"), _sym("d")],
        )
        _set_cov(db, "a", 95.0)   # high
        _set_cov(db, "b", 60.0)   # partial
        _set_cov(db, "c", 10.0)   # low
        # d null -> none
        states = {n["name"]: n["coverage_state"] for n in build_graph(str(db))["nodes"]}
        assert states == {"a": "high", "b": "partial", "c": "low", "d": "none"}


class TestUiAssets:
    def test_index_html_has_coverage_toggle(self):
        # The overlay control and palette must be present in the shipped asset.
        import os
        import sylva.viz.server as srv

        with open(os.path.join(srv.ASSETS_DIR, "index.html")) as f:
            html = f.read()
        assert 'id="cov-mode"' in html
        assert "COV_COLOR" in html
        assert "coverage_state" in html
