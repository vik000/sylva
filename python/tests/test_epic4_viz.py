"""Feature 4.4 — Interactive visualisation UI (Python-native).

Covers the testable core: `build_graph` / `export_graph_json`, and the HTTP
server's routes and error behaviour (bound to an ephemeral port, hit with
urllib — no real browser). The Canvas front-end itself is not unit-tested.
"""

import json
import socket
import threading
import time
import urllib.request
import urllib.error

import pytest

import sylva
from sylva.viz import build_graph, export_graph_json, graph_version, make_server


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _seed(db, path="mod.py", n=None):
    if n is None:
        sylva.write_symbols(
            str(db), path,
            [
                {"name": "b", "kind": "function", "line": 1, "line_end": 2},
                {"name": "a", "kind": "function", "line": 4, "line_end": 5},
            ],
        )
    else:
        syms = [
            {"name": f"f{i}", "kind": "function", "line": i + 1, "line_end": i + 1}
            for i in range(n)
        ]
        sylva.write_symbols(str(db), path, syms)


def _get(port, path):
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")


class _Server:
    """Run make_server on an ephemeral port in a background thread."""

    def __init__(self, db_path):
        self.httpd = make_server(db_path, 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


# --- export / build_graph -------------------------------------------------- #

class TestBuildGraph:
    def test_nodes_and_links(self, tmp_path):
        db = _init(tmp_path)
        _seed(db)
        sylva.build_edges(str(db))  # a -> b would need a call; here just symbols
        g = build_graph(str(db))
        names = {n["name"] for n in g["nodes"]}
        assert names == {"a", "b"}
        assert "links" in g and isinstance(g["links"], list)

    def test_call_edge_becomes_link(self, tmp_path):
        db = _init(tmp_path)
        p = tmp_path / "mod.py"
        p.write_text("def b():\n    return 1\n\ndef a():\n    return b()\n")
        sylva.write_symbols(str(db), str(p), sylva.extract_symbols(str(p)))
        sylva.build_edges(str(db))
        g = build_graph(str(db))
        assert len(g["links"]) == 1
        # degree reflects the edge on both endpoints.
        by = {n["name"]: n for n in g["nodes"]}
        assert by["a"]["degree"] == 1 and by["b"]["degree"] == 1

    def test_node_fields(self, tmp_path):
        db = _init(tmp_path)
        _seed(db)
        node = build_graph(str(db))["nodes"][0]
        # `coverage_state` added by 4.5 (overlay); `foreign` added by 5.0.1.
        assert set(node.keys()) == {
            "id", "name", "kind", "file", "line", "coverage", "coverage_state",
            "degree", "foreign",
        }

    def test_empty_graph(self, tmp_path):
        db = _init(tmp_path)
        # modules/module_links added by Feature 4.6.
        assert build_graph(str(db)) == {
            "nodes": [], "links": [], "modules": [], "module_links": [],
        }

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            build_graph(str(tmp_path / "nope.db"))

    def test_export_writes_file(self, tmp_path):
        db = _init(tmp_path)
        _seed(db)
        out = export_graph_json(str(db), str(tmp_path / "visualisation"))
        assert out.endswith("graph.json")
        with open(out) as f:
            data = json.load(f)
        assert {n["name"] for n in data["nodes"]} == {"a", "b"}


# --- server ---------------------------------------------------------------- #

class TestServer:
    def test_index_html_loads(self, tmp_path):
        db = _init(tmp_path)
        _seed(db)
        with _Server(str(db)) as s:
            status, body, ctype = _get(s.port, "/")
            assert status == 200
            assert b"<canvas" in body
            assert "text/html" in ctype

    def test_graph_json_valid(self, tmp_path):
        db = _init(tmp_path)
        _seed(db)
        with _Server(str(db)) as s:
            status, body, ctype = _get(s.port, "/graph.json")
            assert status == 200
            assert "application/json" in ctype
            data = json.loads(body)
            assert {n["name"] for n in data["nodes"]} == {"a", "b"}

    def test_empty_graph_serves_ok(self, tmp_path):
        db = _init(tmp_path)
        with _Server(str(db)) as s:
            status, body, _ = _get(s.port, "/graph.json")
            assert status == 200
            assert json.loads(body) == {
                "nodes": [], "links": [], "modules": [], "module_links": [],
            }

    def test_unknown_path_404(self, tmp_path):
        db = _init(tmp_path)
        with _Server(str(db)) as s:
            status, body, _ = _get(s.port, "/nope")
            assert status == 404
            assert "error" in json.loads(body)

    def test_large_graph_loads_within_3s(self, tmp_path):
        db = _init(tmp_path)
        _seed(db, n=1200)  # 1000+ nodes
        with _Server(str(db)) as s:
            t0 = time.time()
            status, body, _ = _get(s.port, "/graph.json")
            elapsed = time.time() - t0
            assert status == 200
            assert len(json.loads(body)["nodes"]) == 1200
            assert elapsed < 3.0


class TestErrorControl:
    def test_missing_db_returns_500_json(self, tmp_path):
        missing = tmp_path / "nope.db"
        with _Server(str(missing)) as s:
            status, body, ctype = _get(s.port, "/graph.json")
            assert status == 500
            assert "application/json" in ctype
            assert "error" in json.loads(body)

    def test_port_in_use_raises(self, tmp_path):
        db = _init(tmp_path)
        # Occupy a port, then try to bind the same one.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        taken = sock.getsockname()[1]
        try:
            with pytest.raises(OSError):
                make_server(str(db), taken)
        finally:
            sock.close()


class TestLiveUpdate:
    def test_version_signature(self, tmp_path):
        db = _init(tmp_path)
        _seed(db)
        v = graph_version(str(db))
        assert "version" in v and isinstance(v["version"], str)

    def test_version_changes_when_graph_changes(self, tmp_path):
        db = _init(tmp_path)
        _seed(db)
        before = graph_version(str(db))["version"]
        sylva.write_symbols(
            str(db), "extra.py",
            [{"name": "c", "kind": "function", "line": 1, "line_end": 1}],
        )
        after = graph_version(str(db))["version"]
        assert before != after

    def test_version_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            graph_version(str(tmp_path / "nope.db"))

    def test_version_endpoint(self, tmp_path):
        db = _init(tmp_path)
        _seed(db)
        with _Server(str(db)) as s:
            status, body, ctype = _get(s.port, "/version")
            assert status == 200
            assert "application/json" in ctype
            assert "version" in json.loads(body)

    def test_version_endpoint_missing_db_500(self, tmp_path):
        with _Server(str(tmp_path / "nope.db")) as s:
            status, body, _ = _get(s.port, "/version")
            assert status == 500
            assert "error" in json.loads(body)


class TestCli:
    def test_serve_ui_subcommand_registered(self):
        import sylva.__main__ as cli

        # --db now defaults, so `serve-ui` alone would start the (blocking)
        # server. Verify the subcommand is registered without launching it, by
        # passing a bad --port so argparse exits before serving.
        with pytest.raises(SystemExit):
            cli.main(["serve-ui", "--port", "not-a-number"])

    def test_export_viz_cli_writes_file(self, tmp_path):
        import sylva.__main__ as cli

        db = _init(tmp_path)
        _seed(db)
        out_dir = tmp_path / "viz_out"
        rc = cli.main(["export-viz", "--db", str(db), "--out", str(out_dir)])
        assert rc == 0
        assert (out_dir / "graph.json").exists()

    def test_export_viz_cli_missing_db(self, tmp_path):
        import sylva.__main__ as cli

        rc = cli.main(["export-viz", "--db", str(tmp_path / "nope.db")])
        assert rc == 1
