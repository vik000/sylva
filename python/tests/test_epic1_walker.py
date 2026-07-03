"""Feature 1.3 — Python file walker.

Verifies `sylva.walk_python_files(root, extra_ignores=None)` finds `.py` files
recursively, handles empty/single/nested trees, rejects a missing root with
`FileNotFoundError`, and survives unreadable subdirectories without aborting.
"""

import os
import sys

import pytest

import sylva


def _mkpy(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n")
    return path


def _realset(paths):
    return {os.path.realpath(p) for p in paths}


class TestGeneral:
    def test_finds_all_py_files_nested(self, tmp_path):
        a = _mkpy(tmp_path / "a.py")
        b = _mkpy(tmp_path / "pkg" / "b.py")
        c = _mkpy(tmp_path / "pkg" / "sub" / "c.py")
        # Non-.py files must be ignored.
        (tmp_path / "readme.md").write_text("hi")
        (tmp_path / "pkg" / "data.txt").write_text("hi")

        result = sylva.walk_python_files(str(tmp_path))
        assert _realset(result) == _realset([a, b, c])

    def test_returns_sorted(self, tmp_path):
        _mkpy(tmp_path / "z.py")
        _mkpy(tmp_path / "a.py")
        _mkpy(tmp_path / "m.py")
        result = sylva.walk_python_files(str(tmp_path))
        assert result == sorted(result)

    def test_only_py_extension(self, tmp_path):
        _mkpy(tmp_path / "keep.py")
        (tmp_path / "skip.pyc").write_text("x")
        (tmp_path / "skip.pyi").write_text("x")
        (tmp_path / "notpy").write_text("x")
        result = sylva.walk_python_files(str(tmp_path))
        assert [os.path.basename(p) for p in result] == ["keep.py"]


class TestEdge:
    def test_empty_directory(self, tmp_path):
        assert sylva.walk_python_files(str(tmp_path)) == []

    def test_directory_with_no_py_files(self, tmp_path):
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / "sub").mkdir()
        assert sylva.walk_python_files(str(tmp_path)) == []

    def test_single_file_root(self, tmp_path):
        f = _mkpy(tmp_path / "solo.py")
        result = sylva.walk_python_files(str(f))
        assert _realset(result) == _realset([f])

    def test_deeply_nested(self, tmp_path):
        deep = tmp_path
        for i in range(6):
            deep = deep / f"lvl{i}"
        f = _mkpy(deep / "deep.py")
        result = sylva.walk_python_files(str(tmp_path))
        assert _realset(result) == _realset([f])


class TestExtraIgnores:
    def test_extra_ignore_pattern_excludes(self, tmp_path):
        keep = _mkpy(tmp_path / "keep.py")
        _mkpy(tmp_path / "build" / "gen.py")
        result = sylva.walk_python_files(str(tmp_path), ["build/"])
        assert _realset(result) == _realset([keep])

    def test_extra_ignore_glob(self, tmp_path):
        keep = _mkpy(tmp_path / "keep.py")
        _mkpy(tmp_path / "conftest.py")
        result = sylva.walk_python_files(str(tmp_path), ["conftest.py"])
        assert _realset(result) == _realset([keep])

    def test_none_and_empty_are_noop(self, tmp_path):
        f = _mkpy(tmp_path / "a.py")
        assert _realset(sylva.walk_python_files(str(tmp_path), None)) == _realset([f])
        assert _realset(sylva.walk_python_files(str(tmp_path), [])) == _realset([f])


class TestNegative:
    def test_missing_root_raises_filenotfound(self, tmp_path):
        missing = tmp_path / "does_not_exist"
        with pytest.raises(FileNotFoundError):
            sylva.walk_python_files(str(missing))


class TestErrorControl:
    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX permission model"
    )
    def test_unreadable_subdir_is_skipped_not_fatal(self, tmp_path):
        # An accessible file plus a locked-down subdirectory.
        visible = _mkpy(tmp_path / "visible.py")
        locked = tmp_path / "locked"
        _mkpy(locked / "hidden.py")
        os.chmod(locked, 0o000)
        try:
            # Must not raise, and the accessible file must still be found.
            result = sylva.walk_python_files(str(tmp_path))
            assert os.path.realpath(str(visible)) in _realset(result)
        finally:
            os.chmod(locked, 0o755)  # restore so tmp cleanup succeeds
