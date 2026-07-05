# Changelog

All notable changes to Sylva. Versions follow semver; the history below is
grouped by the epics that shaped each line.

## 4.x — Polyglot analysis, executable MCP & full reporting

- **4.2.0** — **Runtime call tracer (dynamic tier).** `sylva trace` runs the
  project's tests under a `sys.setprofile` hook and records the **real order**
  functions call each other — the true `test → A → B → C` succession with call
  depth, not a coverage-derived set. Persisted as an ordered `call_trace` table
  and rendered in the viz as a **top-down flowchart of labelled boxes**
  ("Execution traces (real order)" sidebar). This is the first genuinely
  *dynamic* view — everything else stays static/deterministic.

The 4.0 major marks Sylva going **beyond Python**: TypeScript/JavaScript are now
fully parsed alongside Python (Rust stays a black box), an agent can be granted
**execute** access to selected functions, and the analysis ships a **health/risk
report**.

- **4.1.0** — **Pipeline subcommands + the `understand` orchestrator.** New
  native commands: `sylva understand` (index + which entrypoints lack a test),
  `sylva logic-paths` (per-test coverage → execution-path block diagrams — the
  coverage→trace bridge is now one command, reading `.coverage` **dependency-free**
  by decoding coverage.py's `numbits` directly), and `sylva coverage` (LCOV/
  Cobertura overlay). New `sylva-understand` skill: an agent triggers one flow —
  understand → write e2e tests for untested entrypoints → ingest → interactive
  block diagrams. Deterministic throughout; the only LLM step is writing tests.
- **4.0.0** — **Detailed analysis report** (`sylva report` → `report/REPORT.md` +
  `report.json`, Feature 8.1): architecture, coverage gaps, change-risk (blast
  radius of the hubs) and structural gaps (orphans) — deterministic and diffable.
  Major bump consolidating the polyglot + executable-MCP line below.
- **3.22.0** — accessible-function badges + an "accessible only" filter in the
  viz (Feature 8.3) — see the agent-callable surface at a glance.
- **3.21.0** — entrypoint inference for **libraries** (public API surface, 9.7).
- **3.20.0 / 3.19.0** — TypeScript/JavaScript **call-edge resolution** + the
  multi-language pipeline walker (5.6 / 5.5): TS/JS repos now get real graphs.
- **3.18.0** — coverage support for Rust/TS (tarpaulin / Istanbul LCOV, 5.4).
- **3.17.0 / 3.15.0** — **TypeScript/JavaScript** and **Rust** extractors (5.3 / 5.2).
- **3.16.0** — `sylva expose`: turn **allowlisted** repo functions into an
  executable MCP server (Feature 8.5) — opt-in; Sylva generates, you review & run.
- **3.14.0** — pluggable language-extractor **trait** (5.1) — the polyglot foundation.

## 3.x — Deterministic understanding, agent access & skills

The 3.0 line makes Sylva a tool that *explains* a codebase, not just maps it —
all deterministically (no LLM in the core).

- **3.13.0** — **Epic 10 complete.** Verified logic paths (Feature 10.2):
  `suggest_test_targets` recommender + `sylva-generate-tests` skill + a "Logic
  paths (from tests)" viz surface — real execution paths as block diagrams.
- **3.12.0** — `sylva onboard`: one command → graph + brief + diagrams + MCP
  config (Feature 10.4).
- **3.11.0** — Mermaid workflow diagrams (`sylva diagram`, Feature 10.3).
- **3.10.0** — project instruction brief (`sylva brief` → `SYLVA.md`, Feature 10.1).
- **3.9.0** — centrality: betweenness (chokepoints) + dominators (gateways) (9.3).
- **3.8.0** — project archetype + architectural layer inference (9.8).
- **3.7.0** — sidebar navigator: launch flows from inferred entrypoints (9.5).
- **3.6.0 / 3.5.0** — black-box **foreign (Rust/PyO3) modules** + their
  visualisation (5.0 / 5.0.1) — first cross-language support.
- **3.4.0** — type-aware method call resolution (`self.method()`, `x = Foo();
  x.m()`) (7.10) — denser, more precise edges.
- **3.3.0** — global "system flow" whole-project block diagram (9.4).
- **3.2.0** — SCC condensation + longest-path "main spine" (9.2).
- **3.1.0** — framework entrypoint markers: routes / CLI / console_scripts (9.1.1).
- **3.0.0** — **Structural Understanding (Epic 9):** global entrypoint inference
  (9.1). Major bump marking the deterministic-analysis direction.

## 2.x — Richer graph, visualisation & agent integration

- Visualisation depth (Epic 4): call-chain tracing, blast radius, architecture
  summary, interactive graph, coverage overlay, module clustering, focus mode,
  layered flow / execution-path / data-flow views, package architecture map.
- Agent integration (Epics 7/8): real MCP handshake (`initialize` / `tools/list`
  / `tools/call`), per-project MCP scaffold (`init-mcp`), `get_source` tool.

## 1.x — MVP

- Epic 1 — Python parsing (tree-sitter) → SQLite graph → MCP server with the
  core query tools.
- Epic 2 — incremental indexing (hash tracking, file watcher, gitignore-aware
  walking).
- Epic 3 — test-coverage integration (LCOV/Cobertura parsing, symbol
  correlation, test→symbol mapping, module rollups, coverage MCP tools).

---

Every Rust function is exported to Python via PyO3; every feature is implemented
and tested (500+ tests). See `CLAUDE.md` for the full feature backlog and
`DESIGN.md` for architecture decisions.
