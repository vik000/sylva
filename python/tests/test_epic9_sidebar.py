"""Feature 9.5 — Sidebar navigator: launch flows from the navigator (issue #59).

Serves Feature 9.1's ranked inferred entrypoints at `/entrypoints`; the sidebar
lists them (primary badged) and clicking one launches its layered flow (9.4/4.10)
— making the Epic 9 structural views discoverable instead of buried behind node
selection.
"""

import json
import os
import threading
import urllib.error
import urllib.request

import pytest

import sylva
from sylva.viz import entrypoints, make_server
import sylva.viz.server as srv


APP = (
    "def c():\n    return 1\n\n"
    "def b():\n    return c()\n\n"
    "def main():\n    return b()\n\n"
    "if __name__ == '__main__':\n    main()\n"
)


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path, text):
    path.write_text(text)
    sylva.write_symbols(str(db), str(path), sylva.extract_symbols(str(path)))
    return path


class TestEntrypointsData:
    def test_ranked_with_primary(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "app.py", APP)
        sylva.build_edges(str(db))
        eps = entrypoints(str(db))
        assert isinstance(eps, list) and eps
        primary = [e for e in eps if e["primary"]]
        assert len(primary) == 1 and primary[0]["symbol"] == "main"
        assert all({"symbol", "rank", "primary", "marker_kind"} <= set(e) for e in eps)

    def test_empty_graph(self, tmp_path):
        db = _init(tmp_path)
        assert entrypoints(str(db)) == []

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            entrypoints(str(tmp_path / "nope.db"))


class TestEndpoint:
    def _serve(self, db):
        httpd = make_server(str(db), 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, httpd.server_address[1]

    def test_endpoint(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "app.py", APP)
        sylva.build_edges(str(db))
        httpd, port = self._serve(db)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/entrypoints", timeout=5
            ) as r:
                body = json.loads(r.read())
            assert any(e["symbol"] == "main" and e["primary"] for e in body)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_endpoint_missing_db_500(self, tmp_path):
        httpd = make_server(str(tmp_path / "nope.db"), 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        try:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/entrypoints", timeout=5)
                assert False, "expected 500"
            except urllib.error.HTTPError as e:
                assert e.code == 500
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestUiAsset:
    def test_entrypoints_launcher_present(self):
        with open(os.path.join(srv.ASSETS_DIR, "index.html")) as f:
            html = f.read()
        # Container + loader + click-to-flow wiring.
        assert 'id="nav-flows"' in html
        assert "loadEntrypoints" in html and "entrypoints" in html
        assert "enterFlow(e.symbol)" in html  # clicking launches the flow
