"""Feature 8.1 — Detailed analysis report (issue #40).

`generate_report(db)` assembles a deterministic health/risk report — the metrics
counterpart to 10.1's narrative brief: Architecture (4.3), Coverage rollup + gaps
(3.4), change Risk via blast radius (4.2), and structural Gaps (orphan symbols).
Pure aggregation over existing query functions (no Rust change); same graph →
identical bytes.
"""

import json
import sqlite3

import pytest

import sylva
from sylva.report import generate_report, report_data


# A small app: main -> a -> helper (a chain), plus an unconnected `orphan`.
APP = (
    "def helper():\n    return 1\n\n"
    "def a():\n    return helper()\n\n"
    "def main():\n    return a()\n\n"
    "def orphan():\n    return 0\n\n"
    "if __name__ == '__main__':\n    main()\n"
)


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, tmp_path, text=APP, name="app.py"):
    p = tmp_path / name
    p.write_text(text)
    sylva.write_symbols(str(db), str(p), sylva.extract_symbols(str(p)))
    sylva.build_edges(str(db))
    return p


def _set_coverage(db, mapping):
    """Set coverage_pct for named symbols; unnamed stay NULL (never measured)."""
    conn = sqlite3.connect(str(db))
    try:
        for name, pct in mapping.items():
            conn.execute("UPDATE symbols SET coverage_pct = ? WHERE name = ?", (pct, name))
        conn.commit()
    finally:
        conn.close()


# --- structure / general --------------------------------------------------- #

class TestSections:
    def test_has_expected_sections(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path)
        md = generate_report(str(db))
        for heading in ("# ", "## Architecture", "## Coverage", "## Risk", "## Gaps"):
            assert heading in md

    def test_report_data_shape(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path)
        d = report_data(str(db))
        assert set(d) == {
            "project", "symbols", "source_files", "architecture",
            "coverage", "risk", "gaps", "errors",
        }
        assert d["symbols"] == 4  # helper, a, main, orphan
        assert set(d["architecture"]) == {"modules", "hubs", "entry_points"}

    def test_orphan_detected(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path)
        d = report_data(str(db))
        orphan_names = {o["name"] for o in d["gaps"]["orphans"]}
        assert "orphan" in orphan_names          # no edges at all
        assert "helper" not in orphan_names      # dst of a call edge

    def test_risk_blast_radius(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path)
        d = report_data(str(db))
        by = {r["symbol"]: r["affected"] for r in d["risk"]}
        # `helper` is reached transitively by a and main -> at least 2 affected.
        assert by.get("helper", 0) >= 2
        md = generate_report(str(db))
        assert "affects" in md


# --- coverage -------------------------------------------------------------- #

class TestCoverage:
    def test_uncovered_and_never_measured(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path)
        # helper fully covered, a uncovered (0%); main/orphan stay NULL.
        _set_coverage(db, {"helper": 100.0, "a": 0.0})
        d = report_data(str(db))
        cov = d["coverage"]
        assert cov["measured"] is True
        assert {i["name"] for i in cov["uncovered"]} == {"a"}
        assert {"main", "orphan"} <= {i["name"] for i in cov["never_measured"]}

    def test_no_coverage_says_so(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path)  # no coverage applied
        d = report_data(str(db))
        assert d["coverage"]["measured"] is False
        md = generate_report(str(db))
        assert "No coverage data" in md
        # Never emits fabricated zeros when nothing was measured.
        assert "0%" not in md.split("## Risk")[0].split("## Coverage")[1]


# --- edge / negative / determinism ---------------------------------------- #

class TestEdges:
    def test_empty_db_minimal_report(self, tmp_path):
        db = _init(tmp_path)
        md = generate_report(str(db))
        assert md.startswith("# ")
        assert "## Architecture" in md and "## Gaps" in md  # valid, not an error

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            generate_report(str(tmp_path / "nope.db"))
        with pytest.raises(FileNotFoundError):
            report_data(str(tmp_path / "nope.db"))

    def test_deterministic(self, tmp_path):
        db = _init(tmp_path)
        _index(db, tmp_path)
        _set_coverage(db, {"helper": 100.0, "a": 0.0})
        assert generate_report(str(db)) == generate_report(str(db))
        assert json.dumps(report_data(str(db)), sort_keys=True) == json.dumps(
            report_data(str(db)), sort_keys=True
        )

    def test_section_failure_degrades(self, tmp_path, monkeypatch):
        db = _init(tmp_path)
        _index(db, tmp_path)

        def _boom(*a, **k):
            raise RuntimeError("blast failed")

        monkeypatch.setattr(sylva, "blast_radius", _boom)
        d = report_data(str(db))          # does not raise
        assert d["errors"]                # the failure is recorded
        assert all(r["affected"] == 0 for r in d["risk"])  # degraded, not crashed
        md = generate_report(str(db))
        assert "Report warnings" in md


# --- CLI ------------------------------------------------------------------- #

class TestCli:
    def test_report_writes_files(self, tmp_path):
        import sylva.__main__ as cli

        db = _init(tmp_path)
        _index(db, tmp_path)
        out = tmp_path / "report"
        rc = cli.main(["report", "--db", str(db), "--out", str(out)])
        assert rc == 0
        assert (out / "REPORT.md").exists()
        data = json.loads((out / "report.json").read_text())
        assert "architecture" in data and "risk" in data

    def test_report_missing_db(self, tmp_path):
        import sylva.__main__ as cli

        rc = cli.main(["report", "--db", str(tmp_path / "nope.db"), "--out", str(tmp_path / "r")])
        assert rc == 1
