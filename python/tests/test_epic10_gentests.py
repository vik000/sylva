"""Feature 10.2 — generate-e2e-tests skill: verified logic paths (issue #62).

Two deterministic, testable pieces support the skill: `suggest_test_targets`
ranks untested logic worth e2e-testing (importance-weighted, reusing 9.1/9.3),
and a viz "Logic paths" surface (`/tests` + sidebar) renders each test's real
execution path (4.11) as a block diagram. Sylva runs no tests.
"""

import json
import os
import threading
import urllib.error
import urllib.request

import pytest

import sylva
from sylva.viz import make_server
from sylva.viz import tests as list_tests  # aliased: pytest would collect `tests` as a test
import sylva.viz.server as srv


# main -> a -> b, guarded. `main` is the primary entrypoint.
CHAIN = (
    "def b():\n    return 1\n\n"           # line 1
    "def a():\n    return b()\n\n"         # line 4
    "def main():\n    return a()\n\n"      # line 7
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


class TestSuggestTargets:
    def test_untested_ranked_covered_excluded(self, tmp_path):
        db = _init(tmp_path)
        src = _index(db, tmp_path / "m.py", CHAIN)
        _index(db, tmp_path / "t.py", "def test_a():\n    return 1\n")
        sylva.build_edges(str(db))
        # A test covers `a` (line 4) -> `a` is now covered.
        sylva.map_tests_to_symbols(str(db), {"test_a": {str(src): [4]}})

        targets = sylva.suggest_test_targets(str(db))
        names = [t["symbol"] for t in targets]
        assert "main" in names and "b" in names  # untested logic
        assert "a" not in names                   # already covered -> excluded
        assert "test_a" not in names              # test code excluded

    def test_primary_entrypoint_ranks_first(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        sylva.build_edges(str(db))
        targets = sylva.suggest_test_targets(str(db))
        assert targets[0]["symbol"] == "main"
        assert "entrypoint" in targets[0]["reason"]
        assert targets[0]["score"] > targets[-1]["score"]

    def test_empty_graph(self, tmp_path):
        db = _init(tmp_path)
        assert sylva.suggest_test_targets(str(db)) == []

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sylva.suggest_test_targets(str(tmp_path / "nope.db"))

    def test_mcp_tool(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        sylva.build_edges(str(db))
        resp = json.loads(
            sylva.handle_request(
                str(db),
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "suggest_test_targets", "params": {}}),
            )
        )
        assert any(t["symbol"] == "main" for t in resp["result"])


class TestLogicPathsViz:
    def test_tests_lists_coverage_mapped_tests(self, tmp_path):
        db = _init(tmp_path)
        src = _index(db, tmp_path / "m.py", CHAIN)
        _index(db, tmp_path / "t.py", "def test_x():\n    return 1\n")
        sylva.build_edges(str(db))
        sylva.map_tests_to_symbols(str(db), {"test_x": {str(src): [7, 4, 1]}})  # main, a, b
        result = list_tests(str(db))
        assert len(result) == 1
        assert result[0]["test"] == "test_x" and result[0]["covers"] == 3

    def test_tests_empty_without_coverage(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "m.py", CHAIN)
        sylva.build_edges(str(db))
        assert list_tests(str(db)) == []  # no test_covers edges yet

    def test_tests_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            list_tests(str(tmp_path / "nope.db"))

    def test_endpoint(self, tmp_path):
        db = _init(tmp_path)
        src = _index(db, tmp_path / "m.py", CHAIN)
        _index(db, tmp_path / "t.py", "def test_x():\n    return 1\n")
        sylva.build_edges(str(db))
        sylva.map_tests_to_symbols(str(db), {"test_x": {str(src): [7, 4]}})
        httpd = make_server(str(db), 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/tests", timeout=5) as r:
                body = json.loads(r.read())
            assert body[0]["test"] == "test_x"
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_ui_asset(self):
        with open(os.path.join(srv.ASSETS_DIR, "index.html")) as f:
            html = f.read()
        assert 'id="nav-tests"' in html
        assert "loadTests" in html and "enterExec(t.test)" in html  # click -> exec-path diagram


class TestSkillArtifact:
    def test_skill_present(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        skill = os.path.join(root, "skills", "sylva-generate-tests.md")
        assert os.path.isfile(skill)
        text = open(skill).read()
        assert "suggest_test_targets" in text and "map_tests_to_symbols" in text
