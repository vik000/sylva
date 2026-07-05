"""Feature 10.4 — understand-project orchestrator (issue #64).

`onboard(root, db)` runs the deterministic pipeline (index → brief → diagrams →
MCP scaffold) that takes an unknown repo to an agent-ready state in one call.
Graceful degradation: per-step failures are recorded, not fatal; the target is
never run. `index_codebase` is the shared indexing core.
"""

import json
import os
import sqlite3

import pytest

import sylva
from sylva.onboard import index_codebase, onboard
import sylva.__main__ as cli


APP = (
    "def helper():\n    return 1\n\n"
    "def main():\n    return helper()\n\n"
    "if __name__ == '__main__':\n    main()\n"
)


def _repo(tmp_path):
    (tmp_path / "app.py").write_text(APP)
    (tmp_path / "native.rs").write_text(
        "#[pyfunction]\nfn go() {}\n\n#[pymodule]\n"
        "fn native(m: &Bound<'_, PyModule>) -> PyResult<()> { Ok(()) }\n"
    )
    return tmp_path


class TestIndexCodebase:
    def test_builds_graph(self, tmp_path):
        _repo(tmp_path)
        db = tmp_path / ".codemcp" / "sylva.db"
        idx = index_codebase(str(tmp_path), str(db))
        assert idx["symbols"] >= 2 and idx["edges"] >= 1
        assert idx["foreign"] >= 1  # the Rust export surface
        conn = sqlite3.connect(str(db))
        try:
            assert conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] > 0
        finally:
            conn.close()

    def test_non_directory_raises(self, tmp_path):
        with pytest.raises(NotADirectoryError):
            index_codebase(str(tmp_path / "nope"), str(tmp_path / "db"))


class TestOnboard:
    def test_produces_all_artifacts(self, tmp_path):
        _repo(tmp_path)
        db = tmp_path / ".codemcp" / "sylva.db"
        brief = tmp_path / "SYLVA.md"
        diags = tmp_path / "DIAGRAMS.md"
        mcp = tmp_path / ".codemcp"
        result = onboard(str(tmp_path), str(db), str(brief), str(diags), str(mcp))

        art = result["artifacts"]
        assert art["brief"] == str(brief) and brief.is_file()
        assert art["diagrams"] == str(diags) and diags.is_file()
        assert "mcp" in art and os.path.isfile(art["mcp"])
        # The artifacts have real content.
        assert "## Entrypoints" in brief.read_text()
        assert "```mermaid" in diags.read_text()
        with open(art["mcp"]) as f:
            assert "mcpServers" in json.load(f)

    def test_graceful_degradation(self, tmp_path, monkeypatch):
        # If one step fails, the others still run and it's recorded, not raised.
        _repo(tmp_path)
        db = tmp_path / ".codemcp" / "sylva.db"
        import sylva.diagrams as dm

        monkeypatch.setattr(dm, "generate_diagrams", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        result = onboard(str(tmp_path), str(db), str(tmp_path / "SYLVA.md"),
                         str(tmp_path / "DIAGRAMS.md"), str(tmp_path / ".codemcp"))
        art = result["artifacts"]
        assert "diagrams_error" in art and "boom" in art["diagrams_error"]
        assert "brief" in art  # brief still produced despite the diagram failure
        assert "mcp" in art


class TestCli:
    def test_onboard_command(self, tmp_path, capsys):
        _repo(tmp_path)
        db = tmp_path / ".codemcp" / "sylva.db"
        rc = cli.main(["onboard", "--root", str(tmp_path), "--db", str(db)])
        assert rc == 0
        assert "onboarded" in capsys.readouterr().out
        # Artifacts are written INTO the onboarded project (not the cwd).
        assert db.is_file()
        assert (tmp_path / "SYLVA.md").is_file()
        assert (tmp_path / "DIAGRAMS.md").is_file()
        assert (tmp_path / ".codemcp" / "mcp.json").is_file()

    def test_onboard_bad_root(self, tmp_path, capsys):
        rc = cli.main(["onboard", "--root", str(tmp_path / "nope"), "--db", str(tmp_path / "db")])
        assert rc == 1
        assert "not a directory" in capsys.readouterr().err


class TestAnalyzeUnchanged:
    def test_analyze_still_works_after_refactor(self, tmp_path, capsys):
        _repo(tmp_path)
        db = tmp_path / "g.db"
        rc = cli.main(["analyze", "--root", str(tmp_path), "--db", str(db)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "analyzed" in out and "foreign export" in out  # 5.0 note preserved


class TestSkillArtifact:
    def test_skill_present(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        skill = os.path.join(root, "skills", "sylva-onboard.md")
        assert os.path.isfile(skill)
        assert "sylva onboard" in open(skill).read()
