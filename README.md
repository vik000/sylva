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

> **Status:** production-ready. **Polyglot** — full parsing for **Python,
> TypeScript, and JavaScript**, with **cross-language boundaries** (Rust/PyO3
> native modules) represented as black boxes. All analysis is **deterministic** —
> Sylva never runs an LLM itself; it *enables* one.

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
- **Multiple languages** — **Python, TypeScript, and JavaScript** are fully
  parsed (symbols *and* call edges) in one graph; **Rust/PyO3 (or C)** modules
  are shown as opaque "black box" nodes by their export surface instead of
  vanishing.
- **A health report** — architecture, coverage gaps, change-risk (blast radius
  of the hubs), and structural gaps (orphans), as a diffable `REPORT.md`.

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

Generate the config **and** wire it into Claude Code in one step:

```bash
sylva init-mcp --db .codemcp/sylva.db --mcp-json
```

`--mcp-json` writes a `.mcp.json` at the **project root** (merging with any
existing one, so other servers are kept) — Claude Code **auto-loads** it when you
open the project. It uses the **absolute path** to the `sylva` executable, so it
launches even when `sylva` lives in a venv that isn't on your PATH. Then open the
project in Claude Code and check `/mcp` shows **sylva** connected.

> **Note on locations:** without `--mcp-json`, `init-mcp` only writes
> `.codemcp/mcp.json` (a scaffold) — most clients do **not** read that path. Use
> `--mcp-json` for the root `.mcp.json` Claude Code reads, or register it manually:
> ```bash
> claude mcp add sylva -- /abs/path/to/.venv/bin/sylva serve --db /abs/path/to/.codemcp/sylva.db
> ```
> (`init-mcp` prints this exact command for your project.)

Other MCP clients: add the `mcpServers` entry from `.codemcp/mcp.json` to the
client's own config. The assistant can then call these tools:

| Tool | Answers |
|---|---|
| `get_overview` | **start here** — archetype, entrypoints, layers, top modules, hubs, spine, in one call |
| `get_outline` | a file's **skeleton** — every symbol's signature + docstring + line span, **no bodies** |
| `list_symbols` | inventory of functions/classes (filter by file/kind) — cheap, no source |
| `neighborhood` | a symbol's N-hop local structure (calls/imports/inherits) |
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

**Navigate, don't read.** The efficient loop for an agent is `get_overview` to
orient → `get_outline` to see a file's shape → `get_source` to pull *only* the
one function it needs. That traverses the deterministic tree instead of opening
whole files — far fewer tokens, and the structure is exact (never a guessed
edge). Sylva builds and exposes the tree; the agent builds its understanding on
top.

### Let an agent *call* your code

`init-mcp` gives an assistant **query** access to the graph. To let it **execute**
selected functions, expose them — **one line, nothing exposed by default:**

```bash
sylva expose --root . --functions "mypkg.api:create_user,mypkg.api:delete_user"
# …or a whole module's public functions:
sylva expose --root . --module mypkg.public
```

This writes a standalone, **reviewable** MCP server (`.codemcp/functions_server.py`)
whose tools are exactly those functions, argument schemas derived from each
function's live signature. **You review and run it** — Sylva only *generates*, it
executes nothing. Register the generated server with your client the same way as
`init-mcp` (point `.mcp.json` at `python .codemcp/functions_server.py`).

**No file to manage?** `serve-functions` runs the server directly, reading the
same allowlist at start-up (the allowlist stays your reviewable surface, and it
prints the exposed functions on launch):

```bash
sylva serve-functions --root . --functions "mypkg.api:create_user"
```
Point `.mcp.json` at that command instead of a generated file.

For a larger, version-controlled surface, list targets in `.codemcp/expose.toml`
(`functions = [...]` / `modules = [...]`) instead of the flags. Language-neutral
by design (Python execution backend first).

---

## Command reference

| Command | What it does |
|---|---|
| `sylva onboard --root <dir>` | **All-in-one:** graph + brief + diagrams + MCP config |
| `sylva analyze --root <dir>` | Scan a codebase into the graph database |
| `sylva serve-ui [--port N] [--no-open]` | Interactive graph in a browser |
| `sylva brief [--out SYLVA.md]` | Write the project instruction brief |
| `sylva diagram [--out DIAGRAMS.md]` | Write Mermaid workflow diagrams |
| `sylva report [--out report]` | Write a **health/risk report** (`REPORT.md` + `report.json`) |
| `sylva init-mcp [--mcp-json]` | Write the MCP config (query access); `--mcp-json` wires it into Claude Code's root `.mcp.json` |
| `sylva expose --root <dir>` | Generate an MCP server exposing **allowlisted** repo functions (execute access) |
| `sylva serve` | Run the MCP server over stdio (for AI tools) |
| `sylva export-viz [--out <dir>]` | Write `visualisation/graph.json` |

All commands default `--db` to `.codemcp/sylva.db`.

## Optional skills

The `skills/` folder ships optional Claude Code skills that wrap the deterministic
commands and let an agent enrich the output: **sylva-onboard**, **sylva-brief**,
**sylva-diagram**, and **sylva-generate-tests** (write e2e tests → verified logic
paths).

---

## Add test coverage to the map

Sylva reads **LCOV** or **Cobertura** — a language-neutral format, so the same
overlay works across languages. Generate a report with your usual tool:

| Language | Command → LCOV |
|---|---|
| **Python** | `coverage run -m pytest && coverage lcov` |
| **Rust** | `cargo tarpaulin --out Lcov` |
| **JS / TS** | Istanbul / nyc / jest `--coverage` (writes `lcov.info`) |

Then apply it:

```python
import sylva
cov = sylva.parse_coverage("coverage.lcov", "lcov")   # or "cobertura"
sylva.apply_coverage(".codemcp/sylva.db", cov)
```

Reload `sylva serve-ui` and switch on **Coverage mode**.

---

## Verified logic paths (from tests)

Static views show what *could* execute. If you have tests — or write them — Sylva
can show what *actually* executed, as block diagrams. Sylva runs nothing itself:
it tells you **what to test**, then **ingests** the coverage your normal test run
produces.

**1. Ask what to test.** Sylva ranks the untested logic that matters most
(entrypoints, chokepoints, gateways), with a reason for each:

```bash
python -c "import sylva, json; print(json.dumps(sylva.suggest_test_targets('.codemcp/sylva.db'), indent=2))"
```
```
[ { "symbol": "wsgi_app", "reason": "chokepoint (betweenness 25.0); gateway (dominates 15)", "score": 330 }, … ]
```

(also available as the `suggest_test_targets` MCP tool, so an AI assistant can
target the right code.)

**2. Write end-to-end tests** for the top targets — drive the real entrypoints
(a request to a route, a CLI command, the public API) and assert behaviour.

**3. Run them with per-test coverage** (your normal test workflow — this is the
only step that runs code):

```bash
coverage run --context=test -m pytest
coverage lcov
```

**4. Ingest the coverage** so the paths become queryable:

```python
import sylva
cov = sylva.parse_coverage("coverage.lcov", "lcov")
sylva.apply_coverage(".codemcp/sylva.db", cov)
sylva.map_tests_to_symbols(".codemcp/sylva.db", trace)   # {test: {source_path: [lines]}}
```

**5. See them.** Reload `sylva serve-ui`: each test appears under **"Logic paths
(from tests)"** in the sidebar — click one to render the execution path it
actually exercised as a block diagram.

> The `skills/sylva-generate-tests` skill packages steps 1–4 for an AI assistant
> to run for you.

---

## Good to know

- **Deterministic, no LLM.** All of Sylva's analysis is mechanical and
  reproducible — same graph, same output. It's built to *feed* an LLM, not be one.
- **Languages.** **Python, TypeScript, and JavaScript** are fully parsed
  (symbols and call edges). **Rust/PyO3 (and C)** modules are represented by
  their **export surface** as black boxes, so cross-language calls stay visible.
  (TS/JS import-aware resolution is a planned refinement.)
- **Edges are precise, not exhaustive.** Calls/imports resolve by name with
  same-file, import-aware, and light **type-aware** (`self.method()`,
  `x = Foo(); x.m()`) disambiguation. Sylva never invents a wrong link;
  genuinely-ambiguous ones are skipped rather than guessed.
- **Re-run any time.** `analyze` / `onboard` are safe to re-run; they refresh
  the graph idempotently.
- **`SYLVA_LOG=1`** for verbose, per-item diagnostics.
