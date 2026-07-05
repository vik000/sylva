"""Feature 5.4 — Coverage support for additional languages (issue #23).

LCOV is language-neutral: cargo-tarpaulin (Rust) and Istanbul/nyc (JS/TS) both
emit standard LCOV, so the existing parser (Feature 3.1) handles them — it reads
`SF:`/`DA:` and ignores the extra record types (`FN`, `FNDA`, `BRDA`, `LF`,
`LH`, `TN`, …). These tests lock that cross-language guarantee in.
"""

import pytest

import sylva


# cargo-tarpaulin --out Lcov (Rust): SF/DA + LF/LH summary lines.
TARPAULIN = (
    "TN:\n"
    "SF:src/lib.rs\n"
    "DA:3,1\n"
    "DA:4,1\n"
    "DA:7,0\n"
    "LF:3\n"
    "LH:2\n"
    "end_of_record\n"
)

# Istanbul / nyc (TypeScript): SF/DA plus function + branch records.
ISTANBUL = (
    "TN:\n"
    "SF:src/math.ts\n"
    "FN:1,add\n"
    "FNF:1\n"
    "FNH:1\n"
    "FNDA:5,add\n"
    "DA:1,5\n"
    "DA:2,5\n"
    "DA:3,0\n"
    "LF:3\n"
    "LH:2\n"
    "BRDA:2,0,0,5\n"
    "BRDA:2,0,1,0\n"
    "BRF:2\n"
    "BRH:1\n"
    "end_of_record\n"
)


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


class TestTarpaulinRust:
    def test_parses_rust_lcov(self, tmp_path):
        cov = sylva.parse_coverage(_write(tmp_path, "tarpaulin.lcov", TARPAULIN), "lcov")
        assert "src/lib.rs" in cov
        # Line 3 & 4 covered (hits > 0), line 7 not; summary/TN lines ignored.
        assert cov["src/lib.rs"] == {3: True, 4: True, 7: False}


class TestIstanbulTypeScript:
    def test_parses_ts_lcov(self, tmp_path):
        cov = sylva.parse_coverage(_write(tmp_path, "lcov.info", ISTANBUL), "lcov")
        assert "src/math.ts" in cov
        # DA lines parsed; FN/FNDA/BRDA/BRF/BRH lines ignored, not a crash.
        assert cov["src/math.ts"] == {1: True, 2: True, 3: False}


class TestMixedLanguage:
    def test_one_report_two_languages(self, tmp_path):
        cov = sylva.parse_coverage(
            _write(tmp_path, "mixed.lcov", TARPAULIN + ISTANBUL), "lcov"
        )
        assert "src/lib.rs" in cov and "src/math.ts" in cov
        assert cov["src/lib.rs"][7] is False
        assert cov["src/math.ts"][1] is True


class TestNegative:
    def test_unknown_format_raises(self, tmp_path):
        with pytest.raises(ValueError):
            sylva.parse_coverage(_write(tmp_path, "c.lcov", TARPAULIN), "bogus")

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sylva.parse_coverage(str(tmp_path / "nope.lcov"), "lcov")
