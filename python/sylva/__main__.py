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

DEFAULT_DB = ".codemcp/sylva.db"


def _analyze(root, db_path):
    """Analyze a codebase into the graph database. Returns a process exit code."""
    if not os.path.isdir(root):
        print(f"sylva: not a directory: {root}", file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    sylva.init_db(db_path)

    files = sylva.walk_python_files(root)
    total_symbols = 0
    skipped = 0
    for path in files:
        try:
            total_symbols += sylva.write_symbols(db_path, path, sylva.extract_symbols(path))
        except Exception as e:  # unreadable / undecodable file — skip, don't abort
            skipped += 1
            if os.environ.get("SYLVA_LOG"):
                print(f"sylva: skipping '{path}': {e}", file=sys.stderr)

    edges = sylva.build_edges(db_path)
    flows = sylva.build_dataflow(db_path)  # Feature 4.12 — static parameter flow

    note = f" ({skipped} skipped)" if skipped else ""
    print(
        f"sylva: analyzed {len(files)} file(s){note}, "
        f"{total_symbols} symbols, {edges} relationships, {flows} data-flow(s) -> {db_path}"
    )
    print(f"sylva: now run  sylva serve-ui --db {db_path}")
    return 0


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
        if not response:
            continue  # JSON-RPC notification (e.g. notifications/initialized): no reply
        sys.stdout.write(response + "\n")
        sys.stdout.flush()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sylva", description="Codebase knowledge graph")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="Analyze a codebase into the graph database")
    analyze.add_argument("--root", required=True, help="Directory (codebase) to analyze")
    analyze.add_argument("--db", default=DEFAULT_DB, help=f"Graph database path (default {DEFAULT_DB})")

    serve = sub.add_parser("serve", help="Start the MCP server over stdio")
    serve.add_argument("--db", default=DEFAULT_DB, help=f"Graph database path (default {DEFAULT_DB})")

    ui = sub.add_parser("serve-ui", help="Serve the interactive visualisation UI")
    ui.add_argument("--db", default=DEFAULT_DB, help=f"Graph database path (default {DEFAULT_DB})")
    ui.add_argument("--port", type=int, default=7700, help="Port to serve on (default 7700)")
    ui.add_argument("--no-open", action="store_true", help="Do not open the browser")

    ex = sub.add_parser("export-viz", help="Write visualisation/graph.json from the graph")
    ex.add_argument("--db", default=DEFAULT_DB, help=f"Graph database path (default {DEFAULT_DB})")
    ex.add_argument("--out", default="visualisation", help="Output directory (default visualisation)")

    im = sub.add_parser("init-mcp", help="Write a per-project MCP scaffold for agent access")
    im.add_argument("--db", default=DEFAULT_DB, help=f"Graph database path (default {DEFAULT_DB})")
    im.add_argument("--out", default=".codemcp", help="Output directory (default .codemcp)")

    args = parser.parse_args(argv)

    if args.command == "analyze":
        return _analyze(args.root, args.db)

    if args.command == "serve":
        return _serve(args.db)

    if args.command == "serve-ui":
        from .viz.server import serve as serve_ui

        try:
            return serve_ui(args.db, port=args.port, open_browser=not args.no_open)
        except OSError as e:
            print(f"sylva: {e}", file=sys.stderr)
            return 1

    if args.command == "export-viz":
        from .viz.export import export_graph_json

        try:
            out = export_graph_json(args.db, args.out)
        except FileNotFoundError as e:
            print(f"sylva: {e}", file=sys.stderr)
            return 1
        print(f"sylva: wrote {out}")
        return 0

    if args.command == "init-mcp":
        try:
            cfg = sylva.init_mcp(args.db, args.out)
        except OSError as e:
            print(f"sylva: {e}", file=sys.stderr)
            return 1
        print(f"sylva: wrote MCP scaffold -> {cfg}")
        print("sylva: add its contents to your Claude Code / MCP client config to query this codebase.")
        return 0

    parser.error(f"unknown command: {args.command}")  # unreachable via argparse


if __name__ == "__main__":
    sys.exit(main())
