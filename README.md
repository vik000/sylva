# Sylva

**Sylva turns a codebase into a queryable knowledge graph — then explains it,
draws it, and serves it to your AI assistant.**

Point Sylva at a project and it reads the code, extracts the functions, classes,
and imports, and works out how they connect (what calls what, what imports what).
From that graph it can:

- **explain the project in plain terms** — the real entrypoint, the main
  execution path, the architectural layers, how to run it;
- **draw it** as an interactive map in your browser;
- **serve it to an AI coding assistant** (Claude Code, Cursor, …) over MCP, so the
  assistant understands your code without reading every file;
- and do it all with **one command**: `sylva onboard`.

It's built for **understanding an unfamiliar codebase fast** — for humans and for
agents.

> **Status:** production-ready on **Python** codebases, with **cross-language
> boundaries** (Rust/PyO3 native modules) represented as black boxes. All
> analysis is **deterministic** — Sylva never runs an LLM itself; it *enables*
> one.

---

## The one command

Inside any repo:

```bash
sylva onboard --root .
```

This produces, in one deterministic pass:

| Artifact | What it is |
|---|---|
| `.codemcp/sylva.db` | the code graph |
| `SYLVA.md` | a written **project brief** — archetype, entrypoints, main flow, layers, how to run |
| `DIAGRAMS.md` | **Mermaid diagrams** — system flow, module map, architectural tiers (render on GitHub) |
| `.codemcp/mcp.json` | an **MCP config** to drop into your AI assistant |

An unknown repo becomes documented, navigable, and agent-ready. Everything below
is the individual pieces `onboard` ties together.

---

## What Sylva understands

From the graph alone, deterministically:

- **The global entrypoint** — the *actual* start of the program (a `main`, a
  `__main__` guard, a `console_scripts` target, or a Flask/FastAPI route / click
  command), ranked — not just "every uncalled function".
- **The main spine** — the longest execution path from the entrypoint.
- **Architectural layers** — is it a library, application, or service? For
  services, which symbols are **interface** / **transport** / **business** /
  **data**.
- **What matters** — hubs (most connected), **chokepoints** (betweenness), and
  **gateways** (dominators — code that gates access to large subsystems).
- **Blast radius** — everything that would break if a symbol changed.
- **Test coverage** — overlay red→green, find untested code, map tests to the
  code they exercise.
- **Cross-language boundaries** — calls from Python into a Rust/PyO3 (or C)
  extension, shown as opaque "black box" nodes instead of vanishing.

---

## Requirements

1. **Python 3.9+** — `python3 --version`
2. **The Rust toolchain** (to build Sylva once) — https://rustup.rs
   ```
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
   ```

## Install (one time — compile Sylva)

From the Sylva project folder:

```bash
python3 -m venv .venv          # 1. isolated Python environment
source .venv/bin/activate      # 2. activate it
pip install maturin            # 3. the build tool
maturin develop --release      # 4. compile Sylva & install the `sylva` command
```

You get a `sylva` command. You never recompile — just `source .venv/bin/activate`
in a new terminal (from the Sylva folder), then run `sylva` from anywhere.

---

## Explore visually

```bash
cd /path/to/your/repo
sylva analyze --root .     # build the graph (or use `sylva onboard --root .`)
sylva serve-ui             # open the interactive map at http://localhost:7700
```

In the browser you can:

- **Zoom / pan / drag** nodes; **click** a node for its details and neighbours.
- **Filter** by name/file; toggle functions / classes / imports / **foreign
  modules**.
- **▤ System flow** — a whole-project block diagram rooted at the inferred
  entrypoint, main spine highlighted.
- Click an **entrypoint** in the sidebar to flow it; **◎ focus** a node's
  neighbourhood; **▸ flow** / **◆ data flow** / **▸ execution path** from any
  symbol.
- **Coverage mode** — colour red→green by test coverage.
- **Module view** — collapse to files or packages.

Bigger nodes are more connected. Foreign (Rust) modules are amber squares.
`Ctrl-C` stops the server.

Sylva writes `.codemcp/` (the graph) relative to your current directory and
respects the repo's `.gitignore`. Add `.codemcp/` to that repo's `.gitignore`.

---

## Connect an AI assistant (MCP)

Generate a ready-to-use config:

```bash
sylva init-mcp --db .codemcp/sylva.db
```

This writes `.codemcp/mcp.json` — add its `mcpServers` entry to your Claude Code /
MCP client config. The assistant can then call these tools:

| Tool | Answers |
|---|---|
| `search_symbol` | where is this symbol defined? |
| `get_source` | show me its actual current code |
| `get_callers` / `get_dependencies` | who calls it / what does it use? |
| `trace_calls` | trace call chains to N depth |
| `blast_radius` | what breaks if I change this? |
| `get_architecture` | modules, hubs, entry points |
| `infer_entrypoints` | the ranked, real entrypoints |
| `main_spine` | the main execution path |
| `infer_layers` | archetype + per-symbol architectural layer |
| `centrality` | chokepoints (betweenness) + gateways (dominators) |
| `get_coverage` / `get_uncovered_paths` / `get_test_coverage` | test-coverage queries |

The server speaks the MCP handshake (`initialize` / `tools/list` / `tools/call`)
and plain JSON-RPC.

---

## Command reference

| Command | What it does |
|---|---|
| `sylva onboard --root <dir>` | **All-in-one:** graph + brief + diagrams + MCP config |
| `sylva analyze --root <dir>` | Scan a codebase into the graph database |
| `sylva serve-ui [--port N] [--no-open]` | Interactive graph in a browser |
| `sylva brief [--out SYLVA.md]` | Write the project instruction brief |
| `sylva diagram [--out DIAGRAMS.md]` | Write Mermaid workflow diagrams |
| `sylva init-mcp [--out .codemcp]` | Write a per-project MCP scaffold |
| `sylva serve` | Run the MCP server over stdio (for AI tools) |
| `sylva export-viz [--out <dir>]` | Write `visualisation/graph.json` |

All commands default `--db` to `.codemcp/sylva.db`.

## Optional skills

The `skills/` folder ships optional Claude Code skills that wrap the deterministic
commands and let an agent enrich the output: **sylva-onboard**, **sylva-brief**,
**sylva-diagram**.

---

## Add test coverage to the map

If your project emits an LCOV or Cobertura report (e.g.
`coverage run -m pytest && coverage lcov`):

```python
import sylva
cov = sylva.parse_coverage("coverage.lcov", "lcov")
sylva.apply_coverage(".codemcp/sylva.db", cov)
```

Reload `sylva serve-ui` and switch on **Coverage mode**.

---

## Good to know

- **Deterministic, no LLM.** All of Sylva's analysis is mechanical and
  reproducible — same graph, same output. It's built to *feed* an LLM, not be one.
- **Languages.** Full parsing is Python today; Rust/PyO3 (and C) modules are
  represented by their **export surface** as black boxes. More full extractors
  are planned.
- **Edges are precise, not exhaustive.** Calls/imports resolve by name with
  same-file, import-aware, and light **type-aware** (`self.method()`,
  `x = Foo(); x.m()`) disambiguation. Sylva never invents a wrong link;
  genuinely-ambiguous ones are skipped rather than guessed.
- **Re-run any time.** `analyze` / `onboard` are safe to re-run; they refresh
  the graph idempotently.
- **`SYLVA_LOG=1`** for verbose, per-item diagnostics.
