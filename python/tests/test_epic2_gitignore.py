"""Feature 2.3 — gitignore-aware file walking.

`walk_python_files` (which replaces the basic Epic 1 walker) now honours the
`.gitignore` hierarchy — even outside a git repo — plus `.codemcpignore`.

Verified precedence (empirically, and asserted below): `.codemcpignore` takes
precedence over `.gitignore`, so a negation in `.codemcpignore` can re-include a
file that `.gitignore` excluded.
"""

import os

import sylva


def _names(root):
    return sorted(os.path.basename(p) for p in sylva.walk_python_files(str(root)))


def _write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class TestGeneral:
    def test_gitignored_file_excluded_without_git_repo(self, tmp_path):
        # No .git dir here — the whole point of Feature 2.3 is that .gitignore
        # is still honoured.
        _write(tmp_path / "keep.py")
        _write(tmp_path / "ignored.py")
        (tmp_path / ".gitignore").write_text("ignored.py\n")
        assert _names(tmp_path) == ["keep.py"]

    def test_gitignore_directory_pattern(self, tmp_path):
        _write(tmp_path / "keep.py")
        _write(tmp_path / "build" / "gen.py")
        (tmp_path / ".gitignore").write_text("build/\n")
        assert _names(tmp_path) == ["keep.py"]

    def test_codemcpignore_adds_ignore(self, tmp_path):
        _write(tmp_path / "keep.py")
        _write(tmp_path / "extra.py")
        (tmp_path / ".codemcpignore").write_text("extra.py\n")
        assert _names(tmp_path) == ["keep.py"]

    def test_extra_ignores_still_apply(self, tmp_path):
        # The caller-supplied patterns from the Epic 1 signature keep working
        # alongside the ignore files.
        _write(tmp_path / "keep.py")
        _write(tmp_path / "skip.py")
        assert sorted(
            os.path.basename(p)
            for p in sylva.walk_python_files(str(tmp_path), ["skip.py"])
        ) == ["keep.py"]


class TestEdge:
    def test_nested_gitignore_respected(self, tmp_path):
        _write(tmp_path / "a.py")
        _write(tmp_path / "sub" / "b.py")
        _write(tmp_path / "sub" / "c.py")
        (tmp_path / "sub" / ".gitignore").write_text("b.py\n")
        # Only sub/b.py is excluded, by the subdir's own .gitignore.
        assert _names(tmp_path) == ["a.py", "c.py"]

    def test_codemcpignore_overrides_gitignore(self, tmp_path):
        # .gitignore excludes secret.py; .codemcpignore re-includes it via
        # negation. .codemcpignore wins.
        _write(tmp_path / "keep.py")
        _write(tmp_path / "secret.py")
        (tmp_path / ".gitignore").write_text("secret.py\n")
        (tmp_path / ".codemcpignore").write_text("!secret.py\n")
        assert _names(tmp_path) == ["keep.py", "secret.py"]


class TestNegative:
    def test_malformed_gitignore_line_not_fatal(self, tmp_path):
        # A malformed pattern must be skipped without aborting the walk, and the
        # valid sibling pattern must still take effect.
        _write(tmp_path / "keep.py")
        _write(tmp_path / "ignored.py")
        (tmp_path / ".gitignore").write_text("[unclosed\nignored.py\n")
        result = _names(tmp_path)
        assert "keep.py" in result          # walk completed
        assert "ignored.py" not in result   # valid pattern still applied


class TestErrorControl:
    def test_missing_gitignore_is_silent(self, tmp_path):
        # No ignore files at all — every .py is returned, no error raised.
        _write(tmp_path / "x.py")
        _write(tmp_path / "pkg" / "y.py")
        assert _names(tmp_path) == ["x.py", "y.py"]
