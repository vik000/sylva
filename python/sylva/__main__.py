"""Command-line entry point for Sylva.

`sylva serve --db <path>` starts the MCP server: a thin stdio loop that reads
one JSON-RPC request per line, dispatches it to the Rust `handle_request`
handler, and writes the JSON-RPC response line back. Keeping the loop here (and
the query logic in Rust) means the handler stays trivially unit-testable while
the CLI stays a thin shell.
"""

import argparse
import os
import sys

import sylva


def _serve(db_path):
    """Run the stdio JSON-RPC loop. Returns a process exit code."""
    # Error control: a missing database is reported clearly at startup, before
    # we begin reading requests.
    if not os.path.exists(db_path):
        print(f"sylva: database not found: {db_path}", file=sys.stderr)
        return 1

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        response = sylva.handle_request(db_path, line)
        sys.stdout.write(response + "\n")
        sys.stdout.flush()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sylva", description="Codebase knowledge graph")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Start the MCP server over stdio")
    serve.add_argument("--db", required=True, help="Path to the sylva.db graph database")

    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(args.db)

    parser.error(f"unknown command: {args.command}")  # unreachable via argparse


if __name__ == "__main__":
    sys.exit(main())
