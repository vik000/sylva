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
    im.add_argument(
        "--mcp-json", nargs="?", const=".mcp.json", default=None, metavar="PATH",
        help="Also write/merge a client config at PATH (default .mcp.json at the "
             "project root) that Claude Code auto-loads — no manual `claude mcp add`",
    )

    br = sub.add_parser("brief", help="Generate a project instruction brief (SYLVA.md)")
    br.add_argument("--db", default=DEFAULT_DB, help=f"Graph database path (default {DEFAULT_DB})")
    br.add_argument("--out", default="SYLVA.md", help="Output file (default SYLVA.md)")

    dg = sub.add_parser("diagram", help="Generate Mermaid workflow diagrams (DIAGRAMS.md)")
    dg.add_argument("--db", default=DEFAULT_DB, help=f"Graph database path (default {DEFAULT_DB})")
    dg.add_argument("--out", default="DIAGRAMS.md", help="Output file (default DIAGRAMS.md)")

    rp = sub.add_parser("report", help="Generate a detailed health/risk report (report/REPORT.md)")
    rp.add_argument("--db", default=DEFAULT_DB, help=f"Graph database path (default {DEFAULT_DB})")
    rp.add_argument("--out", default="report", help="Output directory (default report)")

    ob = sub.add_parser(
        "onboard", help="One command: index + brief + diagrams + MCP scaffold"
    )
    ob.add_argument("--root", required=True, help="Directory (codebase) to onboard")
    ob.add_argument("--db", default=DEFAULT_DB, help=f"Graph database path (default {DEFAULT_DB})")

    cov = sub.add_parser("coverage", help="Attach a coverage report to the graph (module colours + report)")
    cov.add_argument("--db", default=DEFAULT_DB, help=f"Graph database path (default {DEFAULT_DB})")
    cov.add_argument("--report", "--lcov", dest="report", help="LCOV/Cobertura report to ingest")
    cov.add_argument("--format", default="lcov", choices=["lcov", "cobertura"], help="Report format (default lcov)")
    cov.add_argument("--run", metavar="CMD", help="Run this test command (in --root) to produce the report first, then ingest")
    cov.add_argument("--root", default=".", help="Project root for --run (default .)")

    lp = sub.add_parser("logic-paths", help="Turn per-test coverage into execution-path block diagrams")
    lp.add_argument("--db", default=DEFAULT_DB, help=f"Graph database path (default {DEFAULT_DB})")
    lp.add_argument("--coverage-file", default=".coverage", help="coverage.py data file (default .coverage)")
    lp.add_argument("--run", metavar="CMD", help="Run this test command with per-test contexts (in --root), then ingest")
    lp.add_argument("--source", help="Package to measure when using --run (coverage `source`)")
    lp.add_argument("--root", default=".", help="Project root for --run (default .)")

    un = sub.add_parser("understand", help="Index the repo and report which entrypoints have no test (agent plan)")
    un.add_argument("--root", required=True, help="Directory (codebase) to understand")
    un.add_argument("--db", default=DEFAULT_DB, help=f"Graph database path (default {DEFAULT_DB})")
    un.add_argument("--json", action="store_true", help="Emit the plan as JSON")

    tr = sub.add_parser("trace", help="Run the test suite under the runtime tracer and record real call-order execution flows")
    tr.add_argument("--db", default=DEFAULT_DB, help=f"Graph database path (default {DEFAULT_DB})")
    tr.add_argument("--root", default=".", help="Project root to run tests in (default .)")
    tr.add_argument("--tests", default="", help="Extra pytest args, e.g. a path or -k expr")
    tr.add_argument("--trace-file", help="Ingest an existing tracer JSON instead of running tests")

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
        import shutil

        # Resolve the absolute path to the `sylva` executable so the generated
        # config launches correctly even when sylva lives in a venv that isn't on
        # the MCP client's PATH.
        bindir = os.path.dirname(sys.executable)
        candidate = os.path.join(bindir, "sylva")
        sylva_cmd = candidate if os.path.exists(candidate) else (shutil.which("sylva") or "sylva")
        try:
            cfg = sylva.init_mcp(args.db, args.out, sylva_cmd)
        except OSError as e:
            print(f"sylva: {e}", file=sys.stderr)
            return 1
        print(f"sylva: wrote MCP scaffold -> {cfg}  (command: {sylva_cmd})")

        if args.mcp_json:
            import json as _json

            server = {"command": sylva_cmd, "args": ["serve", "--db", os.path.abspath(args.db)]}
            data = {"mcpServers": {}}
            if os.path.exists(args.mcp_json):
                try:
                    with open(args.mcp_json) as f:
                        data = _json.load(f)
                except (OSError, ValueError):
                    data = {"mcpServers": {}}  # unreadable/invalid — start fresh
            data.setdefault("mcpServers", {})["sylva"] = server  # merge, keep others
            with open(args.mcp_json, "w") as f:
                _json.dump(data, f, indent=2)
            print(f"sylva: wrote client config -> {args.mcp_json}  (merged the 'sylva' server)")
            print("sylva: open this project in Claude Code — it auto-loads .mcp.json.")
        else:
            print("sylva: register it with your client, e.g.:")
            print(f"       claude mcp add sylva -- {sylva_cmd} serve --db {os.path.abspath(args.db)}")
            print("sylva: or re-run with --mcp-json to write a root .mcp.json Claude Code auto-loads.")
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

    if args.command == "report":
        import json as _json

        from .report import generate_report, report_data

        try:
            md = generate_report(args.db)
            data = report_data(args.db)
        except FileNotFoundError as e:
            print(f"sylva: {e}", file=sys.stderr)
            return 1
        os.makedirs(args.out, exist_ok=True)
        md_path = os.path.join(args.out, "REPORT.md")
        json_path = os.path.join(args.out, "report.json")
        with open(md_path, "w") as f:
            f.write(md)
        with open(json_path, "w") as f:
            _json.dump(data, f, indent=2, sort_keys=True)
        print(f"sylva: wrote report -> {md_path} and {json_path}")
        return 0

    if args.command == "coverage":
        from . import pipeline

        report = args.report
        try:
            if args.run:
                cov_file = pipeline.run_tests(args.root, args.run, per_test=False)
                if not report:
                    # After a run, look for a produced LCOV report.
                    guess = os.path.join(args.root, "coverage.lcov")
                    report = guess if os.path.exists(guess) else None
            if not report:
                print("sylva: no coverage report to ingest (pass --report FILE, or --run a command that writes coverage.lcov)", file=sys.stderr)
                return 1
            n = pipeline.ingest_coverage(args.db, report, args.format)
        except (FileNotFoundError, ValueError) as e:
            print(f"sylva: {e}", file=sys.stderr)
            return 1
        print(f"sylva: applied coverage for {n} file(s) -> {args.db}")
        print("sylva: reload `sylva serve-ui` and switch on Coverage mode.")
        return 0

    if args.command == "logic-paths":
        from . import pipeline

        cov_file = args.coverage_file
        try:
            if args.run:
                cov_file = pipeline.run_tests(args.root, args.run, per_test=True, source=args.source)
            result = pipeline.ingest_logic_paths(args.db, cov_file)
        except (FileNotFoundError, ValueError) as e:
            print(f"sylva: {e}", file=sys.stderr)
            return 1
        print(f"sylva: mapped {result['tests']} test(s) -> {result['edges']} test_covers edge(s)")
        print("sylva: reload `sylva serve-ui` -> sidebar 'Logic paths (from tests)' -> click a test.")
        return 0

    if args.command == "understand":
        import json as _json

        from . import pipeline
        from .onboard import index_codebase

        if not os.path.isdir(args.root):
            print(f"sylva: not a directory: {args.root}", file=sys.stderr)
            return 1
        index_codebase(args.root, args.db)
        try:
            plan = pipeline.test_plan(args.db)
        except FileNotFoundError as e:
            print(f"sylva: {e}", file=sys.stderr)
            return 1
        if args.json:
            print(_json.dumps(plan, indent=2))
            return 0
        untested = plan["untested"]
        print(f"\nsylva: {len(plan['entrypoints'])} entrypoint(s); {len(untested)} without a test.\n")
        for p in plan["entrypoints"]:
            mark = "  " if p["has_test"] else "->"
            star = "*" if p["primary"] else " "
            tag = "tested" if p["has_test"] else "NO TEST"
            print(f" {mark}{star} {p['entrypoint']:<28} [{tag}]  {p['reason']}  ({p['file']})")
        if untested:
            print("\nsylva: write e2e tests for the '-> ' entrypoints, then run:")
            print("       sylva logic-paths --run \"coverage run -m pytest\" --source <pkg>")
        return 0

    if args.command == "trace":
        import subprocess
        import tempfile

        from .tracing import ingest_trace

        trace_file = args.trace_file
        try:
            if not trace_file:
                fd, trace_file = tempfile.mkstemp(prefix="sylva-trace-", suffix=".json")
                os.close(fd)
                env = dict(os.environ,
                           SYLVA_TRACE_OUT=trace_file,
                           SYLVA_TRACE_ROOT=os.path.abspath(args.root))
                cmd = [sys.executable, "-m", "pytest", "-p", "sylva.tracer"]
                if args.tests:
                    cmd += args.tests.split()
                print(f"sylva: tracing `{' '.join(cmd[2:])}` in {args.root} ...")
                subprocess.run(cmd, cwd=args.root, env=env, check=False)
            result = ingest_trace(args.db, trace_file)
        except FileNotFoundError as e:
            print(f"sylva: {e}", file=sys.stderr)
            return 1
        print(f"sylva: recorded {result['tests']} test(s), {result['calls']} ordered call(s) -> {args.db}")
        print("sylva: reload `sylva serve-ui` -> sidebar 'Execution traces (real order)' -> click a test.")
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
