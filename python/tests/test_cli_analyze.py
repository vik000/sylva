"""CLI: `sylva analyze` — the one-shot analysis command used in the README.

Walks a codebase, extracts + writes symbols, and builds relationships into the
graph database in a single command.
"""

import sqlite3

import sylva.__main__ as cli


def _counts(db):
    conn = sqlite3.connect(str(db))
    try:
        s = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        e = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        return s, e
    finally:
        conn.close()


def test_analyze_builds_graph(tmp_path, capsys):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "pkg" / "b.py").write_text(
        "from pkg.a import helper\n\ndef main():\n    return helper()\n"
    )
    db = tmp_path / "out" / "graph.db"

    rc = cli.main(["analyze", "--root", str(tmp_path), "--db", str(db)])
    assert rc == 0
    assert db.exists()

    symbols, edges = _counts(db)
    assert symbols >= 3          # helper, main, the import binding
    assert edges >= 1            # main -> helper (call), and the import edge
    out = capsys.readouterr().out
    assert "analyzed" in out and "serve-ui" in out


def test_analyze_bad_root(tmp_path):
    rc = cli.main(["analyze", "--root", str(tmp_path / "nope"), "--db", str(tmp_path / "g.db")])
    assert rc == 1


def test_analyze_skips_unreadable_file(tmp_path):
    # A non-UTF-8 ".py" file must be skipped, not abort the whole analysis.
    (tmp_path / "good.py").write_text("def ok():\n    return 1\n")
    (tmp_path / "bad.py").write_bytes(b"\xff\xfe\x00 not utf8")
    db = tmp_path / "g.db"
    rc = cli.main(["analyze", "--root", str(tmp_path), "--db", str(db)])
    assert rc == 0
    symbols, _ = _counts(db)
    assert symbols >= 1  # good.py still indexed
