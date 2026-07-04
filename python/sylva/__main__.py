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

    ui = sub.add_parser("serve-ui", help="Serve the interactive visualisation UI")
    ui.add_argument("--db", required=True, help="Path to the sylva.db graph database")
    ui.add_argument("--port", type=int, default=7700, help="Port to serve on (default 7700)")
    ui.add_argument("--no-open", action="store_true", help="Do not open the browser")

    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(args.db)

    if args.command == "serve-ui":
        from .viz.server import serve as serve_ui

        try:
            return serve_ui(args.db, port=args.port, open_browser=not args.no_open)
        except OSError as e:
            print(f"sylva: {e}", file=sys.stderr)
            return 1

    parser.error(f"unknown command: {args.command}")  # unreachable via argparse


if __name__ == "__main__":
    sys.exit(main())
