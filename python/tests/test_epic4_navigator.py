"""Feature 4.8 — Sidebar navigator (entry points, hubs, logic paths) (issue #49).

The viz sidebar becomes a navigator: it lists the codebase's entry points and
hubs so the graph is browsable rather than free-roam only. The data is a thin
reuse of Feature 4.3 `get_architecture` (no reimplementation), served over an
`/architecture` endpoint and rendered as clickable, click-to-focus lists.
"""

import json
import os
import threading
import urllib.error
import urllib.request

import pytest

import sylva
from sylva.viz import architecture, make_server
import sylva.viz.server as srv


# c (1-2) <- b (4-5) <- a (7-8): a calls b, b calls c. `a` is called by nothing.
CHAIN = (
    "def c():\n    return 1\n\n"
    "def b():\n    return c()\n\n"
    "def a():\n    return b()\n"
)


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path, text):
    path.write_text(text)
    sylva.write_symbols(str(db), str(path), sylva.extract_symbols(str(path)))
    return path


class TestArchitectureData:
    def test_returns_hubs_and_entry_points(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        sylva.build_edges(str(db))

        arch = architecture(str(db))
        assert set(arch) >= {"modules", "hubs", "entry_points"}
        # `a` is called by nothing -> an entry point; `b`/`c` are not.
        entry_names = {e["name"] for e in arch["entry_points"]}
        assert "a" in entry_names and "b" not in entry_names and "c" not in entry_names
        # `b` sits in the middle (2 call edges) -> the top hub.
        hub_names = [h["name"] for h in arch["hubs"]]
        assert "b" in hub_names
        assert all("degree" in h for h in arch["hubs"])

    def test_empty_graph_empty_lists(self, tmp_path):
        db = _init(tmp_path)
        arch = architecture(str(db))
        assert arch["hubs"] == []
        assert arch["entry_points"] == []
        assert arch["modules"] == []

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            architecture(str(tmp_path / "nope.db"))


class TestArchitectureEndpoint:
    def _serve(self, db):
        httpd = make_server(str(db), 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, httpd.server_address[1]

    def test_architecture_endpoint(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        sylva.build_edges(str(db))

        httpd, port = self._serve(db)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/architecture", timeout=5
            ) as r:
                body = json.loads(r.read())
            assert {e["name"] for e in body["entry_points"]} == {"a"}
            assert "b" in {h["name"] for h in body["hubs"]}
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_architecture_missing_db_returns_500(self, tmp_path):
        # A server pointed at a non-existent db: the endpoint reports 500, not a crash.
        httpd = make_server(str(tmp_path / "nope.db"), 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        try:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/architecture", timeout=5
                )
                assert False, "expected 500"
            except urllib.error.HTTPError as e:
                assert e.code == 500
                assert "error" in json.loads(e.read())
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestUiAsset:
    def test_navigator_controls_present(self):
        with open(os.path.join(srv.ASSETS_DIR, "index.html")) as f:
            html = f.read()
        # Sidebar navigator containers + the wiring that populates and uses them.
        assert 'id="nav-entries"' in html and 'id="nav-hubs"' in html
        assert "loadArchitecture" in html and "focusNode" in html
        assert "architecture" in html  # fetches the /architecture endpoint
