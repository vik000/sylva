"""Feature 8.3 — Represent accessible functions in the visualisation (issue #42).

`build_graph` marks each node `accessible` when the symbol is exposed via
Feature 8.5's `expose.toml` (sitting next to the db) — the agent-callable
surface. Reuses 8.5's resolution (methods/privates excluded, module shorthand
honored). The UI badges accessible nodes and offers an "accessible only" filter.
"""

import os

import pytest

import sylva
from sylva.viz import build_graph
import sylva.viz.server as srv


API = "def exposed_fn():\n    return 1\n\ndef hidden_fn():\n    return 2\n"
UTIL = (
    "def helper():\n    return 1\n\n"
    "def _private():\n    return 2\n\n"
    "class Thing:\n    def method(self):\n        return 3\n"
)


def _setup(tmp_path, allowlist_text=None):
    codemcp = tmp_path / ".codemcp"
    codemcp.mkdir()
    db = codemcp / "sylva.db"
    sylva.init_db(str(db))
    for name, text in (("api.py", API), ("util.py", UTIL)):
        p = tmp_path / name
        p.write_text(text)
        sylva.write_symbols(str(db), str(p), sylva.extract_symbols(str(p)))
    sylva.build_edges(str(db))
    if allowlist_text is not None:
        (codemcp / "expose.toml").write_text(allowlist_text)
    return db


def _acc(db):
    return {n["name"]: n["accessible"] for n in build_graph(str(db))["nodes"]}


class TestAccessibleFlag:
    def test_marks_exposed_functions(self, tmp_path):
        db = _setup(tmp_path, 'functions = ["api:exposed_fn"]\nmodules = ["util"]\n')
        acc = _acc(db)
        assert acc["exposed_fn"] is True             # explicit function
        assert acc["helper"] is True                 # module shorthand (public)
        assert acc["hidden_fn"] is False             # not listed
        assert acc.get("_private", False) is False   # private excluded
        # A method is not exposed by module shorthand.
        assert acc.get("method", False) is False

    def test_no_allowlist_all_false(self, tmp_path):
        db = _setup(tmp_path, allowlist_text=None)  # no expose.toml
        assert all(v is False for v in _acc(db).values())

    def test_mod_fn_matches_by_module(self, tmp_path):
        db = _setup(tmp_path, 'functions = ["util:helper"]\n')
        acc = _acc(db)
        assert acc["helper"] is True
        assert acc["exposed_fn"] is False  # a different module's fn not exposed

    def test_every_node_has_accessible_key(self, tmp_path):
        db = _setup(tmp_path, 'functions = ["api:exposed_fn"]\n')
        assert all("accessible" in n for n in build_graph(str(db))["nodes"])


class TestUiAsset:
    def test_badge_and_filter_present(self):
        with open(os.path.join(srv.ASSETS_DIR, "index.html")) as f:
            html = f.read()
        assert 'id="accessible-only"' in html      # the filter toggle
        assert "accessibleOnly" in html            # state + visibility gate
        assert "n.accessible" in html              # badge rendering
