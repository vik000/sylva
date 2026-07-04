"""Feature 4.6 — Module clustering & collapse/expand (issue #47).

build_graph aggregates symbols into per-file modules and collapses cross-module
symbol edges into weighted module edges — the data that powers the UI's
higher-level "module view". The colouring/interaction is data-driven so it's
testable without rendering (same approach as 4.5).
"""

import os

import sylva
from sylva.viz import build_graph


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    sylva.write_symbols(str(db), str(path), sylva.extract_symbols(str(path)))
    return path


def _modules_by_base(g):
    return {os.path.basename(m["id"]): m["symbols"] for m in g["modules"]}


class TestModuleAggregation:
    def test_modules_group_symbols_by_file(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "pkg" / "a.py", "def helper():\n    return 1\n")
        _index(
            db, tmp_path / "pkg" / "b.py",
            "from pkg.a import helper\n\ndef main():\n    return helper()\n",
        )
        sylva.build_edges(str(db))
        g = build_graph(str(db))

        # a.py: helper. b.py: the import binding + main.
        by = _modules_by_base(g)
        assert by == {"a.py": 1, "b.py": 2}

    def test_cross_module_edges_aggregated_with_weight(self, tmp_path):
        db = _init(tmp_path)
        a = _index(db, tmp_path / "pkg" / "a.py", "def helper():\n    return 1\n")
        b = _index(
            db, tmp_path / "pkg" / "b.py",
            "from pkg.a import helper\n\ndef main():\n    return helper()\n",
        )
        sylva.build_edges(str(db))
        g = build_graph(str(db))

        # b -> a via the import edge AND the call edge -> one module link, weight 2.
        assert len(g["module_links"]) == 1
        link = g["module_links"][0]
        assert link["source"] == str(b) and link["target"] == str(a)
        assert link["weight"] == 2

    def test_intra_module_edges_excluded(self, tmp_path):
        db = _init(tmp_path)
        # Both functions in one file; the call is intra-module -> no module link.
        _index(
            db, tmp_path / "solo.py",
            "def b():\n    return 1\n\ndef a():\n    return b()\n",
        )
        sylva.build_edges(str(db))
        g = build_graph(str(db))
        assert len(g["modules"]) == 1
        assert g["module_links"] == []

    def test_empty_graph(self, tmp_path):
        db = _init(tmp_path)
        g = build_graph(str(db))
        assert g["modules"] == [] and g["module_links"] == []

    def test_node_level_data_unchanged(self, tmp_path):
        # Module clustering is additive — the function-level graph still ships.
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", "def f():\n    return 1\n")
        g = build_graph(str(db))
        assert any(n["name"] == "f" for n in g["nodes"])
        assert "links" in g


class TestUiAsset:
    def test_module_view_toggle_present(self):
        import sylva.viz.server as srv

        with open(os.path.join(srv.ASSETS_DIR, "index.html")) as f:
            html = f.read()
        assert 'id="module-view"' in html
        assert "moduleView" in html
        assert "module_links" in html
