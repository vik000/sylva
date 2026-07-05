"""Feature 10.1 — instruction-set skill: deterministic project brief (issue #61).

`generate_brief(db)` assembles an AGENTS.md-style markdown brief mechanically
from the code graph (archetype/layers, entrypoints, spine, modules, centrality,
foreign boundaries). Deterministic and reproducible; the `sylva brief` CLI writes
it to SYLVA.md.
"""

import os

import pytest

import sylva
from sylva.report import generate_brief
import sylva.__main__ as cli


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path, text):
    path.write_text(text)
    sylva.write_symbols(str(db), str(path), sylva.extract_symbols(str(path)))
    return db


APP = (
    "def helper():\n    return 1\n\n"
    "def main():\n    return helper()\n\n"
    "if __name__ == '__main__':\n    main()\n"
)


class TestGenerateBrief:
    def test_brief_references_key_facts(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "app.py", APP)
        sylva.build_edges(str(db))
        md = generate_brief(str(db))

        assert md.startswith("# ")  # a titled markdown doc
        assert "## Overview" in md and "## Entrypoints" in md
        assert "`main`" in md  # the inferred primary entrypoint
        assert "Archetype" in md
        assert "## How to run" in md

    def test_service_brief_has_layers(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db,
            tmp_path / "api.py",
            "from flask import Flask\napp = Flask(__name__)\n\n"
            "@app.route('/')\ndef home():\n    return 'hi'\n",
        )
        _index(db, tmp_path / "models.py", "import sqlalchemy\n\ndef load():\n    return 1\n")
        sylva.build_edges(str(db))
        md = generate_brief(str(db))
        assert "service" in md
        assert "Architectural layers" in md
        assert "interface" in md and "data" in md

    def test_foreign_boundary_reported(self, tmp_path):
        db = _init(tmp_path)
        rs = tmp_path / "native.rs"
        rs.write_text(
            "#[pyfunction]\nfn go() {}\n\n#[pymodule]\nfn native(m: &Bound<'_, PyModule>) "
            "-> PyResult<()> { Ok(()) }\n"
        )
        sylva.write_symbols(str(db), str(rs), sylva.extract_foreign_exports(str(rs)))
        _index(db, tmp_path / "app.py", "def run():\n    return go()\n")
        sylva.build_edges(str(db))
        md = generate_brief(str(db))
        assert "Foreign" in md and "native" in md

    def test_excludes_test_code(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "app.py", APP)
        # A test file: its function is a call-graph root but must NOT appear as a
        # system entrypoint, and its symbols must not inflate the brief.
        _index(
            db,
            tmp_path / "test_app.py",
            "import sqlite3\n\ndef test_helper_runs():\n    assert True\n",
        )
        sylva.build_edges(str(db))
        md = generate_brief(str(db))
        assert "test_helper_runs" not in md  # test code excluded from the brief
        assert "`main`" in md  # real entrypoint still present

    def test_idempotent(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "app.py", APP)
        sylva.build_edges(str(db))
        assert generate_brief(str(db)) == generate_brief(str(db))  # same graph -> same brief


class TestEdgeCases:
    def test_empty_graph_valid_brief(self, tmp_path):
        db = _init(tmp_path)
        md = generate_brief(str(db))  # no crash
        assert md.startswith("# ")
        assert "## Overview" in md
        assert "library" in md  # nothing runs it

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            generate_brief(str(tmp_path / "nope.db"))


class TestCli:
    def test_brief_command_writes_file(self, tmp_path, capsys):
        db = _init(tmp_path)
        _index(db, tmp_path / "app.py", APP)
        sylva.build_edges(str(db))
        out = tmp_path / "SYLVA.md"
        rc = cli.main(["brief", "--db", str(db), "--out", str(out)])
        assert rc == 0
        assert out.is_file()
        assert "## Entrypoints" in out.read_text()

    def test_brief_missing_db_errors(self, tmp_path, capsys):
        rc = cli.main(["brief", "--db", str(tmp_path / "nope.db"), "--out", str(tmp_path / "o.md")])
        assert rc == 1
        assert "not found" in capsys.readouterr().err


class TestSkillArtifact:
    def test_skill_file_present(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        skill = os.path.join(root, "skills", "sylva-brief.md")
        assert os.path.isfile(skill)
        text = open(skill).read()
        assert "sylva brief" in text  # documents the invocation
