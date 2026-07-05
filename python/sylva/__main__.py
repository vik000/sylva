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
    from .onboard import index_codebase

    if not os.path.isdir(root):
        print(f"sylva: not a directory: {root}", file=sys.stderr)
        return 1

    idx = index_codebase(root, db_path, log=bool(os.environ.get("SYLVA_LOG")))

    note = f" ({idx['skipped']} skipped)" if idx["skipped"] else ""
    fnote = f", {idx['foreign']} foreign export(s)" if idx["foreign"] else ""
    print(
        f"sylva: analyzed {idx['files']} file(s){note}, "
        f"{idx['symbols']} symbols, {idx['edges']} relationships, "
        f"{idx['flows']} data-flow(s){fnote} -> {db_path}"
    )
    if idx.get("by_language"):
        langs = ", ".join(f"{k}: {v}" for k, v in sorted(idx["by_language"].items()))
        print(f"sylva: by language — {langs}")
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


QUICKSTART = """\
Sylva — a codebase knowledge graph for AI agents.

Sylva parses your code, builds a graph of its symbols and relationships, and
serves it over MCP (for agents like Claude Code) and an interactive web UI.

Getting started (run these from your project's root):

  1. sylva analyze --root .        Index the codebase into .codemcp/sylva.db
  2. sylva serve-ui                Explore it in the browser (localhost:7700)
  3. sylva init-mcp                Wire it into Claude Code / an MCP client
  4. sylva serve                   Run the MCP server directly (stdio)

Run 'sylva <command> --help' for a command's options.
"""


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="sylva",
        description="Codebase knowledge graph — parses code into a queryable "
        "graph, served over MCP and a web UI.",
    )
    # Not required: a bare `sylva` prints a friendly quickstart instead of a
    # terse argparse error.
    sub = parser.add_subparsers(dest="command", required=False)

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

    br = sub.add_parser("brief", help="Generate a project instruction brief (SYLVA.md)")
    br.add_argument("--db", default=DEFAULT_DB, help=f"Graph database path (default {DEFAULT_DB})")
    br.add_argument("--out", default="SYLVA.md", help="Output file (default SYLVA.md)")

    dg = sub.add_parser("diagram", help="Generate Mermaid workflow diagrams (DIAGRAMS.md)")
    dg.add_argument("--db", default=DEFAULT_DB, help=f"Graph database path (default {DEFAULT_DB})")
    dg.add_argument("--out", default="DIAGRAMS.md", help="Output file (default DIAGRAMS.md)")

    ob = sub.add_parser(
        "onboard", help="One command: index + brief + diagrams + MCP scaffold"
    )
    ob.add_argument("--root", required=True, help="Directory (codebase) to onboard")
    ob.add_argument("--db", default=DEFAULT_DB, help=f"Graph database path (default {DEFAULT_DB})")

    ex2 = sub.add_parser(
        "expose", help="Generate an MCP server exposing allowlisted repo functions"
    )
    ex2.add_argument("--root", required=True, help="Project root")
    ex2.add_argument("--db", default=DEFAULT_DB, help=f"Graph database path (default {DEFAULT_DB})")
    ex2.add_argument("--config", help="Allowlist file (default <root>/.codemcp/expose.toml)")
    ex2.add_argument("--out", help="Output server file (default <root>/.codemcp/functions_server.py)")

    args = parser.parse_args(argv)

    # Bare `sylva` (no command): show the quickstart + available commands, then
    # exit 0 — an invocation with no work to do is not an error.
    if args.command is None:
        print(QUICKSTART)
        parser.print_help()
        return 0

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

    if args.command == "brief":
        from .report import generate_brief

        try:
            md = generate_brief(args.db)
        except FileNotFoundError as e:
            print(f"sylva: {e}", file=sys.stderr)
            return 1
        with open(args.out, "w") as f:
            f.write(md)
        print(f"sylva: wrote project brief -> {args.out}")
        return 0

    if args.command == "diagram":
        from .diagrams import generate_diagrams

        try:
            md = generate_diagrams(args.db)
        except FileNotFoundError as e:
            print(f"sylva: {e}", file=sys.stderr)
            return 1
        with open(args.out, "w") as f:
            f.write(md)
        print(f"sylva: wrote diagrams -> {args.out}")
        return 0

    if args.command == "expose":
        from .expose import generate_server, parse_allowlist

        if not os.path.isdir(args.root):
            print(f"sylva: not a directory: {args.root}", file=sys.stderr)
            return 1
        config = args.config or os.path.join(args.root, ".codemcp", "expose.toml")
        allowlist = {"functions": [], "modules": []}
        if os.path.isfile(config):
            with open(config) as f:
                allowlist = parse_allowlist(f.read())
        else:
            print(f"sylva: no allowlist at {config} — generating an empty server. "
                  "List functions/modules there to expose them.", file=sys.stderr)
        db = args.db if args.db != DEFAULT_DB else os.path.join(args.root, DEFAULT_DB)
        try:
            code, count, warnings = generate_server(db, args.root, allowlist)
        except FileNotFoundError as e:
            print(f"sylva: {e}", file=sys.stderr)
            return 1
        out = args.out or os.path.join(args.root, ".codemcp", "functions_server.py")
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w") as f:
            f.write(code)
        for w in warnings:
            print(f"sylva: warning: {w}", file=sys.stderr)
        print(f"sylva: wrote MCP function server ({count} tool(s)) -> {out}")
        print(f"sylva: REVIEW it (it executes these functions), then run:  python {out}")
        return 0

    if args.command == "onboard":
        from .onboard import onboard

        if not os.path.isdir(args.root):
            print(f"sylva: not a directory: {args.root}", file=sys.stderr)
            return 1
        # Write all artifacts *into* the onboarded project, not the cwd.
        root = args.root
        db = args.db if args.db != DEFAULT_DB else os.path.join(root, DEFAULT_DB)
        result = onboard(
            root,
            db,
            brief_out=os.path.join(root, "SYLVA.md"),
            diagram_out=os.path.join(root, "DIAGRAMS.md"),
            mcp_out=os.path.join(root, ".codemcp"),
        )
        idx, art = result["index"], result["artifacts"]
        print(
            f"sylva: onboarded {args.root} — {idx['symbols']} symbols, "
            f"{idx['edges']} relationships, {idx['foreign']} foreign export(s)."
        )
        for label, key in (("graph", "db"), ("brief", "brief"), ("diagrams", "diagrams"), ("MCP config", "mcp")):
            if key in art:
                print(f"sylva:   {label:<11} -> {art[key]}")
            elif f"{key}_error" in art:
                print(f"sylva:   {label:<11} FAILED: {art[key + '_error']}", file=sys.stderr)
        print("sylva: explore with  sylva serve-ui  or connect the MCP scaffold to your agent.")
        return 0

    parser.error(f"unknown command: {args.command}")  # unreachable via argparse


if __name__ == "__main__":
    sys.exit(main())
