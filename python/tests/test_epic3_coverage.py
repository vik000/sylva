"""Feature 3.1 — Parse coverage.py output (LCOV / Cobertura XML).

`sylva.parse_coverage(path, format) -> {file: {line: covered}}`. Pure parsing,
verified with hand-written fixtures for both formats.
"""

import pytest

import sylva


# --- fixtures -------------------------------------------------------------- #

LCOV = """\
TN:
SF:src/foo.py
DA:1,1
DA:2,3
DA:3,0
LF:3
LH:2
end_of_record
SF:src/bar.py
DA:1,0
DA:2,0
end_of_record
"""

COBERTURA = """\
<?xml version="1.0" ?>
<coverage line-rate="0.5" version="1.0">
  <packages>
    <package name="src">
      <classes>
        <class filename="src/foo.py" name="foo">
          <lines>
            <line number="1" hits="1"/>
            <line number="2" hits="0"/>
            <line number="3" hits="4"/>
          </lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
"""


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


# --- general --------------------------------------------------------------- #

class TestLcov:
    def test_parses_lcov(self, tmp_path):
        cov = sylva.parse_coverage(_write(tmp_path, "cov.info", LCOV), "lcov")
        assert cov == {
            "src/foo.py": {1: True, 2: True, 3: False},
            "src/bar.py": {1: False, 2: False},
        }

    def test_line_types_are_int_and_bool(self, tmp_path):
        cov = sylva.parse_coverage(_write(tmp_path, "cov.info", LCOV), "lcov")
        line, covered = next(iter(cov["src/foo.py"].items()))
        assert isinstance(line, int) and isinstance(covered, bool)

    def test_format_is_case_insensitive(self, tmp_path):
        cov = sylva.parse_coverage(_write(tmp_path, "cov.info", LCOV), "LCOV")
        assert "src/foo.py" in cov


class TestCobertura:
    def test_parses_cobertura(self, tmp_path):
        cov = sylva.parse_coverage(_write(tmp_path, "cov.xml", COBERTURA), "cobertura")
        assert cov == {"src/foo.py": {1: True, 2: False, 3: True}}


# --- edge ------------------------------------------------------------------ #

class TestEdge:
    def test_file_zero_percent(self, tmp_path):
        cov = sylva.parse_coverage(_write(tmp_path, "c.info", LCOV), "lcov")
        assert cov["src/bar.py"] == {1: False, 2: False}  # nothing covered

    def test_file_hundred_percent(self, tmp_path):
        text = "SF:a.py\nDA:1,1\nDA:2,5\nend_of_record\n"
        cov = sylva.parse_coverage(_write(tmp_path, "c.info", text), "lcov")
        assert cov["a.py"] == {1: True, 2: True}

    def test_empty_report_is_empty_dict(self, tmp_path):
        assert sylva.parse_coverage(_write(tmp_path, "empty.info", ""), "lcov") == {}

    def test_file_with_no_line_entries(self, tmp_path):
        # An SF section with no DA records -> present with an empty line map.
        text = "SF:blank.py\nend_of_record\n"
        cov = sylva.parse_coverage(_write(tmp_path, "c.info", text), "lcov")
        assert cov == {"blank.py": {}}

    def test_repeated_line_ors_to_covered(self, tmp_path):
        # Same line reported uncovered then covered -> covered wins.
        text = "SF:a.py\nDA:1,0\nDA:1,2\nend_of_record\n"
        cov = sylva.parse_coverage(_write(tmp_path, "c.info", text), "lcov")
        assert cov["a.py"][1] is True


# --- negative -------------------------------------------------------------- #

class TestNegative:
    def test_invalid_format_raises_valueerror(self, tmp_path):
        with pytest.raises(ValueError):
            sylva.parse_coverage(_write(tmp_path, "c.info", LCOV), "json")

    def test_invalid_format_raises_before_reading_file(self, tmp_path):
        # Bad format wins even if the path is missing.
        with pytest.raises(ValueError):
            sylva.parse_coverage(str(tmp_path / "missing.info"), "json")

    def test_missing_file_raises_filenotfound(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sylva.parse_coverage(str(tmp_path / "missing.info"), "lcov")

    def test_corrupt_cobertura_raises(self, tmp_path):
        bad = "<coverage><classes><class filename='x.py'><lines><line number=1"
        with pytest.raises(ValueError):
            sylva.parse_coverage(_write(tmp_path, "bad.xml", bad), "cobertura")


# --- error control --------------------------------------------------------- #

class TestErrorControl:
    def test_malformed_da_line_skipped_valid_kept(self, tmp_path):
        # A garbage DA line is skipped; valid lines in the same section survive.
        text = "SF:a.py\nDA:1,1\nDA:garbage\nDA:oops,nope\nDA:3,0\nend_of_record\n"
        cov = sylva.parse_coverage(_write(tmp_path, "c.info", text), "lcov")
        assert cov["a.py"] == {1: True, 3: False}

    def test_da_outside_section_skipped(self, tmp_path):
        # A DA with no preceding SF is skipped, not fatal.
        text = "DA:1,1\nSF:a.py\nDA:2,1\nend_of_record\n"
        cov = sylva.parse_coverage(_write(tmp_path, "c.info", text), "lcov")
        assert cov == {"a.py": {2: True}}

    def test_cobertura_class_without_filename_skipped(self, tmp_path):
        text = (
            "<coverage><packages><package><classes>"
            "<class name='noname'><lines><line number='1' hits='1'/></lines></class>"
            "<class filename='ok.py'><lines><line number='2' hits='1'/></lines></class>"
            "</classes></package></packages></coverage>"
        )
        cov = sylva.parse_coverage(_write(tmp_path, "c.xml", text), "cobertura")
        assert cov == {"ok.py": {2: True}}  # the filename-less class was skipped
