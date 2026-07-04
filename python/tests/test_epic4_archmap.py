"""Feature 4.7 — File/package-level architecture view (issue #48).

`module_map(db, level)` aggregates the graph to a top-level architecture map:
each file (level='file') or directory/package (level='package') is a node, with
cross-group call/import edges aggregated and weighted. Complements 4.6's
in-network file clustering as the coarser, dedicated map. Served via
`/architecture-map`.
"""

import json
import os
import threading
import urllib.error
import urllib.request

import pytest

import sylva
from sylva.viz import build_graph, make_server, module_map
import sylva.viz.server as srv


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    sylva.write_symbols(str(db), str(path), sylva.extract_symbols(str(path)))
    return path


def _labels(result):
    return {n["label"] for n in result["nodes"]}


class TestPackageLevel:
    def test_two_packages_with_cross_edge(self, tmp_path):
        db = _init(tmp_path)
        # pkg_a/ has two files; a() in one calls helper() in pkg_b/.
        _index(db, tmp_path / "pkg_a" / "m1.py", "def a():\n    return helper()\n")
        _index(db, tmp_path / "pkg_a" / "m2.py", "def extra():\n    return 1\n")
        _index(db, tmp_path / "pkg_b" / "u.py", "def helper():\n    return 2\n")
        sylva.build_edges(str(db))

        result = module_map(str(db), "package")
        assert result["level"] == "package"
        assert _labels(result) == {"pkg_a", "pkg_b"}
        # pkg_a groups two files; pkg_b one.
        by_label = {n["label"]: n for n in result["nodes"]}
        assert by_label["pkg_a"]["files"] == 2
        assert by_label["pkg_b"]["files"] == 1
        # One cross-package edge pkg_a -> pkg_b (a -> helper), weight 1.
        assert len(result["edges"]) == 1
        e = result["edges"][0]
        ids = {n["id"]: n["label"] for n in result["nodes"]}
        assert ids[e["source"]] == "pkg_a" and ids[e["target"]] == "pkg_b"
        assert e["weight"] == 1

    def test_symbol_counts_aggregate(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "pkg" / "m1.py", "def a():\n    return 1\n\ndef b():\n    return 2\n")
        _index(db, tmp_path / "pkg" / "m2.py", "def c():\n    return 3\n")
        sylva.build_edges(str(db))
        node = module_map(str(db), "package")["nodes"][0]
        assert node["symbols"] == 3 and node["files"] == 2


class TestFileLevel:
    def test_file_level_one_node_per_file_matches_46(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m1.py", "def a():\n    return 1\n")
        _index(db, tmp_path / "m2.py", "def b():\n    return 1\n")
        sylva.build_edges(str(db))

        result = module_map(str(db), "file")
        # One node per file; ids line up with 4.6's build_graph modules.
        map_ids = {n["id"] for n in result["nodes"]}
        clustered_ids = {m["id"] for m in build_graph(str(db))["modules"]}
        assert map_ids == clustered_ids
        assert all(n["files"] == 1 for n in result["nodes"])


class TestEdgeCases:
    def test_single_package_no_cross_edges(self, tmp_path):
        db = _init(tmp_path)
        # Two files in one dir with an intra-package call: one node, no edges.
        _index(db, tmp_path / "pkg" / "m1.py", "def a():\n    return b()\n")
        _index(db, tmp_path / "pkg" / "m2.py", "def b():\n    return 1\n")
        sylva.build_edges(str(db))
        result = module_map(str(db), "package")
        assert len(result["nodes"]) == 1
        assert result["edges"] == []  # intra-package edge collapses, no self-loop

    def test_empty_graph(self, tmp_path):
        db = _init(tmp_path)
        result = module_map(str(db), "package")
        assert result == {"level": "package", "nodes": [], "edges": []}

    def test_unknown_level_raises(self, tmp_path):
        db = _init(tmp_path)
        with pytest.raises(ValueError):
            module_map(str(db), "galaxy")

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            module_map(str(tmp_path / "nope.db"), "file")


class TestArchMapEndpoint:
    def _serve(self, db):
        httpd = make_server(str(db), 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, httpd.server_address[1]

    def test_endpoint_package_level(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "pkg_a" / "m.py", "def a():\n    return helper()\n")
        _index(db, tmp_path / "pkg_b" / "u.py", "def helper():\n    return 2\n")
        sylva.build_edges(str(db))
        httpd, port = self._serve(db)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/architecture-map?level=package", timeout=5
            ) as r:
                body = json.loads(r.read())
            assert {n["label"] for n in body["nodes"]} == {"pkg_a", "pkg_b"}
            assert body["level"] == "package"
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_endpoint_default_level_is_file(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", "def a():\n    return 1\n")
        sylva.build_edges(str(db))
        httpd, port = self._serve(db)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/architecture-map", timeout=5
            ) as r:
                body = json.loads(r.read())
            assert body["level"] == "file"
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_endpoint_bad_level_400(self, tmp_path):
        db = _init(tmp_path)
        httpd, port = self._serve(db)
        try:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/architecture-map?level=galaxy", timeout=5
                )
                assert False, "expected 400"
            except urllib.error.HTTPError as e:
                assert e.code == 400
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestUiAsset:
    def test_level_selector_present(self):
        with open(os.path.join(srv.ASSETS_DIR, "index.html")) as f:
            html = f.read()
        assert 'id="map-level"' in html and 'value="package"' in html
        assert "applyModuleMap" in html and "architecture-map" in html
