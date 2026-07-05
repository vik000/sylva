"""Feature 10.3 — diagram skill: deterministic Mermaid diagrams (issue #63).

`generate_diagrams(db)` emits a DIAGRAMS.md of fenced ```mermaid blocks from the
graph — system flow (9.4, spine styled), package module map (4.7), and layer
tiers (9.8). Deterministic; the `sylva diagram` CLI writes it.
"""

import os

import pytest

import sylva
from sylva.diagrams import generate_diagrams
import sylva.__main__ as cli


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    sylva.write_symbols(str(db), str(path), sylva.extract_symbols(str(path)))
    return db


APP = (
    "def helper():\n    return 1\n\n"
    "def main():\n    return helper()\n\n"
    "if __name__ == '__main__':\n    main()\n"
)


def _mermaid_blocks(md):
    return md.count("```mermaid")


class TestGenerateDiagrams:
    def test_valid_mermaid_with_spine(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "app.py", APP)
        sylva.build_edges(str(db))
        md = generate_diagrams(str(db))

        assert md.startswith("# ")
        assert _mermaid_blocks(md) >= 2  # system flow + module map at least
        assert "flowchart TD" in md and "flowchart LR" in md
        # System flow reflects the inferred primary + a highlighted spine.
        assert '"main"' in md
        assert ":::spine" in md and "classDef spine" in md
        # Every mermaid fence is closed.
        assert md.count("```") % 2 == 0

    def test_edges_rendered(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "app.py", APP)
        sylva.build_edges(str(db))
        md = generate_diagrams(str(db))
        assert "-->" in md  # main --> helper appears as an edge

    def test_service_has_layer_diagram(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db,
            tmp_path / "api.py",
            "from flask import Flask\napp = Flask(__name__)\n\n"
            "@app.route('/')\ndef home():\n    return 'hi'\n",
        )
        _index(db, tmp_path / "models.py", "import sqlalchemy\n\ndef load():\n    return 1\n")
        sylva.build_edges(str(db))
        md = generate_diagrams(str(db))
        assert "Architectural layers" in md
        assert "interface" in md and "data" in md

    def test_idempotent(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "app.py", APP)
        sylva.build_edges(str(db))
        assert generate_diagrams(str(db)) == generate_diagrams(str(db))

    def test_test_packages_excluded_from_module_map(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "src" / "app.py", APP)
        _index(db, tmp_path / "tests" / "test_app.py", "def test_x():\n    assert True\n")
        sylva.build_edges(str(db))
        md = generate_diagrams(str(db))
        # The tests/ package is not drawn in the module map.
        assert "tests (" not in md


class TestEdgeCases:
    def test_empty_graph_valid_doc(self, tmp_path):
        db = _init(tmp_path)
        md = generate_diagrams(str(db))  # no crash
        assert md.startswith("# ")
        assert "```mermaid" in md
        assert md.count("```") % 2 == 0

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            generate_diagrams(str(tmp_path / "nope.db"))


class TestCli:
    def test_diagram_command_writes_file(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path / "app.py", APP)
        sylva.build_edges(str(db))
        out = tmp_path / "DIAGRAMS.md"
        rc = cli.main(["diagram", "--db", str(db), "--out", str(out)])
        assert rc == 0
        assert out.is_file()
        assert "```mermaid" in out.read_text()

    def test_diagram_missing_db_errors(self, tmp_path, capsys):
        rc = cli.main(["diagram", "--db", str(tmp_path / "nope.db"), "--out", str(tmp_path / "o.md")])
        assert rc == 1
        assert "not found" in capsys.readouterr().err


class TestSkillArtifact:
    def test_skill_file_present(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        skill = os.path.join(root, "skills", "sylva-diagram.md")
        assert os.path.isfile(skill)
        assert "sylva diagram" in open(skill).read()
