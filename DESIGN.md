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
