# Sylva — Codebase Knowledge Graph

Sylva is a Rust + SQLite tool that parses codebases via tree-sitter, builds a
persistent knowledge graph, and exposes it as an MCP server so AI coding agents
(Claude Code, Cursor, etc.) can query code structure without reading files
one by one.

---

## Core Principles

- Every Rust function is exported to Python via PyO3 — no exceptions
- Each Feature is implemented and tested in Python before moving to the next
- The user manually confirms tests pass before proceeding
- Test files are named `test_epic<N>_<feature>.py` — never overwrite existing ones
- All tests are permanent — they form the regression suite
- Thorough testing: general cases, edge cases, negative cases, error handling
- Every code path ends in: valid output, explicitly ignored, explicitly repaired,
  or explicitly rejected — never silent corruption
- One feature at a time — do not implement ahead

---

## Repository Structure

```
sylva/
├── CLAUDE.md                        # This file
├── DESIGN.md                        # Architecture decisions
├── Cargo.toml
├── pyproject.toml
├── src/
│   └── lib.rs                       # PyO3 module root
├── python/
│   ├── sylva/
│   │   └── __init__.py
│   └── tests/
│       ├── conftest.py
│       └── test_epic<N>_<feature>.py
├── test_data/                       # Sample repos for smoke tests
└── .github/workflows/ci.yml
```

---

## Build Commands

```bash
maturin develop          # compile Rust, install into current venv
pytest python/tests/ -v  # run full test suite
```

---

## PyO3 Export Pattern

Every public Rust function must follow this pattern:

```rust
#[pyfunction]
fn my_function(arg: SomeType) -> PyResult<ReturnType> {
    // implementation
}

#[pymodule]
fn sylva(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(my_function, m)?)?;
    Ok(())
}
```

---

## Robustness Rules

Every code path must end in exactly one of:
- **Valid output** — accepted, correct result
- **Explicitly ignored** — intentional policy, with reason logged
- **Explicitly repaired** — deterministic fix, logged
- **Explicitly rejected** — invalid input, logged with clear message

Never: silent corruption, undefined behaviour, unlogged failures.

---

## Epics and Features

---

### Epic 1 — MVP: Python parsing + MCP server

#### Feature 1.1 — Project scaffold
- Description: Rust workspace, PyO3 bindings, CI, pytest harness
- Inputs: None
- Process: Cargo.toml, src/lib.rs, pyproject.toml, conftest.py, GitHub Actions
- Outputs: `import sylva` succeeds; `pytest python/tests/ -v` passes
- Testing:
  - General: module imports, module name is "sylva"
  - Error control: compilation errors surfaced clearly

#### Feature 1.2 — SQLite schema and migrations
- Description: Define and initialise the graph data model in SQLite
- Inputs: path to `.codemcp/sylva.db`
- Process: Create tables for files, symbols, relationships; run migrations via rusqlite
- Outputs: `sylva.init_db(path)` creates the DB; schema introspectable from Python
- Testing:
  - General: DB created at correct path, all tables present, columns correct
  - Edge: calling init_db twice is idempotent (no error, no duplicate tables)
  - Negative: unwritable path raises clear error
  - Error control: migration errors surface with meaningful message

#### Feature 1.3 — Python file walker
- Description: Walk a directory tree respecting .gitignore and .codemcpignore
- Inputs: root path (str), optional list of extra ignore patterns
- Process: Recurse directories, skip ignored paths, return list of .py file paths
- Outputs: `sylva.walk_python_files(root) -> list[str]`
- Testing:
  - General: finds all .py files in a temp tree
  - Edge: empty directory returns [], single file, nested dirs
  - Negative: non-existent root raises FileNotFoundError
  - Error control: permission errors on subdirs are logged and skipped, not fatal

#### Feature 1.4 — Python AST extractor
- Description: Parse a Python file with tree-sitter and extract symbols
- Inputs: file path (str)
- Process: Parse with tree-sitter-python, extract functions, classes, imports with name/kind/line/docstring
- Outputs: `sylva.extract_symbols(path) -> list[dict]` — each dict has keys: name, kind, line, docstring
- Testing:
  - General: extracts functions, classes, imports from a real .py file
  - Edge: empty file returns [], file with only comments, nested classes
  - Negative: non-existent file raises FileNotFoundError, binary file raises clear error
  - Error control: parse errors return partial results with error flag, not a crash

#### Feature 1.5 — Graph writer
- Description: Write extracted symbols and file records into SQLite
- Inputs: db path, file path, list of symbol dicts
- Process: Upsert file record, upsert symbols, link symbols to file
- Outputs: `sylva.write_symbols(db_path, file_path, symbols)` — returns count of written symbols
- Testing:
  - General: symbols appear in DB after write, file record created
  - Edge: writing same symbols twice is idempotent (upsert, not duplicate)
  - Negative: invalid db path raises error
  - Error control: partial write failure rolls back transaction

#### Feature 1.6 — MCP server: 3 core tools
- Description: Expose the graph over stdio MCP with search_symbol, get_callers, get_dependencies
- Inputs: db path (from config or CLI arg)
- Process: JSON-RPC over stdio; each tool queries SQLite and returns structured JSON
- Outputs: `sylva serve --db .codemcp/sylva.db` starts the server; tools respond correctly
- Testing:
  - General: each tool returns correct results for known graph content
  - Edge: query for non-existent symbol returns empty list, not error
  - Negative: malformed JSON input returns JSON-RPC error response
  - Error control: DB not found returns clear error on startup

---

### Epic 2 — Incremental Indexing

#### Feature 2.1 — File hash tracking
- Description: Store SHA-256 of each file at index time; skip unchanged files on reindex
- Inputs: db path, file path
- Process: Hash file contents, compare to stored hash, return skip/reindex decision
- Outputs: `sylva.file_needs_reindex(db_path, file_path) -> bool`
- Testing:
  - General: unchanged file returns False, modified file returns True, new file returns True
  - Edge: empty file has stable hash, very large file hashes correctly
  - Negative: unreadable file raises error
  - Error control: DB write failure on hash update is logged and retried once

#### Feature 2.2 — File watcher
- Description: Watch a directory for changes and trigger reindex of modified files
- Inputs: root path, db path, debounce ms
- Process: Use notify crate; on file change, reparse and upsert symbols
- Outputs: `sylva.start_watcher(root, db_path)` — non-blocking, returns watcher handle
- Testing:
  - General: modifying a .py file triggers reindex within debounce window
  - Edge: creating a new file indexes it, deleting a file marks it removed in DB
  - Negative: non-existent root raises error on start
  - Error control: watcher errors logged, watcher recovers without crashing

#### Feature 2.3 — gitignore-aware file walking
- Description: Respect .gitignore and .codemcpignore when walking
- Inputs: root path
- Process: Parse .gitignore hierarchy, apply patterns, skip matched paths
- Outputs: `sylva.walk_python_files(root)` (replaces Feature 1.3) respects ignore files
- Testing:
  - General: gitignored files excluded, non-ignored files included
  - Edge: nested .gitignore files respected, .codemcpignore overrides
  - Negative: malformed .gitignore line is skipped with warning, not fatal
  - Error control: missing .gitignore is silently ignored (not an error)

---

### Epic 3 — Test Coverage Integration

#### Feature 3.1 — Parse coverage.py output
- Description: Parse LCOV or Cobertura XML produced by coverage.py
- Inputs: path to coverage report file, format (lcov|cobertura)
- Process: Parse line-level hit/miss data per file
- Outputs: `sylva.parse_coverage(path, format) -> dict[str, dict[int, bool]]` — file → line → covered
- Testing:
  - General: parses real coverage.py LCOV output correctly
  - Edge: file with 0% coverage, file with 100% coverage, missing file entry
  - Negative: invalid format string raises ValueError, corrupt file raises clear error
  - Error control: unrecognised file paths in report are skipped with warning

#### Feature 3.2 — Correlate coverage to symbols
- Description: Match covered lines to symbol boundaries in the graph; attach coverage % as node attribute
- Inputs: db path, coverage dict from Feature 3.1
- Process: For each symbol, check which of its lines are covered; compute coverage %
- Outputs: `sylva.apply_coverage(db_path, coverage)` — updates symbol records in DB
- Testing:
  - General: symbol spanning covered lines gets correct %, uncovered symbol gets 0%
  - Edge: symbol with no lines in coverage report gets null (not 0%), partial coverage
  - Negative: symbol not in DB is skipped without error
  - Error control: DB update failure rolls back per-symbol, not full batch

#### Feature 3.3 — Test-to-symbol mapping
- Description: Build edges from test functions to the symbols they exercise
- Inputs: db path, coverage trace data (per-test line data from coverage.py --source)
- Process: For each test function, find which non-test symbols its lines hit; write test→symbol edges
- Outputs: `sylva.map_tests_to_symbols(db_path, trace)` — returns count of edges written
- Testing:
  - General: test that calls function X produces edge test→X
  - Edge: test that calls nothing produces no edges, fixture-only test
  - Negative: trace referencing unknown symbol is skipped
  - Error control: duplicate edges are upserted, not duplicated

#### Feature 3.4 — Module-level coverage rollup
- Description: Aggregate symbol-level coverage into per-module and per-file totals
- Inputs: db path
- Process: Group symbols by module/file, compute weighted average coverage
- Outputs: `sylva.get_module_coverage(db_path) -> dict[str, float]` — module → coverage %
- Testing:
  - General: module with mixed coverage returns correct weighted average
  - Edge: module with no coverage data returns None, empty module
  - Negative: db not found raises error
  - Error control: symbols with null coverage excluded from average with warning

#### Feature 3.5 — MCP tools for coverage queries
- Description: Expose coverage data via MCP: get_coverage, get_uncovered_paths, get_test_coverage
- Inputs: symbol name or module name
- Process: Query SQLite coverage attributes, return structured JSON
- Outputs: Three new MCP tools alongside existing ones from Feature 1.6
- Testing:
  - General: get_coverage returns correct % for known symbol
  - Edge: get_uncovered_paths returns empty list when fully covered
  - Negative: unknown symbol returns null coverage, not error
  - Error control: DB unavailable returns MCP error response, not crash

---

### Epic 4 — Richer Graph Queries + Visualisation

#### Feature 4.0 — Populate the edges table (call/import relationships)
- Description: Write `calls` and `imports` edges into the graph so relationships
  exist to traverse. Prerequisite for all of Epic 4 (4.1–4.5) and makes
  Feature 1.6's `get_callers` / `get_dependencies` return real data. Sequence
  this BEFORE Feature 4.1, which assumes call edges already exist. (Tracks GitHub issue #27.)
- Inputs: db path (edges are derived during/after symbol writing)
- Process: Two halves, each independently testable —
  - Import edges: extractor reports import targets; writer resolves each target
    name to a symbol already in the graph (cross-file) and inserts an `imports` edge.
  - Call edges: extractor captures call sites; writer resolves each callee name
    to a symbol id and inserts a `calls` edge (document resolution limits:
    same-name collisions, unresolved external calls).
- Outputs: `calls` / `imports` rows present in `edges` after indexing;
  `get_dependencies` and `get_callers` return real results
- Testing:
  - General: `a()` calling `b()` yields edge a→b; import of a known symbol yields an `imports` edge
  - Edge: recursive/self-call handled; re-index is idempotent (no duplicate edges)
  - Negative: import/call to an unindexed target is skipped, not an error
  - Error control: unresolved reference logged and skipped, never a crash or a dangling edge

#### Feature 4.1 — Call chain tracing
- Description: Traverse inbound/outbound call edges to N depth
- Inputs: symbol name, direction (inbound|outbound|both), max depth
- Process: BFS/DFS over call edges in SQLite graph
- Outputs: `sylva.trace_calls(db_path, symbol, direction, depth) -> list[dict]` — structured path
- Testing:
  - General: traces direct caller, multi-hop chain, bidirectional
  - Edge: depth=0 returns just the symbol, circular calls don't loop forever
  - Negative: unknown symbol returns empty list, invalid direction raises ValueError
  - Error control: depth limit enforced strictly, never infinite loop

#### Feature 4.2 — Blast radius analysis
- Description: Given a symbol, return everything that would break if its signature changed
- Inputs: symbol name, db path
- Process: Traverse all inbound call/import edges recursively
- Outputs: `sylva.blast_radius(db_path, symbol) -> list[dict]` — affected symbols with distance
- Testing:
  - General: changing a widely-used utility returns all callers
  - Edge: leaf symbol (nothing calls it) returns empty list
  - Negative: unknown symbol returns empty list
  - Error control: cycle detection prevents infinite traversal

#### Feature 4.3 — Architecture summary tool
- Description: Top-level view of the codebase: modules, entry points, most-connected symbols
- Inputs: db path
- Process: Query symbol degree, identify top-N hubs, list module boundaries
- Outputs: `sylva.get_architecture(db_path) -> dict` — modules, hubs, entry points
- Testing:
  - General: returns correct hub symbols for known graph
  - Edge: single-file repo, repo with no imports
  - Negative: empty DB returns empty architecture, not error
  - Error control: missing DB raises clear error

#### Feature 4.4 — Interactive visualisation UI
- Description: Local web UI served by the binary — force-directed module graph
- Inputs: db path, port (default 7700)
- Process: Export graph to JSON, serve single HTML file with D3 force layout; nodes = modules/symbols, edges = call/import relationships; clickable nodes, filterable by depth or module
- Outputs: `sylva serve-ui --db .codemcp/sylva.db` opens browser at localhost:7700
- Testing:
  - General: server starts, /graph.json returns valid JSON, HTML page loads
  - Edge: empty graph renders without error, large graph (1000+ nodes) loads within 3s
  - Negative: port in use raises clear error with suggestion to use --port
  - Error control: DB not found returns 500 with JSON error body

#### Feature 4.5 — Coverage overlay in visualisation
- Description: Extend the module graph UI with a coverage mode
- Inputs: coverage data already in DB from Epic 3
- Process: Add toggle to UI; in coverage mode, colour nodes red→green by coverage %; show % on hover; highlight untested logic paths
- Outputs: Coverage toggle visible in UI; nodes coloured correctly when enabled
- Testing:
  - General: coverage mode colours nodes correctly for known coverage data
  - Edge: node with null coverage shown as grey, fully covered node is green
  - Negative: coverage toggle when no coverage data shows informative empty state
  - Error control: partial coverage data (some symbols null) renders without crash

---

### Epic 5 — Additional Language Support

#### Feature 5.1 — Pluggable language extractor trait
- Description: Define a clean Rust trait so adding a new language is implementing one module
- Inputs: language name, file extension, tree-sitter grammar
- Process: Trait with extract_symbols method; registration map from extension to extractor
- Outputs: `sylva.list_languages() -> list[str]` returns registered languages
- Testing:
  - General: Python extractor still works after refactor
  - Edge: registering same language twice is idempotent
  - Negative: unknown file extension returns empty symbols, not error
  - Error control: extractor panic is caught and returned as error, not crash

#### Feature 5.2 — Rust extractor
- Description: tree-sitter-rust — extract fns, structs, traits, impl blocks
- Inputs: .rs file path
- Process: Parse with tree-sitter-rust, extract symbols with same interface as Python extractor
- Outputs: `sylva.extract_symbols(path)` works for .rs files
- Testing:
  - General: extracts fn, struct, trait, impl from real .rs file
  - Edge: empty file, file with only comments, nested impls
  - Negative: non-Rust file passed to Rust extractor raises clear error
  - Error control: parse error returns partial results with error flag

#### Feature 5.3 — TypeScript/JavaScript extractor
- Description: tree-sitter-typescript — extract functions, classes, interfaces
- Inputs: .ts or .js file path
- Process: Parse with tree-sitter-typescript, extract symbols
- Outputs: `sylva.extract_symbols(path)` works for .ts/.js files
- Testing:
  - General: extracts function, class, interface from real .ts file
  - Edge: .jsx/.tsx files, empty file, ES modules vs CommonJS
  - Negative: non-TS file raises clear error
  - Error control: parse error returns partial results with error flag

#### Feature 5.4 — Coverage support for additional languages
- Description: LCOV parsing already works (Feature 3.1); wire up cargo-tarpaulin and Istanbul
- Inputs: coverage report path, language (rust|typescript)
- Process: All emit LCOV — same parser, just document the commands to generate reports
- Outputs: `sylva.parse_coverage(path, "lcov")` works for Rust and TS coverage reports
- Testing:
  - General: parses tarpaulin LCOV and Istanbul LCOV correctly
  - Edge: mixed-language LCOV report
  - Negative: unknown language hint raises ValueError
  - Error control: missing coverage file raises FileNotFoundError

---

### Epic 6 — Semantic Layer

#### Feature 6.1 — Vector embeddings on symbols
- Description: Embed docstrings + signatures; store via sqlite-vec
- Inputs: db path, embedding model (local or API)
- Process: For each symbol with docstring, generate embedding vector, store in sqlite-vec table
- Outputs: `sylva.embed_symbols(db_path, model)` — returns count of embedded symbols
- Testing:
  - General: embedded symbols retrievable by vector similarity
  - Edge: symbol with no docstring uses signature only, empty signature skipped
  - Negative: invalid model name raises ValueError
  - Error control: embedding API failure retries once, then marks symbol as unembedded

#### Feature 6.2 — Hybrid search: BM25 + vector
- Description: FTS5 for exact matches, vector for semantic; fuse with RRF
- Inputs: db path, query string, top-k
- Process: Run FTS5 query and vector similarity query in parallel; fuse results with Reciprocal Rank Fusion
- Outputs: `sylva.search(db_path, query, k) -> list[dict]` — ranked symbols
- Testing:
  - General: exact name match ranks high, semantic match surfaces related symbols
  - Edge: k larger than result set returns all results, empty query returns empty
  - Negative: unembedded DB falls back to FTS5 only with warning
  - Error control: vector search failure falls back to BM25 only, not crash

#### Feature 6.3 — Graph export for external tools
- Description: Export graph to JSON and GraphML for Obsidian, Gephi, Graphviz
- Inputs: db path, format (json|graphml), output path
- Process: Query full graph, serialise in requested format
- Outputs: `sylva.export_graph(db_path, format, output_path)` — writes file, returns node/edge count
- Testing:
  - General: JSON export is valid JSON with correct node/edge structure; GraphML is valid XML
  - Edge: empty graph exports as valid empty structure, large graph exports without timeout
  - Negative: unknown format raises ValueError, unwritable output path raises error
  - Error control: partial export failure cleans up partial file
