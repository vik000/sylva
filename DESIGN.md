# Sylva — Design Decisions

## Why Rust + PyO3?

Rust gives us zero-dependency binaries, memory safety, and tree-sitter
performance. PyO3 lets every function be tested from Python before wiring
into the MCP server, giving a clean testability layer without compromising
the binary's portability.

## Why SQLite?

- Zero server, zero config — lives in `.codemcp/sylva.db` inside the repo
- Fast enough for graphs of millions of nodes with proper indexing
- sqlite-vec extension adds vector search without a separate store
- Single file makes it trivial to share, backup, or gitignore

## Graph data model

```
files      (id, path, hash, indexed_at)
symbols    (id, file_id, name, kind, line_start, line_end, docstring, coverage_pct)
edges      (id, src_id, dst_id, kind)   -- kind: calls | imports | test_covers
```

`kind` on edges lets us filter by relationship type cheaply.

## MCP transport

stdio JSON-RPC — the simplest transport, works with every MCP-compatible
agent without any network config. The binary is started by the agent's MCP
config and communicates over stdin/stdout.

## Visualisation

Single self-contained HTML file, no build step, no npm, no framework.

**Revised in Feature 4.4** (from the original "served by the binary, D3,
`include_str!`" plan): the visualisation is **Python-native** (`sylva/viz/`).
The export reads `sylva.db` via stdlib `sqlite3`; the server is the stdlib
`http.server`; the front-end uses **vanilla Canvas + a from-scratch force
simulation** rather than D3. Rationale: the project has no Rust binary target
(it's a cdylib + Python console script), Python's stdlib covers the server for
free, and modern JS/Canvas make D3 unnecessary — d3-force's only real value (the
n-body sim) is small enough to hand-write with a settle-then-freeze strategy for
the 1000-node target (Barnes–Hut left as a future optimisation if live large
graphs are needed).

## Coverage integration

We consume standard LCOV/Cobertura output from:
- Python: `coverage.py`
- Rust: `cargo-tarpaulin` or `llvm-cov`
- TypeScript: `istanbul` / `c8`

All produce LCOV, so one parser covers all languages.

## Language extensibility

Each language is a struct implementing the `Extractor` trait:

```rust
trait Extractor {
    fn extensions(&self) -> &[&str];
    fn extract(&self, path: &Path) -> Result<Vec<Symbol>>;
}
```

New languages register themselves in a map at startup. The core pipeline
never needs to know about specific languages.

---

## Layout decision — layered / block-diagram views (Feature 9.6 spike)

**Question:** the layered renderer (`renderLayered`, used by Features 4.10/4.11/
4.12 and the global 9.4 system-flow view) places nodes by BFS depth with a fixed
within-layer order (alphabetical) and **no crossing minimisation**. At scale this
can tangle. Do we invest in a proper Sugiyama pipeline or adopt an engine
(dagre / elkjs / Graphviz)?

**Evidence** (benchmarked on Flask, `src/`, 806 symbols / 294 resolved call edges):

- The **actual per-entrypoint views are small, not tangled.** The 9.4
  system-flow from the inferred primary was **8 nodes / 9 edges** (widest layer
  4). Static Python call resolution is sparse (many method/dynamic calls don't
  resolve), so a single entrypoint's reachable subgraph stays modest.
- **When a layered graph *is* dense, crossing-minimisation is decisive but
  cheap.** Layering the *whole* call graph (441 nodes, widest layer 312) gave
  **7338 crossings** with the current alphabetical order; a from-scratch
  **barycentre** heuristic (a few down/up sweeps) cut that to **522 — a 93%
  reduction**, with zero new dependencies.

**Decision: keep the from-scratch layout (option a).** A ~50-line barycentre
crossing-minimisation pass recovers ~93% of the achievable readability while
preserving the project's constraints (dependency-free, self-contained
`index.html`, settle-then-freeze Canvas). Bundling **dagre**/**elkjs** (a JS
layout dependency in the self-contained page) or requiring **Graphviz**
server-side is not justified by the marginal gain over barycentre.

**Priority: deferred / pull-when-needed.** The current per-entrypoint views are
small enough that crossings are not yet a real problem, so the barycentre pass is
filed as a follow-up to add only when a real repo shows a genuinely tangled flow
— not built speculatively.

**Orthogonal findings (not layout):** the two real readability limits surfaced
were (1) **entrypoint ambiguity for libraries** — Flask has no single `main`, so
a CLI command (`shell_command`) is a defensible-but-arbitrary primary; and (2)
**sparse call resolution** thinning the flows (ties to type-aware resolution,
issues #36/#45, and import edges #37). Both are separate from layout quality.
