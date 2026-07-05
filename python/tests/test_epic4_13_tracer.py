"""Runtime call tracer — real ordered execution flows (the dynamic tier, #54).

`sylva.tracer.Recorder` (a `sys.setprofile` hook, shipped as a pytest plugin)
records the *actual order* functions call each other while the project's own
tests run. `sylva.tracing` resolves those events to graph symbols, persists an
ordered `call_trace` table, and serves it as a top-down succession the viz draws
as boxes. This is genuine runtime data — the true "test → A → B → C", not a
coverage-derived set.
"""

import json
import os
import sqlite3
import sys
import threading
import urllib.error
import urllib.request

import pytest

import sylva
from sylva import tracing
from sylva.tracer import Recorder


# --- Recorder: captures real call order ------------------------------------ #

class TestRecorder:
    def test_records_ordered_chain(self):
        root = os.path.dirname(__file__)  # these nested defs live here

        def c():
            return 1

        def b():
            return c()

        def a():
            return b()

        rec = Recorder(root)
        sys.setprofile(rec)
        try:
            a()
        finally:
            sys.setprofile(None)

        seen = [ev[7] for ev in rec.events if ev[7] in ("a", "b", "c")]
        assert seen == ["a", "b", "c"]                 # real call order
        # depth increases down the chain.
        depth = {ev[7]: ev[1] for ev in rec.events if ev[7] in ("a", "b", "c")}
        assert depth["a"] < depth["b"] < depth["c"]

    def test_skips_foreign_and_synthetic(self):
        rec = Recorder("/nonexistent-root")   # nothing is "own" code
        sys.setprofile(rec)
        try:
            [x for x in range(3)]             # a listcomp + builtins
        finally:
            sys.setprofile(None)
        assert rec.events == []


# --- a graph + a hand-built trace ------------------------------------------ #

SRC = "def target():\n    return 1\n\ndef helper():\n    return target()\n"
TST = "def test_x():\n    return 1\n"


def _repo(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    src = tmp_path / "src.py"
    src.write_text(SRC)
    tst = tmp_path / "test_src.py"
    tst.write_text(TST)
    for p in (src, tst):
        sylva.write_symbols(str(db), str(p), sylva.extract_symbols(str(p)))
    sylva.build_edges(str(db))
    return db, src, tst


def _trace_file(tmp_path, src, tst):
    # test_x -> helper -> target, in order (seq, depth, caller_f, caller_l,
    # caller_n, callee_f, callee_l, callee_n).
    events = [
        [0, 0, "", 0, "", str(tst), 1, "test_x"],
        [1, 1, str(tst), 1, "test_x", str(src), 4, "helper"],
        [2, 2, str(src), 4, "helper", str(src), 1, "target"],
    ]
    f = tmp_path / "trace.json"
    f.write_text(json.dumps({"test_src.py::test_x": events}))
    return f


# --- ingest + view --------------------------------------------------------- #

class TestIngest:
    def test_persists_ordered_calls(self, tmp_path):
        db, src, tst = _repo(tmp_path)
        tf = _trace_file(tmp_path, src, tst)
        res = tracing.ingest_trace(str(db), str(tf))
        assert res == {"tests": 1, "calls": 3}
        rows = sqlite3.connect(str(db)).execute(
            "SELECT COUNT(*) FROM call_trace").fetchone()[0]
        assert rows == 3

    def test_idempotent(self, tmp_path):
        db, src, tst = _repo(tmp_path)
        tf = _trace_file(tmp_path, src, tst)
        tracing.ingest_trace(str(db), str(tf))
        tracing.ingest_trace(str(db), str(tf))            # re-ingest same test
        rows = sqlite3.connect(str(db)).execute(
            "SELECT COUNT(*) FROM call_trace").fetchone()[0]
        assert rows == 3                                   # replaced, not doubled

    def test_missing_db_raises(self, tmp_path):
        tf = tmp_path / "t.json"
        tf.write_text("{}")
        with pytest.raises(FileNotFoundError):
            tracing.ingest_trace(str(tmp_path / "nope.db"), str(tf))

    def test_missing_trace_raises(self, tmp_path):
        db, _, _ = _repo(tmp_path)
        with pytest.raises(FileNotFoundError):
            tracing.ingest_trace(str(db), str(tmp_path / "nope.json"))


class TestView:
    def test_trace_view_succession(self, tmp_path):
        db, src, tst = _repo(tmp_path)
        tracing.ingest_trace(str(db), str(_trace_file(tmp_path, src, tst)))
        tl = tracing.list_traces(str(db))
        assert len(tl) == 1 and tl[0]["calls"] == 3
        view = tracing.trace_view(str(db), tl[0]["test"])
        names = [n["name"] for n in view["nodes"]]
        assert names == ["test_x", "helper", "target"]     # in real call order
        # edges follow the succession, with a root (test) and depth increasing.
        by = {n["name"]: n for n in view["nodes"]}
        assert by["target"]["depth"] > by["helper"]["depth"] > by["test_x"]["depth"]
        assert len(view["edges"]) == 2

    def test_unknown_test_empty(self, tmp_path):
        db, _, _ = _repo(tmp_path)
        assert tracing.trace_view(str(db), "nope")["nodes"] == []

    def test_no_table_empty(self, tmp_path):
        db, _, _ = _repo(tmp_path)              # never ingested a trace
        assert tracing.list_traces(str(db)) == []
        assert tracing.trace_view(str(db), "x")["nodes"] == []

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            tracing.list_traces(str(tmp_path / "nope.db"))


# --- server endpoints ------------------------------------------------------ #

class _Server:
    def __init__(self, db_path):
        from sylva.viz.server import make_server
        self.httpd = make_server(db_path, 0)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class TestEndpoints:
    def test_traces_and_trace(self, tmp_path):
        db, src, tst = _repo(tmp_path)
        tracing.ingest_trace(str(db), str(_trace_file(tmp_path, src, tst)))
        with _Server(str(db)) as s:
            st, ts = s.get("/traces")
            assert st == 200 and ts[0]["calls"] == 3
            st, tv = s.get("/trace?test=" + ts[0]["test"])
            assert st == 200
            assert [n["name"] for n in tv["nodes"]] == ["test_x", "helper", "target"]

    def test_trace_requires_test_param(self, tmp_path):
        db, _, _ = _repo(tmp_path)
        with _Server(str(db)) as s:
            st, body = s.get("/trace")
            assert st == 400 and "error" in body


# --- UI asset -------------------------------------------------------------- #

class TestAsset:
    def test_box_renderer_present(self):
        import sylva.viz.server as srv
        html = open(os.path.join(srv.ASSETS_DIR, "index.html")).read()
        assert "drawTrace" in html and "renderTrace" in html   # box renderer
        assert 'id="nav-traces"' in html                       # sidebar list
        assert "enterTrace" in html                            # click handler
        assert "roundRect" in html and "drawArrow" in html     # boxes + arrows
        # Layered views (flow / exec / system-flow / data-flow) also draw boxes.
        assert "function drawLayered" in html
        assert "if (flowMode) return drawLayered" in html
        # UX: land on the high-level diagram (not the force graph); task-oriented
        # groups; the test-path lists sit above the Explore filter.
        assert "load().then(() => enterSystemFlow())" in html
        for grp in ("See the whole system", "Follow one test", "Dig into the code"):
            assert grp in html
        assert html.index('id="nav-traces"') < html.index('id="filter"')
        assert html.index('id="nav-tests"') < html.index('id="filter"')
