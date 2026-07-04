# Sylva

**Sylva turns a codebase into a queryable knowledge graph — then shows it to you.**

Point Sylva at a Python project and it reads every file, extracts the functions,
classes, and imports, and works out how they connect (what calls what, what
imports what). It stores all of this in a single small database file, and can
then draw it as an **interactive map of your code** in your browser — or serve
it to an AI coding assistant so the assistant can understand your code without
reading every file.

It's useful for **exploring an unfamiliar codebase**, **finding the important /
central pieces**, **seeing what a change might break**, and **spotting untested
code**.

> **Status:** works today on **Python** codebases. Analysis and the interactive
> visualisation are ready to use.

---

## What you get

- **A map of your code** — every function/class as a node, every call and import
  as a link, in an interactive, zoomable, filterable diagram.
- **The big picture** — the most-connected "hub" functions and the entry points.
- **Coverage overlay** (optional) — colour the map red→green by test coverage.
- **An MCP server** — expose the graph to AI tools (Claude Code, etc.).

---

## Requirements

You need two things installed first:

1. **Python 3.9 or newer** — check with `python3 --version`
2. **The Rust toolchain** (used to build Sylva once) — if you don't have it:
   ```
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
   ```
   (or see https://rustup.rs)

---

## Install (one time)

From the Sylva project folder:

```bash
python3 -m venv .venv          # create an isolated environment
source .venv/bin/activate      # turn it on (do this in each new terminal)
pip install maturin            # the build tool
maturin develop --release      # build & install Sylva into the environment
```

That's it — you now have a `sylva` command available while the environment is
active. (In a new terminal, just re-run `source .venv/bin/activate`.)

---

## Try it on a repo (2 steps)

### 1. Analyze your code

```bash
sylva analyze --root /path/to/your/repo
```

This scans the repo and builds the graph. You'll see something like:

```
sylva: analyzed 83 file(s), 2270 symbols, 1320 relationships -> .codemcp/sylva.db
sylva: now run  sylva serve-ui --db .codemcp/sylva.db
```

The graph is saved to `.codemcp/sylva.db`. (Sylva respects your `.gitignore`, so
it skips virtualenvs, build folders, etc.)

### 2. See the map

```bash
sylva serve-ui
```

This opens your browser at **http://localhost:7700** with the interactive graph.

**In the diagram you can:**

- **Scroll** to zoom, **drag the background** to pan.
- **Drag a node** to move it; **click a node** to highlight what it connects to
  and see its details (file, line, degree, coverage).
- **Filter** by name/file, or toggle functions / classes / imports on and off.
- Turn on **Coverage mode** to colour nodes red→green by test coverage.

Bigger nodes are more connected — a quick way to spot the important code.

Press `Ctrl-C` in the terminal to stop the server.

> Tip: both commands default to the same database (`.codemcp/sylva.db`), so you
> can run `sylva analyze --root <repo>` then just `sylva serve-ui`. Use `--db` on
> both if you want to keep graphs for several repos side by side.

---

## Optional: add test coverage to the map

If your project produces a coverage report (LCOV or Cobertura XML — e.g.
`coverage run -m pytest && coverage lcov`), you can overlay it:

```python
import sylva
cov = sylva.parse_coverage("coverage.lcov", "lcov")
sylva.apply_coverage(".codemcp/sylva.db", cov)
```

Then reload `sylva serve-ui` and switch on **Coverage mode**.

---

## Optional: connect an AI assistant (MCP)

Sylva can serve the graph over the Model Context Protocol so an AI coding tool
can query it:

```bash
sylva serve --db .codemcp/sylva.db
```

It speaks JSON-RPC over stdin/stdout and exposes tools like `search_symbol`,
`get_callers`, `get_dependencies`, `get_coverage`, and `get_uncovered_paths`.

---

## Command reference

| Command | What it does |
|---|---|
| `sylva analyze --root <dir> [--db <path>]` | Scan a codebase into the graph database |
| `sylva serve-ui [--db <path>] [--port N] [--no-open]` | Open the interactive graph in a browser |
| `sylva export-viz [--db <path>] [--out <dir>]` | Write the graph to `visualisation/graph.json` |
| `sylva serve [--db <path>]` | Run the MCP server (for AI tools) over stdio |

All commands default `--db` to `.codemcp/sylva.db`.

---

## Good to know

- **Python only, for now.** Sylva currently understands Python (`.py`) files.
  Support for more languages is planned.
- **Relationships are best-effort.** Call/import links are resolved by name (with
  same-file and import-aware disambiguation). Sylva never invents a wrong link,
  but some calls to same-named methods across many classes are left out rather
  than guessed.
- **Re-run any time.** `sylva analyze` is safe to re-run; it refreshes the graph.
- **Set `SYLVA_LOG=1`** for verbose, per-item diagnostics.
