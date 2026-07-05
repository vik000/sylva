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
- Process: The decision and the checkpoint are split (Option A) so the "indexed"
  state is never committed before the index it gates succeeds —
  - `file_needs_reindex`: read-only. Hash file contents, compare to stored hash,
    return the skip/reindex decision. Makes no DB changes.
  - `mark_indexed`: persist the current hash, marking the file done. Call only
    AFTER a successful (re)index, so a crash mid-index leaves the file flagged
    for reindex rather than silently skipped.
- Outputs:
  - `sylva.file_needs_reindex(db_path, file_path) -> bool`
  - `sylva.mark_indexed(db_path, file_path) -> None`
- Intended loop:
  ```
  if file_needs_reindex(db, f):
      write_symbols(db, f, extract_symbols(f))
      mark_indexed(db, f)          # checkpoint only on success
  ```
- Testing:
  - General: unchanged file returns False, modified file returns True, new file returns True
  - Edge: empty file has stable hash, very large file hashes correctly
  - Read-only: file_needs_reindex writes nothing; a crash before mark_indexed
    leaves the file still flagged for reindex
  - Negative: unreadable file raises error (both functions)
  - Error control: mark_indexed write failure is logged and retried once, never fatal

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
- Design note — path reconciliation (see issue #33): the coverage dict from
  Feature 3.1 is keyed by the paths *inside the report* (e.g. `src/foo.py`,
  relative to where coverage ran), while the graph's `files.path` holds whatever
  the walker produced (often absolute). These two path spaces must be reconciled
  (normalise/relativise, or match by suffix) or coverage will silently attach to
  no symbols. This must be handled here, with tests for the relative-vs-absolute
  and differing-root cases. Also depends on symbol spans — see Feature 7.1
  (`line_end`, issue #28) for accurate boundary matching.
- Testing:
  - General: symbol spanning covered lines gets correct %, uncovered symbol gets 0%
  - Edge: symbol with no lines in coverage report gets null (not 0%), partial coverage
  - Path reconciliation: relative report path matches an absolute graph path;
    differing roots handled; genuinely unrelated path attaches to nothing
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
- Description: Local web UI — force-directed graph of the codebase. **This is
  Sylva's first Python-native feature** (module `python/sylva/viz/`, no Rust /
  PyO3): the export reads `sylva.db` via stdlib `sqlite3` and the server is the
  stdlib `http.server`. The front-end is a single self-contained `index.html`
  using **vanilla Canvas + a from-scratch force simulation** (settle-then-freeze
  for scale) — not D3, and not served by the Rust binary (decisions from design
  review; supersedes the original "served by the binary / D3" wording).
- Inputs: db path, port (default 7700)
- Process:
  - `sylva.viz.export_graph_json(db, out_dir="visualisation")` writes
    `visualisation/graph.json` (nodes = symbols with kind/file/coverage/degree,
    links = call/import edges) — the artifact, refreshable manually now, with a
    watcher hook as a follow-on. `build_graph(db)` is the shared, testable core.
  - `sylva.viz.serve(db, port, open_browser)` serves `/` (the HTML) and a live
    `/graph.json`, and opens the browser.
- Outputs: `sylva serve-ui --db .codemcp/sylva.db [--port N] [--no-open]` writes
  the graph, starts the local server, and opens the browser at localhost:7700
- Testing:
  - General: server starts, /graph.json returns valid JSON, HTML page loads
  - Edge: empty graph renders without error, large graph (1000+ nodes) loads within 3s
  - Negative: port in use raises clear error with suggestion to use --port
  - Error control: DB not found returns 500 with JSON error body
- Note: `graph.json` is kept viz-shaped and separate from Feature 6.3's general
  JSON/GraphML export for external tools.

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

#### Feature 4.6 — Module clustering & collapse/expand (issue #47)
- Description: Tame the force-directed "hairball" by grouping symbols into their
  modules (file/package) and letting the user collapse a module to a single
  block and expand it back to its functions. Start collapsed for a legible
  high-level view, drill down on demand. Highest-leverage readability win.
- Inputs: db path (or the existing viz graph.json — nodes carry `file`)
- Process:
  - Group nodes by file/package; draw a visual boundary (hull/block) around each.
  - Aggregate edges between modules: module A → B if any symbol in A calls/imports
    one in B (carry a weight = count).
  - Collapsed module = one node; expanding reveals its symbols + intra-module edges.
  - Data-layer first (emit module id + aggregated module edges in the export, so
    it's testable without rendering — same pattern as 4.5's `coverage_state`);
    then the collapse/expand interaction in `index.html`.
- Outputs: a collapsible module view in the visualisation; module-level nodes +
  aggregated edges available in the graph data
- Testing:
  - General: symbols grouped into the correct module; module→module edge exists
    when a cross-module call/import exists, with the right weight
  - Edge: single-file repo → one module; module with no cross-module edges
  - Collapse/expand: collapsing hides intra-module symbols, expanding restores them
  - Error control: empty graph renders without crash

#### Feature 4.10 — Per-entry-point call flowchart (layered layout) (issue #51)
- Description: Show *flow*, not just structure. Pick an entry point and render its
  reachable call subgraph as a top-down **layered / hierarchical (Sugiyama/DAG)**
  flowchart — far more readable for "how does execution move through this code"
  than the global force graph.
- Inputs: db path, an entry-point symbol name
- Process:
  - Compute the reachable outbound call subgraph from the entry point. Reuse
    Feature 4.1 `trace_calls(direction="outbound")` — its per-node `depth` is the
    layer assignment (BFS depth). Cycles terminate via the visited set.
  - Emit a flow subgraph: nodes with a `layer` (depth) + the call edges among the
    reached set. This is the **data-layer** (testable without rendering, like
    4.5 `coverage_state` / 4.6 modules).
  - UI: a "flow view" mode with an entry-point picker; lay nodes out by layer
    top-to-bottom, edges pointing downward.
- Outputs: a layered flowchart view for a chosen entry point; a flow subgraph
  (nodes+layers+edges) in the graph data / an endpoint
- Testing:
  - General: chain a→b→c assigns layers 0,1,2; branch (a→b, a→c) puts b,c at layer 1
  - Edge: diamond (a→b→d, a→c→d) — d at its deepest layer; recursion/mutual
    recursion terminate (no infinite layering)
  - Negative: unknown entry point → empty flow
  - Error control: entry point with no outbound calls → just itself (layer 0)
- Note: unblocks Feature 4.11 (coverage execution paths) and 4.12 (data flow),
  which reuse the layered layout.

#### Feature 4.11 — Execution-path view from coverage contexts (issue #52)
- Description: Visualise the **real path a test/run exercised** — not just the
  static possibilities — reusing data we already have (Epic 3). Given a test,
  show the part of the call graph it actually touched, as a layered flowchart.
- Inputs: db path, a test name
- Process:
  - The symbols a test exercised = targets of its outbound `test_covers` edges
    (Feature 3.3). Take the induced subgraph of those symbols + the `calls`
    edges among them.
  - Layer it like Feature 4.10: roots = covered symbols with no inbound call
    from within the covered set; BFS depth = layer. Reuse the 4.10 layered
    rendering.
  - `exec_path(db, test) -> {test, nodes: [{..., layer}], edges}`; served via an
    endpoint (e.g. `/exec?test=`); UI: pick a test → show its exercised flow.
- Outputs: a layered flowchart of the symbols a test actually exercised
- Testing:
  - General: a test covering a→b→c (via test_covers) with those call edges →
    layers 0,1,2
  - Edge: test covering isolated symbols (no calls among them) → all layer 0
  - Negative: unknown test → empty; test with no coverage → empty
  - Error control: missing db raises; empty induced subgraph handled
- Note: pragmatic bridge to true data flow (4.12) using existing coverage data,
  no runtime tracer needed. Depends on Feature 3.3 (test_covers) + 4.10 (layout).

#### Feature 4.8 — Sidebar navigator (entry points, hubs, logic paths) (issue #49)
- Description: Turn the sidebar into a table-of-contents / navigator for the
  codebase, so the graph is navigable instead of requiring free-roam panning.
- Inputs: db path
- Process:
  - Serve the architecture summary (Feature 4.3 `get_architecture` → modules,
    hubs, entry points) via an endpoint (e.g. `/architecture`).
  - UI: render clickable lists of **entry points** and **hubs** in the sidebar;
    clicking an item **focuses** it (find by name → select + centre the view).
  - Logic paths: entry points double as call-chain starts — clicking an entry
    point can flow it (reuse Feature 4.10 `enterFlow`), so the sidebar is the
    natural launch point for the flowchart view.
- Outputs: sidebar shows entry points + hubs; clicking navigates/focuses (and,
  for entry points, can open the layered flow)
- Testing:
  - General: `/architecture` returns hubs + entry_points for a known graph
  - Edge: empty graph → empty lists (no crash)
  - Negative: missing db → error response
  - UI: the sidebar contains the navigator containers + is populated from
    `/architecture`
- Note: pure reuse of Feature 4.3 (get_architecture) + 4.10 (flow); no Rust
  change. Ties the clustering / flowchart features into a "pick → explore" UX.

#### Feature 4.9 — Focus / neighborhood mode with depth control (issue #50)
- Description: The cheapest cure for the hairball — stop showing everything at
  once. Click a node → show only its N-hop neighborhood, hide/dim the rest, with
  a depth slider controlling N. Extends the existing filter/highlight.
- Inputs: db path, a centre symbol name, depth N
- Process:
  - Data-layer first (same pattern as 4.6/4.10/4.11): `neighborhood(db, symbol,
    depth) -> {center, depth, nodes:[{..., dist}], edges}` — the induced subgraph
    of every symbol within N undirected hops of the centre over `calls`/`imports`
    edges, each node carrying its hop distance (`dist`) from the centre. BFS with
    a visited set (cycle-safe); depth 0 is just the centre. Served via an
    endpoint (e.g. `/neighborhood?symbol=&depth=`).
  - UI: a "focus mode" — clicking a node (or a navigator entry) restricts the
    canvas to its neighbourhood; a depth slider re-queries N; a "back to graph"
    exit. Optional niceties: pin nodes to keep them visible, breadcrumb of the
    focus history.
- Outputs: a focused N-hop subgraph view around a chosen symbol; a
  neighbourhood subgraph (nodes+dist+edges) in the graph data / an endpoint
- Testing:
  - General: chain a-b-c-d, focus b depth 1 → {a,b,c} (dist 0/1); depth 2 → +d
  - Edge: depth 0 → just the centre; isolated node → just itself; cycle
    terminates (no infinite BFS)
  - Negative: unknown symbol → empty; missing db → error response
  - UI: the asset contains the focus-mode + depth-slider controls
- Note: undirected neighbourhood (both callers and callees), distinct from the
  directional 4.10 flow / 4.2 blast radius. Pairs with 4.8 (click a hub → focus
  its neighbourhood).

#### Feature 4.7 — File/package-level architecture view (issue #48)
- Description: A dedicated top-level architecture map where each **file or
  package (directory)** is a node and edges are aggregated between them — a few
  dozen nodes instead of thousands. Feature 4.6 already delivered *file*-level
  clustering as an in-network toggle; 4.7 completes it with a general,
  level-selectable aggregator (adding the **package/directory** roll-up for an
  even higher-level view) exposed as data + an endpoint + a level selector.
- Inputs: db path, grouping `level` ("file" | "package")
- Process:
  - Pure aggregation over existing symbols + edges (no parsing). `module_map(db,
    level) -> {level, nodes:[{id, label, symbols, files}], edges:[{source,
    target, weight}]}`. `level="file"` groups by file path (one node per file,
    `files`=1); `level="package"` groups by the file's directory (one node per
    package, `files`=count of files in it). `symbols`=symbol count per group.
    Edge A→B when any symbol in A calls/imports one in B; weight = count of such
    cross-group symbol edges (intra-group edges collapse away).
  - Served via an endpoint (e.g. `/architecture-map?level=package`).
  - UI: a grouping selector in the module view so the same canvas can show the
    file map or the coarser package map.
- Outputs: a file-or-package architecture map (nodes+edges+weights) in the graph
  data / an endpoint, and a level selector in the UI
- Testing:
  - General: two files in one dir + one in another → 2 package nodes; a
    cross-directory call yields a package edge with the right weight
  - File level: one node per file (matches 4.6's `modules`)
  - Edge: single-package repo → one package node, no cross-package edges;
    intra-package edges don't create self-loops
  - Negative: unknown level → ValueError; missing db → error response
  - UI: the asset contains the level selector
- Note: pure aggregation, complements 4.6 (clustering in the network) as the
  coarser, dedicated architecture map. No Rust change.

#### Feature 4.12 — Data-flow (data-path) view — tier 1: static parameter flow (issue #53)
- Description: Track how *data* moves, not just which functions call which — the
  most valuable and most expensive idea. Sequenced last (4.10/4.11 already give
  most of the "trace a path" value far more cheaply). **Two tiers, be honest
  about cost:**
  - **Tier 1 (this feature) — static, approximate def-use:** parameter
    pass-through. When function `f` passes one of its parameters `p` as an
    argument into a call to `g`, record a data-flow `p: f → g`. Answers "what
    touches parameter X". This is **real new extraction** — we currently have
    *call* edges, not argument dataflow.
  - **Tier 2 (deferred, stretch, separate issue) — dynamic data flow:**
    instrument + execute to trace real values; run-the-target baggage (env,
    security, non-determinism). Not in scope here.
- Inputs: file path (extraction); db path + a symbol name (query/view)
- Process:
  - Extraction (Rust, tree-sitter): extend the Python extractor to capture, per
    function, its parameter names and the *argument identifiers* at each internal
    call site. Emit a data-flow relation `(enclosing_fn, callee_name, param)`
    whenever a call argument is a bare identifier matching an enclosing
    parameter. Resolve `callee_name` to a symbol id (reuse Feature 4.0
    name-resolution). Persist in a dedicated `dataflow(src_id, dst_id, param)`
    table (schema migration; keeps `edges` semantics untouched).
  - View (Python viz, data-layer-first like 4.10/4.11): `data_flow(db, symbol)
    -> {symbol, nodes:[{..., layer}], edges:[{source, target, param}]}` — the
    reachable parameter-flow subgraph from `symbol`, layered by hop (reuse
    4.10's `_bfs_layers` + `renderLayered`). Served via `/dataflow?symbol=`; UI
    adds a data-flow mode.
- Outputs: a `dataflow` table populated at index time; a `data_flow(db, symbol)`
  layered view + endpoint + UI mode showing where a symbol's parameter data flows
- Testing:
  - General: `def f(x): g(x)` → dataflow edge f→g (param x); chain
    `f(x)->g(x)->h(x)` layers 0,1,2 via the reused layout
  - Approximation limits (documented + tested as such): name-based only — no
    aliasing, reassignment, or attribute/subscript tracking; a renamed local
    (`y = x; g(y)`) is *not* followed in tier 1
  - Edge: a parameter passed nowhere yields no edge; recursion/cycles terminate
  - Negative: unknown symbol → empty; parse error returns partial, not a crash
  - Error control: re-index is idempotent (no duplicate dataflow rows)
- Note: the honest "most expensive" feature — new AST extraction + a schema
  migration, distinct from the pure-aggregation viz features. Depends on Feature
  4.10 (layered rendering) and reuses Feature 4.0 (name resolution). File the
  dynamic tier-2 as its own issue when this closes.

---

### Epic 5 — Additional Language Support

#### Feature 5.0 — Black-box foreign modules from the export surface (issue #43)
- Description: For non-Python languages (Rust/C/C++ accelerators), **do not fully
  parse** — represent each foreign module as an opaque **black-box node**
  exposing only its **export surface** (the entrypoints callable from indexed
  code). Cross-language calls from Python resolve to those entrypoints, so the
  boundary is visible instead of a silently-dropped edge.
- **Scope reality (important):** this covers **foreign-*language*** boundaries
  (e.g. a repo's own Rust/PyO3 or C extension) — NOT unresolved external *Python*
  packages (click, werkzeug). Sylva itself (Rust + PyO3) is the natural dogfood
  target: Python→Rust calls (`sylva.build_edges(...)`) currently show as
  unresolved; 5.0 makes the Rust module + its `#[pyfunction]` exports a black box.
- Inputs: repo root / foreign source files, db path
- Process:
  - Walk foreign files (extend the walker beyond `.py`). A lightweight
    **export-surface extractor** recognises only export declarations (targeted
    parse / tree-sitter query), not internals:
    - Rust + PyO3: `#[pyfunction]`, `#[pymodule]`, `m.add_function(...)`
    - (later) C `PyMethodDef`, FFI `#[no_mangle]` / `extern "C"`, `__all__`
  - Emit one **`foreign_module`** node per module + its **`foreign_export`**
    entrypoint symbols (opaque).
  - Cross-boundary resolution: a Python call/import to an exported name resolves
    to the black-box entrypoint (`calls`/`imports` edge across the boundary).
  - Internals are NOT parsed. Fits under Feature 5.1's extractor trait as a
    distinct "export-surface" kind (5.1 not yet built — see dependency note).
- Outputs: foreign modules represented (not dropped); Python→foreign edges exist
- Testing:
  - A Python file calling a PyO3-exported Rust fn → edge to a black-box entrypoint
  - A foreign file with no recognisable exports → module node with no entrypoints
    (or skipped) — never a crash
  - Mixed repo: non-Python files represented as black boxes, not silently dropped
- Note: depends conceptually on Feature 5.1 (trait) — build minimal/targeted here
  rather than the full refactor. Distinct from external-Python-dep black-boxing.
  Feature 5.0.1 (#44) styles these nodes in the viz.

#### Feature 5.0.1 — Represent black-box foreign modules in the visualisation (issue #44)
- Description: Show Feature 5.0's black-box foreign modules distinctly in the
  interactive graph so cross-language boundaries are visible and obviously opaque.
- Process (data-layer first, like 4.5 `coverage_state`): `build_graph` carries a
  per-node black-box flag/kind; `index.html` maps it to a distinct style
  (shape/colour/badge) + a legend entry + a "show/hide foreign modules" filter
  toggle (reuse the 4.4/4.5 toggle pattern). Render the cross-language edges.
- Outputs: foreign module/entrypoint nodes visibly distinct + a filter control
- Testing:
  - Foreign nodes carry the black-box flag/kind in graph.json
  - The UI asset contains the distinct rendering + the foreign-module filter
- Note: depends on Feature 5.0 (defines the data) + 4.4/4.5 (viz/toggle pattern).

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

#### Feature 5.5 — Multi-language pipeline walker (issue #70)
- Description: Sylva can *extract* Python/Rust/TS/JS (5.1–5.3), but the analyze
  pipeline still walks only `.py` (+ foreign `.rs`), so `analyze`/`onboard` index
  nothing on a TS/JS repo. Generalise the indexer to walk **all supported
  extensions** and dispatch through `extract_symbols`, so the extractors are
  actually reached.
- Inputs: repo root, db path
- Process:
  - New gitignore-aware `walk_source_files(root)` (Rust, `ignore` crate) that
    returns files whose extension is in the union of the registry's extractor
    extensions (`extractor::supported_extensions()`).
  - `index_codebase` (onboard.py) walks those and routes by extension:
    **`.rs` → `extract_foreign_exports`** (black-box export surface, per 5.0 —
    the decided policy; gitignore-aware now, replacing the ad-hoc `rglob`);
    **everything else → `extract_symbols`** (full parse, dispatched by the 5.1
    trait). Report per-language symbol counts in the summary.
  - `build_dataflow` also gains the `.py`-only guard `build_edges` already has
    (Python-specific analysis; skip non-Python files cleanly).
- Outputs: TS/JS (and Python) symbols indexed from `analyze`/`onboard`; `.rs`
  still black-boxed; a per-language breakdown in the summary
- Testing:
  - A TS-only repo → its functions/classes/interfaces are in the graph
  - A mixed Python+TS(+Rust) repo indexes each: Python/TS full-parsed, Rust
    black-boxed
  - `.gitignore` respected; the Python-only path is unchanged (regression)
  - Per-language counts reported
- Note: **symbols only** — non-Python files get no `calls`/`imports` edges yet
  (that is Feature 5.6 / #71; `build_edges` still parses Python only). Decided
  policy: `.rs` stays black-boxed (matches 5.0/5.2). Unblocks true polyglot
  analysis together with 5.6.

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

---

### Epic 7 — Tech Debt & Hardening

Cross-cutting cleanups and hardening surfaced during Epics 1–2: coverage gaps,
small correctness races, protocol upgrades, and CI improvements. Non-blocking —
these group deferred notes so they are tracked, not lost. (GitHub milestone #7.)

#### Feature 7.1 — Populate symbols.line_end
- Surfaced in: Features 1.4 (extractor) / 1.5 (writer)
- Description: The `symbols` table has `line_start` and `line_end`, but the
  extractor only reports a single start `line`, so `line_end` is always NULL.
- Process: `extract_symbols` also reports each definition's end line
  (`node.end_position()`); the dict carries `line_end`; `write_symbols` persists it.
- Outputs: symbols have a correct `line_end`; span-based features (3.2 coverage,
  visualisation) can rely on it
- Testing:
  - General: multi-line function reports correct line_start/line_end
  - Edge: single-line def has start == end; nested defs get their own spans

#### Feature 7.2 — Checkpoint the exact indexed hash (close mark_indexed re-hash race)
- Surfaced in: Features 2.1 (file hash tracking) / 2.2 (watcher)
- Description: `mark_indexed` re-reads and re-hashes the file. If the file
  changes between the index write and the checkpoint, it stores the hash of the
  new content while the graph holds the old symbols — the next run then skips a
  file that was never indexed at its current state.
- Process: thread the exact hash computed at index time through to the
  checkpoint (e.g. `file_needs_reindex` returns (decision, hash), or
  `mark_indexed(db, path, hash)`), so we store the hash of what was actually indexed.
- Outputs: modifying the file mid-index leaves it flagged for reindex
- Testing:
  - General: normal path still idempotent
  - Edge: change between index and checkpoint keeps the file flagged
  - Note: low-severity (the 2.2 watcher re-fires on the next change), but real

#### Feature 7.3 — Test watcher loop resilience to mid-loop errors
- Surfaced in: Feature 2.2 (file watcher)
- Description: The "errors logged, watcher recovers" guarantee is currently true
  by construction (`process_path` logs and continues) but not proven by a test
  that forces a failure inside the running loop.
- Process: induce a reindex/delete failure for one event, then verify a
  subsequent valid event is still processed and the watcher thread is alive.
- Outputs: end-to-end proof the loop survives an in-flight error
- Testing:
  - General: after an induced error on one path, a later create/modify on
    another path still indexes
  - Error control: watcher thread remains alive after the error

#### Feature 7.4 — Real MCP protocol handshake (initialize / tools/list / tools/call)
- Surfaced in: Feature 1.6 (MCP server)
- Description: The server speaks plain JSON-RPC (one method per tool), which
  matches the 1.6 spec and is tested, but real MCP clients (Claude Code, Cursor)
  expect MCP framing: an `initialize` handshake, `tools/list` discovery, and
  `tools/call` with a tool-name + arguments envelope.
- Process: layer MCP framing over the existing `handle_request` dispatch —
  implement `initialize`, `tools/list` (advertise the 3 tools + input schemas),
  and route `tools/call` to the current handlers.
- Outputs: the server interoperates with a real MCP client
- Testing:
  - General: `initialize` returns capabilities; `tools/list` lists tools with
    schemas; `tools/call` dispatches correctly
  - Negative: unknown tool in `tools/call` returns a proper error

#### Feature 7.5 — CI hardening: Python version matrix + caching
- Surfaced in: scaffold / ongoing
- Description: CI builds once on Python 3.12 with no dependency caching and no
  OS/Python matrix. The wheel is abi3-py39 and the watcher (2.2) is
  OS-dependent, so cross-platform/version coverage matters.
- Process: add a Python version matrix (3.9–3.13) and optionally an OS matrix
  (ubuntu + macos); cache cargo (`~/.cargo`, `target/`) and pip; decide whether
  to commit `Cargo.lock` (currently gitignored) for reproducible CI.
- Outputs: CI green across the matrix; faster builds via caching
- Testing:
  - General: CI passes across the matrix
  - Error control: build time reduced by caching

#### Feature 7.6 — apply_coverage correlation robustness (dedup + stale reset)
- Surfaced in: Feature 3.2 (correlate coverage to symbols) — tracks issue #34
- Description: two low-severity robustness gaps in `apply_coverage`:
  - (A) if two report entries suffix-match the same graph file, `reconcile`
    does a nondeterministic last-wins overwrite (HashMap iteration order);
  - (B) coverage is never reset, so a file absent from a later report keeps its
    stale `coverage_pct` from a previous run.
- Process:
  - (A) detect the collision and either merge the line maps (OR) or skip as
    ambiguous, consistent with the existing file→multiple-graph handling;
  - (B) decide the contract — a reset pass (NULL all `coverage_pct` first) or
    document that callers must re-index / that apply is additive.
- Outputs: deterministic, non-stale coverage correlation
- Testing:
  - (A) two report paths mapping to one graph file → deterministic result
  - (B) re-apply a report missing a previously-covered file → stale handled
- Note: per-symbol error isolation in `apply_coverage` is structural/untested —
  same class as Feature 7.3 (issue #30).

#### Feature 7.7 — Type-aware edge resolution (reduce name-match imprecision)
- Surfaced in: Feature 4.0 (edge population) — tracks issue #36
- Description: `build_edges` resolves references by name only, which never
  invents a false edge but misses real ones under name collisions:
  - method calls resolve by method name, ignoring the receiver's type;
  - no scope/shadowing model;
  - colliding same-named definitions are skipped, not disambiguated.
- Process: prefer same-file/same-scope matches; use import bindings to
  disambiguate (`from bar import b` → `b()` resolves to `bar.b`); longer term,
  light type inference for `self.method()` / `obj.method()`.
- Outputs: more complete and precise call/import edges
- Testing:
  - same-named functions across files: an importing file's call resolves to the
    imported one, not skipped
  - `self.method()` resolves to the enclosing class's method under name collision
- Note: improves accuracy of Feature 4.1 (call chains) and 4.2 (blast radius).

#### Feature 7.8 — Import edges are name-conflated / lost for aliased imports
- Surfaced in: Feature 4.2 (blast radius) — tracks issue #37
- Description: Feature 4.0 builds `imports` edges as `binding -> definition`
  resolved by the binding's name. Aliased imports (`import x as y`) lose the
  original name so no edge forms; non-aliased imports share the target's name so
  the binding is conflated with the target. Net: import edges don't enrich
  `blast_radius` / `get_dependencies` today.
- Process: retain the original imported name + source module in the extractor;
  resolve import edges by original name + module (reuse 3.2 suffix matching) so
  an importing context links to the real definition, distinct from it.
- Outputs: import relationships surface in blast radius and get_dependencies
- Testing:
  - `from lib import util as u` yields an imports edge to `util` (survives alias)
  - an importing file appears as a distinct dependent in `blast_radius('util')`
    with `via: 'imports'`, without seeding the edge by hand
- Note: distinct from Feature 7.7 (#36), which is about call resolution.

#### Feature 7.10 — Type-aware method call resolution (issue #45)
- Surfaced in: Features 7.7 (#36) / 7.8 (#37), both closed — this is their
  deferred "type inference" half. On real repos it is the dominant source of
  dropped edges (Flask: ~900 references stay ambiguous/unresolved after #36/#37,
  e.g. `self.get()`, `obj.add_url_rule()`), undercounting `get_callers`,
  `blast_radius`, `trace_calls`, and thinning every Epic 9 view.
- Description: Call resolution is name-based, so `obj.method()` resolves to *any*
  unique `method` and same-named methods across classes are skipped as ambiguous.
  Add light, conservative receiver type inference in `build_edges` — never invent
  a false edge; ambiguous-skip stays the fallback.
- Inputs: db path (edge build), reusing the existing extractor + `build_edges`
- Process (build on the existing resolution, don't redo #36/#37):
  - **Class membership:** map each method (a `function` whose span is nested in a
    `class` span) to its enclosing class, so a class's own methods are a
    resolution scope.
  - **`self.method()`** (and `cls.method()`) → the enclosing class's `method`,
    even when other classes define `method`.
  - **Simple local binding:** `x = Foo(); x.method()` → `Foo.method` when `Foo`
    resolves to a known class (track trivial `name = ClassName(...)` assignments
    within a function body).
  - Narrow candidates by class membership + imports; if still ambiguous, skip
    (no false edge). Keep the bounded skip summary (#39); measure the drop in
    ambiguous skips on a real repo.
- Outputs: denser, still-precise `calls` edges; `self.method()` / `x.method()`
  resolve; `get_callers` / `blast_radius` / `trace_calls` recover real edges
- Testing:
  - `self.method()` resolves to the enclosing class's method even when other
    classes define `method` (was ambiguous → now resolved)
  - `x = Foo(); x.bar()` resolves to `Foo.bar`
  - Genuinely unknowable receiver stays skipped (no false edge)
  - Re-index idempotent; no regression on the existing 4.0/#36/#37 cases
  - Evidence: ambiguous-skip count drops materially on a real repo (e.g. Flask)
- Note: continuation of #36/#37; improves Features 4.1/4.2/1.6 and every Epic 9
  analysis. Conservative by design — precision over recall; never a false edge.

---

### Epic 8 — Reporting & Agent Integration

Make the analysed graph reachable and usable by AI agents (Claude Code, Cursor,
others) with minimal setup, and surface it for reporting. (GitHub milestone
"Epic 8 — Reporting & Agent Integration".)

#### Feature 8.2 — Per-project MCP scaffold (agent access) (issue #41)
- Description: One-step, project-local MCP setup so an agent can query a repo's
  Sylva knowledge graph. **Decision: interpretation (A)** — scaffold config that
  wires Sylva's *existing* query tools to this codebase's db (not (B), executing
  the repo's own functions). Builds on the MCP server (1.6 / 3.5) and the real
  MCP handshake (Feature 7.4 / #31).
- Inputs: db path, output folder (default `.codemcp`)
- Process:
  - `sylva init-mcp --db <path> [--out .codemcp]` writes the folder + an
    `mcp.json` config (server launch command + **absolute** db path) ready to
    drop into a Claude Code / MCP client config. Idempotent (safe to re-run).
  - Expose the graph-query tools not yet on the MCP surface — `trace_calls`,
    `blast_radius`, `get_architecture` — as `tools/call` tools + in `tools/list`,
    so the scaffold advertises the full toolset. (These already exist as PyO3
    functions; this wires them into `handle_request`.)
  - Optionally emit a tool manifest (names + descriptions + input schemas) that
    Feature 8.3 can consume for the viz.
- Outputs: a `.codemcp/mcp.json` that launches the Sylva MCP server against this
  repo's db; the three additional tools live on the MCP surface
- Testing:
  - Scaffold: `init-mcp` writes valid JSON referencing the correct absolute db
    path + server command; re-running is idempotent; custom `--out` respected
  - New tools: `trace_calls` / `blast_radius` / `get_architecture` respond via
    both plain JSON-RPC and `tools/call`; appear in `tools/list` with schemas
  - Negative: unknown args / missing required param return a proper JSON-RPC error
- Note: interpretation (B) — turning the repo's own functions into invokable
  tools (sandboxing, arg schemas, code execution) — is explicitly out of scope;
  file separately if ever wanted.

#### Feature 8.4 — get_source MCP tool (fetch a symbol's code) (issue #46)
- Description: An MCP tool that returns the **actual source code** of a symbol,
  so an agent can fetch code precisely via the graph instead of opening whole
  files. The graph stores structure + metadata (name, kind, file,
  `line_start`/`line_end`, docstring) but not source bodies; `get_source` reads
  the file span on demand, keeping the DB lean and the source never stale.
- Inputs: a symbol name (or an explicit `file` + `start` + `end` range)
- Process:
  - `get_source(name)` → for each matching symbol, read its file for the
    `line_start`..`line_end` span (uses Feature 7.1 spans) and return `{name,
    kind, file, line_start, line_end, code}`.
  - Optional range form `get_source(file, start, end)` returns an arbitrary
    slice as a single result.
  - Slots into the existing dispatch (`handle_request`) as another tool, on both
    the plain JSON-RPC surface and `tools/call`, and in `tools/list`.
- Outputs: a `get_source` MCP tool returning exact current source for a symbol
  (or range)
- Testing:
  - General: returns the exact source lines of a known multi-line symbol
  - Range: `get_source(file, start, end)` returns that slice
  - Reads current file contents at query time (edit the file → new source, no
    stale stored copy)
  - Negative: unknown symbol → empty list (not an error); missing/unreadable
    file → clear MCP error, not a crash; symbol with a NULL span skipped/handled
- Note: closes the "find where code lives → read it" loop for agents; depends on
  symbol spans (Feature 7.1, done). Complements the query tools (1.6 / 3.5) and
  the MCP handshake (7.4 / #31).

#### Feature 8.5 — Expose selected repo functions as an executable MCP server (issue #69)
- Description: The deferred interpretation (B) from #41. Turn the analysed repo
  into an MCP server whose tools are the repo's **own functions** — callable by
  an agent — with **explicit, opt-in control over which functions are exposed**.
  Distinct from 8.2 (query access to the graph): 8.5 lets an agent *invoke* the
  repo's code, so it is opt-in and safety-scoped.
- Inputs: repo root, db path, output path
- **Settled design (decided): allowlist-only selection · dependency-free
  generated server · standalone reviewable file · per-function tools + module
  shorthand · Python execution backend first.** The **allowlist** is chosen over
  a decorator because it is **language-neutral** (one config for Python/Rust/TS —
  a decorator would need a per-language marker + scanner), **deterministic**
  (a static declarative contract → identical server every time), and
  **auditable** (the entire exposed surface in one file). No decorator.
- Process:
  - **Selection (opt-in; nothing exposed by default):** a language-neutral
    allowlist config, e.g. `.codemcp/expose.toml` — naming functions to expose
    (and a **module shorthand** = its public functions). Sylva resolves each name
    against the graph (symbol + file + language). The allowlist format names any
    language's targets so it stays valid as execution backends are added.
  - **Generation:** `sylva expose --root .` writes a **reviewable, standalone**
    MCP server file. **Python execution backend (first):** for each allowlisted
    Python function, `from <import-path> import <fn>` (import path derived from
    the file path relative to the root), wrap it as a `tools/call` tool, and
    derive the argument schema from the **live signature** (`inspect.signature`
    at runtime — no stored signatures). Non-Python targets are listed but skipped
    with a note until their backend lands. Reuses Sylva's dependency-free
    JSON-RPC/MCP framing (initialize / tools/list / tools/call) — the generated
    server needs **no third-party runtime dependency**.
  - **Safety:** Sylva **generates**; the **user reviews and runs** the server in
    their own environment — Sylva executes nothing. The generated file carries a
    header documenting the trust boundary + the exact exposed surface.
- Outputs: a standalone server file (e.g. `.codemcp/functions_server.py`)
  exposing exactly the allowlisted functions; a `sylva expose` CLI subcommand
- Testing:
  - An allowlisted function is exposed as a tool; a non-listed function is not
  - Module shorthand exposes a module's public functions
  - The generated server is valid Python, imports the target functions, and
    derives a tool schema from each signature (in-process: import the generated
    module, check its tool registry / call a tool)
  - Empty / missing allowlist → a safe empty server (no crash), not an error
  - Import-path derivation handles package nesting
  - Non-Python allowlist entry is skipped with a note (backend not yet present)
- Note: executes target code (opt-in) — the one feature where the artifact runs
  the repo. Sylva stays the generator; leverages the function inventory (names,
  files, docstrings) from Epics 1/9. Language-neutral selection is future-proof;
  Python execution first, extensible. Ties to 8.3 (accessible functions in viz).

---

### Epic 9 — Structural Understanding (deterministic)

Deterministic, LLM-free analysis that infers project structure: the global
entrypoint, the main logic spine, centrality, and a whole-system layered "block
diagram" view. Composes existing static data (call graph, edges, spans) with
established algorithms (dominators, SCC + longest-path, Sugiyama layout).
Precedes Epic 10 (skills). (GitHub milestone "Epic 9 …".)

**NOTE: starting this Epic bumps the major to 3.0.0** — the first Feature-9
`/feature-done` bumps from the 2.x line (last shipped 2.10.0) to 3.0.0.

#### Feature 9.1 — Global entrypoint inference (markers + dominance) (issue #55)
- Description: Today's "entry points" (Feature 4.3) are just every function with
  zero inbound calls minus `test_` — dozens of leaves, not **the** entrypoint of
  the system. Infer the real start(s) of the program deterministically, and
  designate a **primary** entrypoint.
- Inputs: db path
- Process (two signals, combined + ranked; no LLM). **Scope decision: cheap
  subset first (option b)** — the reachability/dominance backbone plus the
  highest-signal markers; richer framework markers are deferred to Feature 9.1.1
  (issue #65).
  - **Convention markers (9.1 subset):** `if __name__ == '__main__'` guards and a
    top-level function named `main`. Detected via a lightweight re-parse (as
    `build_edges` does). Deferred to 9.1.1: `console_scripts`, CLI frameworks
    (argparse/click/typer), web routes (`@app.route`, FastAPI).
  - **Topological dominance:** condense SCCs → DAG; take roots (zero in-degree);
    rank each by **reachable-set size** (BFS over outbound `calls`, cycle-safe);
    dominator-tree coverage is the deeper signal, foldable in if cheap, else a
    refinement. The 'main' entrypoint reaches ~all.
- Outputs: `infer_entrypoints(db) -> [{symbol, file, reachable, is_marker,
  marker_kind, rank}]` (ranked) with a designated **primary**; exposed via MCP +
  the viz sidebar.
- Testing:
  - Marker detection: a `__main__` / console-script target is flagged
    (`is_marker`, correct `marker_kind`)
  - Ranking: the root that reaches the most symbols ranks first / is primary
  - No markers: falls back to pure reachability ranking
  - Edge: empty graph → empty; a single-node graph → that node; cycles condensed
    (no infinite traversal)
  - Negative: db not found raises a clear error
- Note: 'longest path' ≠ 'the entrypoint' — the entry is found by markers +
  dominance here; the deepest chain is Feature 9.2. Depends on the call graph
  (4.0); feeds 9.2 (spine), 9.4 (global flow view), 9.5 (sidebar).

#### Feature 9.1.1 — Framework/entrypoint marker detection (issue #65)
- Description: Deferred follow-up to Feature 9.1. Real repos are mostly framework
  apps that have **no `main()` / `__main__` guard** — a Flask/FastAPI service
  starts at route handlers, a click/typer CLI at command functions. Without these
  markers, 9.1 falls back to pure reachability and can crown the wrong primary,
  which matters because 9.4/9.5 root the whole diagram at it. This adds the
  framework-specific markers so entrypoint inference is robust on real codebases.
- Inputs: db path (same `infer_entrypoints` surface)
- Process — extend 9.1's marker scan with:
  - **`console_scripts`** in `pyproject.toml` / `setup.cfg` / `setup.py` — parse
    the config at the project root, map each `name = module:function` target
    back to a graph symbol (by module-path + function name; reuse suffix
    matching à la 3.2).
  - **CLI frameworks:** click/typer `@command` / `@app.command` / `@group`
    decorators, and an argparse `main`.
  - **Web routes:** Flask `@app.route` / `@blueprint.route`, FastAPI/Starlette
    route decorators (`@app.get/post/...`), Django URLconf view targets.
  - Each contributes a distinct `marker_kind` (e.g. `console_script`, `cli`,
    `web_route`) and boosts the symbol's entrypoint rank, alongside the existing
    `main` / `main_guard`.
- Outputs: `infer_entrypoints` flags framework entrypoints with the right
  `marker_kind`; framework apps get a correct primary
- Testing:
  - A `@app.route`-decorated function → flagged `web_route`
  - A `console_scripts` target resolves to its symbol → flagged `console_script`
  - A click/typer command → flagged `cli`
  - Non-framework repo unchanged (9.1 behaviour preserved); a decorator we don't
    recognise is ignored, not misflagged
- Note: decorator detection is a re-parse concern (decorators aren't symbols in
  the graph); `console_scripts` needs project-root config parsing. Reuses 9.1's
  `sym_by_pos` / `defs_by_name` resolution. Completes the entrypoint story before
  9.4/9.5 surface it.

#### Feature 9.2 — SCC condensation + longest-path "main spine" (issue #56)
- Description: Identify the system's principal execution path — the 'spine' —
  from an entrypoint, not just isolated chains. Renders as the backbone of the
  global block diagram (9.4).
- Inputs: db path, optional entry-point symbol name (default: the primary from
  Feature 9.1)
- Process (deterministic):
  - **SCC-condense** the call graph (reuse the iterative Tarjan from 9.1; lift it
    into a shared `graph.rs` so 9.2/9.3 don't duplicate it) → a DAG of components
    (recursion/cycles collapse to one node, so longest-path terminates).
  - **Longest path in the DAG** via topological order + DP, starting from the
    component containing the entry. Each node on the path carries its DAG-layer
    (depth) so it renders top-to-bottom like the 4.10 layered view.
  - Expand condensed SCC nodes back to their member symbols for display (note
    which layer members belong to a collapsed cycle).
- Outputs: `main_spine(db, entry=None) -> {entry, nodes:[{symbol, file, line,
  layer, in_cycle}], edges:[{source, target}]}` — the ordered spine; exposed via
  MCP + reused by 9.4. Reuses the 4.10 layered shape.
- Testing:
  - General: chain a→b→c→d from entry a → spine length 4, layers 0..3, order
    a,b,c,d
  - Branch: a→b, a→c→d → longest branch (a,c,d) chosen as the spine
  - Cycle: b↔c condensed into one component; no infinite loop; members flagged
    `in_cycle`
  - Default entry: with no `entry` arg, starts from Feature 9.1's primary
  - Negative: unknown entry → empty; empty graph → empty; db not found raises
- Note: 'longest path' is the deepest chain *from the entry* — it consumes 9.1's
  entrypoint, it doesn't rediscover it. Feeds 9.4 (global flow view). Lifts the
  shared SCC/graph helpers out of `entrypoints.rs` for reuse by 9.3.

#### Feature 9.3 — Centrality: dominators + betweenness (issue #57)
- Description: Rank symbols by *structural importance on paths*, beyond the raw
  degree the viz already uses. **Betweenness** finds the chokepoints many
  entry→leaf paths pass through; **dominators** find the gateways that gate
  access to large subgraphs (removing one cuts off everything below). Deterministic.
- Inputs: db path
- Process (over the `calls` graph; reuse `graph.rs`):
  - **Betweenness centrality** — count of shortest paths through each node
    (Brandes' algorithm on the call graph; on a cyclic graph, run over the
    reachable structure with a visited set so it terminates).
  - **Dominator tree** — from a virtual super-source over the inferred
    entrypoints (9.1), compute immediate dominators (iterative dataflow /
    Cooper-Harvey-Kennedy); `dominates` = size of each node's dominated subtree.
  - Also carry `degree` (existing) for comparison.
- Outputs: `centrality(db) -> [{symbol, file, betweenness, dominates, degree}]`,
  ranked; exposed via MCP; usable by the viz for node sizing/highlight and to
  fold the dominator signal back into 9.1 entrypoint ranking.
- Testing:
  - Betweenness: a single articulation point on all paths scores highest
  - Dominators: a node gating a subtree reports the right `dominates` count; a
    leaf dominates nothing
  - Edge: disconnected / single-node / empty graph handled; cycles terminate
  - Negative: db not found raises
- Note: reuses `graph.rs` (SCC/traversal). Completes Epic 9; the deferred
  "dominator-tree ranking" signal from Feature 9.1 lands here and can enrich
  entrypoint ranking. Precision/determinism over cleverness.

#### Feature 9.4 — Global "system flow" view (whole-project block diagram) (issue #58)
- Description: The first *visible* Epic 9 feature — a whole-project, top-down
  **block diagram** rooted at the inferred primary entrypoint (9.1), not the
  force "hairball". Promotes the layered renderer (`renderLayered`, 4.10/4.11/
  4.12) from a per-node, on-click mode to a first-class **global** view.
- Inputs: db path, optional root entry-point symbol (default: 9.1 primary)
- Process (viz layer — Python `viz/` + `index.html`, no Rust change):
  - Data-layer first (endpoint, testable without rendering): a `/system-flow`
    endpoint serving the reachable outbound call subgraph from the root, layered
    by BFS depth. Reuse Feature 4.10's `flow_layout` (already BFS-layered from an
    entry) with the root defaulting to `infer_entrypoints`' primary; carry the
    **spine** (9.2 `main_spine`) so the backbone can be emphasised.
  - Default root: call `infer_entrypoints`; take `primary` (empty if none).
  - UI: a top-level **"System flow"** mode/toggle that loads the global layered
    diagram on open (no node-click required), reusing `renderLayered`. The
    module/package grouping (4.6/4.7) remains available.
- Outputs: `sylva.viz.system_flow(db, root=None) -> {root, nodes:[{..., layer,
  on_spine}], edges}`; a `/system-flow` endpoint; a global block-diagram UI mode
- Testing:
  - General: endpoint returns a layered subgraph rooted at the inferred primary
    (layers assigned); spine nodes flagged `on_spine`
  - Explicit root honoured; default root = 9.1 primary
  - Edge: empty graph / no-entry → empty (no crash); large graph layered
  - Negative: db not found → error response
  - UI: a global system-flow mode exists and is reachable without selecting a
    node first (asset assertion)
- Note: the deterministic core made visible — reuses 4.10 (layout) + 9.1
  (primary) + 9.2 (spine). Layout quality at project scale is gated by Feature
  9.6 (the Sugiyama-vs-dagre/ELK spike); do that alongside if the layered output
  looks tangled. Sidebar launch is Feature 9.5.

#### Feature 9.5 — Sidebar navigator: launch flows from the navigator (issue #59)
- Description: The Epic 9 structural features (inferred entrypoints 9.1, system
  flow 9.4, spine 9.2) exist but aren't *discoverable* — the sidebar navigator
  (4.8) lists architecture entry points/hubs and only *focuses* them. Make the
  ranked, inferred entrypoints first-class launch points so the structural views
  are reachable in one click instead of buried behind node selection.
- Inputs: db path (reuses `/system-flow`, `infer_entrypoints`; add an endpoint
  for the ranked entrypoints)
- Process (viz layer — Python `viz/` + `index.html`, no Rust change):
  - Serve Feature 9.1's ranked entrypoints (an `/entrypoints` endpoint wrapping
    `sylva.infer_entrypoints`), carrying `primary`, `rank`, `marker_kind`,
    `reachable`.
  - UI: a sidebar **"Entrypoints"** section listing them ranked (primary badged,
    marker_kind shown); clicking one **launches its flow** (reuse 4.10
    `enterFlow`), not just focus. Surface the primary prominently as the default
    system-flow root.
  - Reuse the 4.8 navigator container pattern; the existing `/architecture`
    hubs/entry lists remain (this adds the *inferred, ranked* entrypoints +
    click-to-flow).
- Outputs: sidebar shows ranked inferred entrypoints; clicking flows one;
  primary is the obvious starting point → the structural work is navigable
- Testing:
  - `/entrypoints` returns the ranked list with `primary`/`rank`/`marker_kind`
  - Edge: empty graph → empty list (no crash); missing db → error response
  - UI: the sidebar contains the entrypoints container + a launch handler wired
    to the flow view
- Note: pure reuse of 9.1 (`infer_entrypoints`) + 9.4/4.10 (flow); no Rust
  change. The discoverability capstone for the Epic 9 structural features.

#### Feature 9.6 — Layout spike: from-scratch Sugiyama vs dagre/ELK (issue #60)
- **This is a SPIKE / evaluation, not a code feature.** Deliverable = a written
  recommendation (+ an optional throwaway prototype), not shipped/tested code. It
  does not follow the usual test-and-version-bump shape.
- Description: The layered block diagram (4.10 `renderLayered`, now global via
  9.4) places nodes by BFS depth with a fixed within-layer order and **no
  crossing minimisation**. At whole-project scale this can look tangled. Decide
  whether to invest in a proper Sugiyama pipeline or adopt an established engine.
- Inputs: the current `renderLayered` behaviour + a real-repo `/system-flow`
  payload to judge tangle at scale
- Process — evaluate the options and recommend one:
  - **Keep from-scratch**, add layer-ordering / barycentre crossing-minimisation
    (stays dependency-free; matches the original "vanilla Canvas" decision).
  - **dagre** (JS, MIT) — mature layered layout; emits coordinates into Canvas.
  - **elkjs** (ELK) — heavier, best-in-class hierarchical layout.
  - **Graphviz `dot`** precomputed server-side (Python) — ship coordinates; no JS
    layout dep but adds a Graphviz requirement.
  - Weigh against constraints: dependency-free ethos, self-contained
    `index.html`, settle-then-freeze performance, and readability at 100s–1000s
    of nodes.
- Outputs: a decision + rationale (recorded in this repo — e.g. DESIGN.md and/or
  the issue), gating any 9.4/9.5 layout polish. A throwaway prototype only if
  needed to decide.
- Testing: N/A (spike). "Done" = the recommendation is written down and the issue
  is closed with the decision.
- Note: sequence right after 9.4 (which exposed the scale problem) and before
  investing further in the layered views.

#### Feature 9.8 — Project archetype + architectural layer inference (issue #68)
- Description: Infer the project's **archetype** and, for services, decompose the
  code into **architectural layers** — a *semantic* grouping above files/dirs.
  Complements the entrypoint/spine/flow work with "which tier does this belong
  to". Deterministic, no LLM.
  - **Library** (public API, `__all__`, no service framework) → treat as-is
    (ties to 9.7); no layer split forced.
  - **Application / microservice** → tag each symbol with a layer:
    **interface** (routes/CLI/controllers — largely the 9.1.1 markers),
    **transport** (HTTP clients, message brokers/queues, RPC),
    **data** (ORM models, repositories, DB/cache clients),
    **business** (everything else — the default).
- Inputs: db path
- Process (deterministic heuristics; **cheap subset first**, like 9.1.1):
  - **Archetype detection** by imports/markers: a web framework + routes, or
    queue/RPC clients ⇒ service; `__all__` / rich `__init__` re-exports and no
    service framework ⇒ library.
  - **Layer classification** by the module families a symbol's *file* imports
    (e.g. imports `sqlalchemy`/`psycopg`/`redis` → data; `flask`/`fastapi` +
    route decorator → interface; `httpx`/`requests`/`kafka`/`celery` →
    transport) plus the 9.1.1 decorator markers; genuinely ambiguous → the
    `business` default (never guess).
  - Data-layer first (like 4.6/4.10): emit an inferred `layer` per symbol so it's
    testable without rendering. Viz banding (render the system flow in horizontal
    tiers) is a follow-on, not required for this feature's core.
- Outputs: `infer_layers(db) -> {archetype, layers: {symbol -> layer}}` (or a
  per-symbol `layer` tag); exposed via MCP; consumable by the viz for banding.
- Testing:
  - Service: a route handler → `interface`; a sqlalchemy model/user → `data`; an
    httpx/requests caller → `transport`; a plain helper → `business`
  - Library: `__all__` + no framework → `archetype='library'`, no forced layering
  - Ambiguous/unknown imports → `business` default, not misclassified
  - Empty graph handled; db not found raises
- Note: complements 4.6/4.7 (clustering), 9.1.1 (markers = interface), 9.7
  (library branch). Sequenced after the resolution-fidelity work (7.10/5.0) so
  layers are inferred over a denser, more accurate graph. Cheap subset first;
  richer per-framework rules can follow.

---

### Epic 10 — Agent Skills (optional, installable)

Optional, installable skills that let an LLM agent do the semantic synthesis
static analysis can't — using Sylva's now-rich MCP surface (Epics 1–9) as
substrate and **persisting results back**. Sylva stays deterministic and never
runs an LLM itself; the skills orchestrate the agent side. (GitHub milestone
"Epic 10 …".)

#### Feature 10.1 — instruction-set skill (project brief generation) (issue #61)
- Description: Generate a persistent **project instruction brief** (an
  `AGENTS.md` / CLAUDE-style doc): the inferred entrypoint(s), archetype +
  layers, main flow/spine, module boundaries, key hubs/chokepoints, and how to
  run/test — bootstrapped from Sylva's graph. The distilled "how this system
  works" that every future agent session reuses.
- **Shape (decided): a deterministic generator core + a thin skill wrapper.**
  `sylva.report.generate_brief(db) -> markdown` assembles the brief *mechanically*
  from the Epic 9 analysis (testable, reproducible, on-ethos); a Claude Code
  skill / `sylva brief` CLI invokes it and (optionally) lets the agent enrich the
  prose. Sylva provides the deterministic substrate; the agent narration is an
  optional layer, not required for the core.
- Inputs: db path, output path (default `AGENTS.md` / `SYLVA.md`)
- Process (deterministic, reuses existing analysis — no new graph logic):
  - Archetype + layers (9.8), primary + ranked entrypoints (9.1/9.1.1), main
    spine (9.2), hubs + chokepoints/gateways (4.3 / 9.3), module & package map
    (4.6/4.7), foreign boundaries (5.0), and how-to-run (console_scripts / main
    from the entrypoint markers). Pull module/symbol docstrings for purpose hints.
  - Render a structured markdown brief with stable section ordering.
  - `sylva brief --db … [--out AGENTS.md]` writes it; the skill file documents
    invocation + optional agent enrichment.
- Outputs: a written `AGENTS.md`-style brief; a `brief` CLI subcommand; a skill
  definition that invokes it
- Testing:
  - General: `generate_brief` produces markdown referencing the inferred primary
    entrypoint, archetype, and top modules for a known repo
  - Idempotent / regenerable (same graph → same brief)
  - Edge: empty graph → a minimal, valid brief (no crash)
  - Negative: db not found raises
- Note: highest-leverage skill. Depends on Epic 9 (esp. 9.1/9.8). No Rust change
  — pure Python report assembly over the existing analysis functions. The
  deterministic core is the product; the "skill" is the convenient invocation.

#### Feature 10.3 — diagram skill (whole-workflow block diagrams) (issue #63)
- Description: Produce **whole-workflow block diagrams** (Mermaid) from Sylva's
  graph — persisted artifacts (checked into the repo), not chat-only. Complements
  9.4 (which *renders* structure in the UI); this *exports* it, and gives an
  agent a base to annotate with meaning.
- **Shape (same as 10.1): deterministic generator core + a thin skill wrapper.**
  `sylva.diagrams.generate_diagrams(db) -> markdown` emits fenced ```mermaid
  blocks *mechanically* from the analysis; `sylva diagram` CLI writes
  `DIAGRAMS.md`; a skill invokes it and lets the agent add captions/annotations.
- Inputs: db path, output path (default `DIAGRAMS.md`)
- Process (deterministic, reuse existing analysis — no new graph logic):
  - **System flow** (`flowchart TD`) from Feature 9.4 `system_flow` (rooted at the
    inferred primary), with the main spine (9.2) styled distinctly.
  - **Module/package map** (`flowchart LR`) from Feature 4.7 `module_map`
    (package level), weighted edges; test packages filtered out.
  - **Layer diagram** (`flowchart TD`) from Feature 9.8 archetype/layers — the
    interface→business→data→transport tiers with counts.
  - Sanitise node ids/labels for valid Mermaid. GitHub renders the ```mermaid
    blocks; one `DIAGRAMS.md` artifact.
- Outputs: a `DIAGRAMS.md` with valid Mermaid diagrams; a `diagram` CLI
  subcommand; a skill definition
- Testing:
  - General: emits valid Mermaid (flowchart declarations + node/edge lines) for a
    known repo; the system-flow diagram contains the inferred primary + spine
  - Idempotent (same graph → same diagrams)
  - Edge: empty graph → a valid doc (no crash); db not found raises
- Note: depends on Epic 9 (9.2/9.4) + 4.7. No Rust change; pure Python emit over
  the existing analysis. Test code filtered (reuses 10.1's `_is_test_file`).
  Pairs with 10.1: brief (text) + diagrams (visual) = an onboarding pack.

#### Feature 10.4 — understand-project skill (orchestrator) (issue #64)
- Description: One command that takes an unknown repo to a documented, navigable,
  agent-ready state — the end-to-end realisation of "enable the LLM to understand
  the project". Orchestrates the deterministic pipeline; the agent-side test-gen
  step (10.2) is optional and documented in the skill.
- **Shape (same as 10.1/10.3): deterministic orchestrator core + skill wrapper.**
  `sylva.onboard.onboard(root, db) ` runs: **index** (walk + extract + foreign +
  edges + dataflow) → **brief** (10.1 → SYLVA.md) → **diagrams** (10.3 →
  DIAGRAMS.md) → **MCP scaffold** (8.2 → .codemcp/mcp.json). `sylva onboard --root`
  CLI. Each step is wrapped so a failure is recorded and the pipeline **continues**
  (graceful degradation) — the deterministic pipeline never runs the target.
- Inputs: root dir, db path (+ output paths)
- Process:
  - Factor the indexing core out of `_analyze` into a reusable
    `index_codebase(root, db)` (dedup; used by `analyze` and `onboard`).
  - `onboard()` runs index → brief → diagrams → init_mcp, collecting an artifact
    summary; per-step try/except so one failure doesn't abort the rest.
  - The skill wrapper documents the full agent pipeline including the optional
    10.2 (generate e2e tests → real exec paths) step, run between index and brief
    when a runnable env exists.
- Outputs: a `.codemcp/sylva.db` graph + `SYLVA.md` + `DIAGRAMS.md` +
  `.codemcp/mcp.json`, from one `sylva onboard` command; a skill definition
- Testing:
  - End-to-end on a sample repo yields graph + brief + diagrams + mcp scaffold
  - Graceful degradation: the pipeline completes without running the target; a
    per-step failure is recorded, not fatal
  - `index_codebase` produces the graph (symbols + edges); `_analyze` still works
    after the refactor (no regression)
  - Negative: non-directory root → error
- Note: sequenced last in Epic 10; pure orchestration over 10.1/10.3 + 8.2 +
  the indexer. Depends on Epic 9. 10.2's runtime test-gen stays agent-side/optional.

#### Feature 10.2 — generate-e2e-tests skill (verified logic paths) (issue #62)
- Description: The bridge from *possible* paths (static) to *verified* paths
  (real runs). A **skill** instructs an agent to write end-to-end tests for the
  inferred entrypoints / untested logic; those tests run in the project's own
  test workflow (coverage.py — Sylva executes nothing); Sylva ingests the
  coverage via the existing Epic 3 loop so the **execution paths (4.11)** become
  real, and surfaces them in the visualisation as first-class **logic-path block
  diagrams**. Sylva builds the *instruction*, never an LLM.
- **Shape: skill (primary) + a deterministic recommender + a viz surface.**
- Inputs: db path (recommender + viz); the skill drives the agent
- Process:
  - **Skill** (`skills/sylva-generate-tests.md`): target the inferred entrypoints
    (`infer_entrypoints`) / high-value untested logic (`suggest_test_targets`),
    write e2e tests, run with **per-test coverage**, and re-ingest via the
    existing `parse_coverage` → `map_tests_to_symbols` → `apply_coverage` loop
    (Epic 3) so `test_covers` edges + coverage exist. Opt-in; runs in the
    project's normal test env, not Sylva.
  - **Recommender** (deterministic, testable): `suggest_test_targets(db) ->
    [{symbol, file, reason, score}]` — rank symbols that (a) have **no inbound
    `test_covers`** edge and (b) **matter** (entrypoint / centrality /
    reachability). Tells the agent *what* to test. Exposed via MCP.
  - **Viz "Logic paths"** (deterministic, testable): a sidebar list of tests that
    have `test_covers` edges (an `/tests` endpoint) → click a test → its
    **execution-path block diagram** (reuse 4.11 `exec_path` + `renderLayered`),
    discoverable like 9.5 did for entrypoints. Makes verified logic paths
    first-class in the UI instead of hidden behind a per-node button.
- Outputs: a `sylva-generate-tests` skill; `suggest_test_targets` (fn + MCP tool);
  a `/tests` endpoint + a "Logic paths (from tests)" sidebar surface rendering
  exec-path diagrams
- Testing:
  - Recommender: an untested, high-centrality symbol is suggested; a
    well-covered symbol is not; empty graph → []; db not found raises
  - Viz: `/tests` lists tests with `test_covers` edges; clicking renders an
    exec-path diagram; the asset contains the logic-paths container + wiring
  - Skill artifact present + documents the target→write→run→ingest loop
- Note: the game-changer — real execution paths as block diagrams. Reuses Epic 3
  (coverage loop), 4.11 (exec-path rendering), 9.1/9.3 (targets). Sylva runs no
  code; the tests run in the project's own workflow (opt-in). Unblocks the
  dynamic-dataflow tier-2 (#54).
