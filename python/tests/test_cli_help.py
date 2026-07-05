"""CLI: bare `sylva` shows a friendly quickstart instead of a terse error.

Running the command with no subcommand should print the quickstart + the list
of available commands and exit 0 (there is no work to fail at), rather than
argparse's one-line "arguments are required" error.
"""

import pytest

import sylva.__main__ as cli


def test_bare_invocation_prints_quickstart(capsys):
    rc = cli.main([])
    out = capsys.readouterr().out
    assert rc == 0
    # Purpose blurb + the four documented commands are all shown.
    assert "codebase knowledge graph" in out.lower()
    for cmd in ("analyze", "serve-ui", "init-mcp", "serve"):
        assert cmd in out


def test_unknown_command_still_errors(capsys):
    # A wrong command is still a usage error (argparse exits non-zero).
    with pytest.raises(SystemExit) as exc:
        cli.main(["bogus"])
    assert exc.value.code != 0


def test_help_flag_lists_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "analyze" in out and "init-mcp" in out
