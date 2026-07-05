# Changelog

All notable changes to Sylva. Versions follow semver; the history below is
grouped by the epics that shaped each line.

## 3.x — Deterministic understanding, agent access & skills

The 3.0 line makes Sylva a tool that *explains* a codebase, not just maps it —
all deterministically (no LLM in the core).

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
