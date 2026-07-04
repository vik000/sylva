"""Feature 7.9 — Quiet build_edges logging (issue #39).

`build_edges` used to emit one stderr line per skipped reference (81KB from one
real repo). It now emits a single bounded summary; per-reference detail is
opt-in via the SYLVA_LOG env var. Verified with `capfd` (captures fd-level
stderr from the Rust extension, which `capsys` would miss).
"""

import os

import sylva


def _many_ambiguous(tmp_path, calls=40):
    """A graph where a call target name is ambiguous and called many times."""
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    # Two definitions of `get` -> every call to get() is ambiguous.
    for name in ("a.py", "b.py"):
        p = tmp_path / name
        p.write_text("def get():\n    return 1\n")
        sylva.write_symbols(str(db), str(p), sylva.extract_symbols(str(p)))
    body = "def use():\n" + "".join(f"    get()\n" for _ in range(calls))
    caller = tmp_path / "caller.py"
    caller.write_text(body)
    sylva.write_symbols(str(db), str(caller), sylva.extract_symbols(str(caller)))
    return db


class TestQuietByDefault:
    def test_summary_not_per_skip(self, tmp_path, capfd):
        db = _many_ambiguous(tmp_path, calls=40)
        sylva.build_edges(str(db))
        err = capfd.readouterr().err
        lines = [l for l in err.splitlines() if l.strip()]
        # 40 ambiguous skips must NOT be 40 lines — a single bounded summary.
        assert len(lines) <= 2
        assert any("skipped" in l and "ambiguous" in l for l in lines)

    def test_clean_graph_is_silent(self, tmp_path, capfd):
        # No ambiguity / unresolved -> no summary line at all.
        db = tmp_path / "sylva.db"
        sylva.init_db(str(db))
        p = tmp_path / "m.py"
        p.write_text("def b():\n    return 1\n\ndef a():\n    return b()\n")
        sylva.write_symbols(str(db), str(p), sylva.extract_symbols(str(p)))
        sylva.build_edges(str(db))
        err = capfd.readouterr().err
        assert err.strip() == ""


class TestVerboseOptIn:
    def test_sylva_log_enables_detail(self, tmp_path, capfd, monkeypatch):
        monkeypatch.setenv("SYLVA_LOG", "1")
        db = _many_ambiguous(tmp_path, calls=40)
        sylva.build_edges(str(db))
        err = capfd.readouterr().err
        lines = [l for l in err.splitlines() if l.strip()]
        # With SYLVA_LOG set, per-reference detail lines appear (plus summary).
        assert len(lines) > 5
        assert any("ambiguous call reference 'get'" in l for l in lines)


class TestEdgesStillCorrect:
    def test_quieting_did_not_change_edges(self, tmp_path):
        # The summary refactor must not alter which edges are produced.
        db = tmp_path / "sylva.db"
        sylva.init_db(str(db))
        p = tmp_path / "m.py"
        p.write_text("def b():\n    return 1\n\ndef a():\n    return b()\n")
        sylva.write_symbols(str(db), str(p), sylva.extract_symbols(str(p)))
        n = sylva.build_edges(str(db))
        assert n == 1  # a -> b still resolved
