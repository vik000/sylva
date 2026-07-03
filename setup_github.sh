#!/usr/bin/env bash
# Run this once from inside the sylva/ directory after `gh auth login`
# Creates the GitHub repo, milestones (epics), and all issues.

set -e

OWNER=$(gh api user --jq .login)
REPO="sylva"

echo "Creating repo $OWNER/$REPO..."
gh repo create "$REPO" \
  --public \
  --description "Codebase knowledge graph — AST parsing, SQLite graph, MCP server" \
  --source=. \
  --remote=origin \
  --push

echo "Creating milestones (epics)..."

M1=$(gh api repos/$OWNER/$REPO/milestones --method POST \
  --field title="Epic 1 — MVP: Python parsing + MCP server" \
  --field description="Scaffold, SQLite schema, Python file walker, AST extractor, graph writer, MCP server with 3 core tools." \
  --jq .number)

M2=$(gh api repos/$OWNER/$REPO/milestones --method POST \
  --field title="Epic 2 — Incremental Indexing" \
  --field description="File hash tracking, file watcher, gitignore-aware walking." \
  --jq .number)

M3=$(gh api repos/$OWNER/$REPO/milestones --method POST \
  --field title="Epic 3 — Test Coverage Integration" \
  --field description="Parse coverage.py output, correlate to symbols, test-to-symbol mapping, rollup, MCP tools." \
  --jq .number)

M4=$(gh api repos/$OWNER/$REPO/milestones --method POST \
  --field title="Epic 4 — Richer Graph Queries + Visualisation" \
  --field description="Call chain tracing, blast radius, architecture summary, interactive D3 UI, coverage overlay." \
  --jq .number)

M5=$(gh api repos/$OWNER/$REPO/milestones --method POST \
  --field title="Epic 5 — Additional Language Support" \
  --field description="Pluggable extractor trait, Rust extractor, TypeScript/JS extractor, multi-language coverage." \
  --jq .number)

M6=$(gh api repos/$OWNER/$REPO/milestones --method POST \
  --field title="Epic 6 — Semantic Layer" \
  --field description="Vector embeddings, hybrid BM25+vector search, graph export for external tools." \
  --jq .number)

echo "Milestones created: $M1 $M2 $M3 $M4 $M5 $M6"

echo "Creating issues..."

# ── Epic 1 ────────────────────────────────────────────────────────────────────

gh issue create \
  --title "Feature 1.1 — Project scaffold" \
  --milestone "$M1" \
  --body "## Description
Rust workspace, PyO3 bindings, CI pipeline, pytest harness.

## Inputs
None.

## Outputs
- \`import sylva\` succeeds in Python
- \`pytest python/tests/ -v\` passes

## Testing
- **General:** module imports, module name is \`sylva\`
- **Error control:** compilation errors surfaced clearly

## Notes
Run \`maturin develop\` after any Rust change. Do not proceed to Feature 1.2 until tests pass."

gh issue create \
  --title "Feature 1.2 — SQLite schema and migrations" \
  --milestone "$M1" \
  --body "## Description
Define and initialise the graph data model in SQLite using rusqlite.

## Schema
\`\`\`sql
files   (id INTEGER PK, path TEXT UNIQUE, hash TEXT, indexed_at TEXT)
symbols (id INTEGER PK, file_id INTEGER FK, name TEXT, kind TEXT,
         line_start INTEGER, line_end INTEGER, docstring TEXT, coverage_pct REAL)
edges   (id INTEGER PK, src_id INTEGER FK, dst_id INTEGER FK, kind TEXT)
\`\`\`

## Inputs
- \`db_path: str\` — path to \`.codemcp/sylva.db\`

## Outputs
- \`sylva.init_db(db_path: str) -> None\`

## Testing
- **General:** DB created at correct path, all tables present, columns correct
- **Edge:** calling \`init_db\` twice is idempotent (no error, no duplicate tables)
- **Negative:** unwritable path raises clear error
- **Error control:** migration errors surface with meaningful message"

gh issue create \
  --title "Feature 1.3 — Python file walker" \
  --milestone "$M1" \
  --body "## Description
Walk a directory tree and return all \`.py\` file paths. Respects basic ignore patterns.
(Full gitignore support comes in Epic 2.)

## Inputs
- \`root: str\` — directory to walk
- \`extra_ignore: list[str]\` — optional extra patterns to skip

## Outputs
- \`sylva.walk_python_files(root: str, extra_ignore: list[str] = []) -> list[str]\`

## Testing
- **General:** finds all .py files in a temp directory tree
- **Edge:** empty directory returns \`[]\`, single file, deeply nested dirs
- **Negative:** non-existent root raises \`FileNotFoundError\`
- **Error control:** permission errors on subdirs are logged and skipped, not fatal"

gh issue create \
  --title "Feature 1.4 — Python AST extractor" \
  --milestone "$M1" \
  --body "## Description
Parse a Python source file with tree-sitter-python and extract symbols.

## Inputs
- \`file_path: str\`

## Outputs
- \`sylva.extract_symbols(file_path: str) -> list[dict]\`
- Each dict: \`{name: str, kind: str, line_start: int, line_end: int, docstring: str | None}\`
- \`kind\` values: \`function\`, \`class\`, \`import\`

## Testing
- **General:** extracts functions, classes, imports from a real .py file
- **Edge:** empty file returns \`[]\`, file with only comments, nested classes
- **Negative:** non-existent file raises \`FileNotFoundError\`
- **Error control:** parse errors return partial results with error flag, not a crash"

gh issue create \
  --title "Feature 1.5 — Graph writer" \
  --milestone "$M1" \
  --body "## Description
Write extracted symbols and file records into the SQLite graph.

## Inputs
- \`db_path: str\`
- \`file_path: str\`
- \`symbols: list[dict]\` — from Feature 1.4

## Outputs
- \`sylva.write_symbols(db_path: str, file_path: str, symbols: list[dict]) -> int\`
- Returns count of symbols written.

## Testing
- **General:** symbols appear in DB after write, file record created
- **Edge:** writing same symbols twice is idempotent (upsert, no duplicates)
- **Negative:** invalid DB path raises clear error
- **Error control:** partial write failure rolls back full transaction"

gh issue create \
  --title "Feature 1.6 — MCP server: search_symbol, get_callers, get_dependencies" \
  --milestone "$M1" \
  --body "## Description
Expose the graph over stdio MCP protocol with 3 core tools.

## MCP Tools
- \`search_symbol(name_pattern: str) -> list[dict]\` — regex search over symbol names
- \`get_callers(symbol: str) -> list[dict]\` — symbols that call this one
- \`get_dependencies(symbol: str) -> list[dict]\` — symbols this one imports/calls

## Invocation
\`\`\`bash
sylva serve --db .codemcp/sylva.db
\`\`\`

## Testing
- **General:** each tool returns correct results for known graph content
- **Edge:** query for non-existent symbol returns empty list, not error
- **Negative:** malformed JSON-RPC input returns JSON-RPC error response
- **Error control:** DB not found returns clear error on startup, not silent hang"

# ── Epic 2 ────────────────────────────────────────────────────────────────────

gh issue create \
  --title "Feature 2.1 — File hash tracking" \
  --milestone "$M2" \
  --body "## Description
Store SHA-256 hash of each file at index time. On reindex, skip files whose hash is unchanged.

## Inputs
- \`db_path: str\`
- \`file_path: str\`

## Outputs
- \`sylva.file_needs_reindex(db_path: str, file_path: str) -> bool\`

## Testing
- **General:** unchanged file returns \`False\`, modified file returns \`True\`, new file returns \`True\`
- **Edge:** empty file has stable hash, very large file hashes correctly
- **Negative:** unreadable file raises error
- **Error control:** DB write failure on hash update is logged and retried once"

gh issue create \
  --title "Feature 2.2 — File watcher" \
  --milestone "$M2" \
  --body "## Description
Watch a directory for file changes and trigger incremental reindex.

## Inputs
- \`root: str\`
- \`db_path: str\`
- \`debounce_ms: int\` (default 300)

## Outputs
- \`sylva.start_watcher(root: str, db_path: str, debounce_ms: int = 300) -> WatcherHandle\`
- Non-blocking. Returns a handle with a \`.stop()\` method.

## Testing
- **General:** modifying a .py file triggers reindex within debounce window
- **Edge:** creating a new file indexes it, deleting a file marks it removed in DB
- **Negative:** non-existent root raises error on start
- **Error control:** watcher errors are logged; watcher recovers without crashing"

gh issue create \
  --title "Feature 2.3 — gitignore-aware file walking" \
  --milestone "$M2" \
  --body "## Description
Upgrade the file walker from Feature 1.3 to respect .gitignore and .codemcpignore hierarchies.

## Inputs
- \`root: str\`

## Outputs
- \`sylva.walk_python_files(root: str) -> list[str]\` — same signature, now respects ignore files

## Testing
- **General:** gitignored files excluded, non-ignored files included
- **Edge:** nested .gitignore files respected, .codemcpignore overrides .gitignore
- **Negative:** malformed .gitignore line is skipped with warning, not fatal
- **Error control:** missing .gitignore is silently ignored (not an error)"

# ── Epic 3 ────────────────────────────────────────────────────────────────────

gh issue create \
  --title "Feature 3.1 — Parse coverage.py output (LCOV / Cobertura XML)" \
  --milestone "$M3" \
  --body "## Description
Parse line-level coverage data from coverage.py reports.

## Inputs
- \`path: str\` — path to coverage report file
- \`format: str\` — \`lcov\` or \`cobertura\`

## Outputs
- \`sylva.parse_coverage(path: str, format: str) -> dict[str, dict[int, bool]]\`
- Maps file path → line number → covered (True/False)

## Testing
- **General:** parses real coverage.py LCOV output correctly
- **Edge:** file with 0% coverage, file with 100% coverage, file not in report
- **Negative:** invalid format raises \`ValueError\`, corrupt file raises clear error
- **Error control:** unrecognised file paths in report are skipped with warning"

gh issue create \
  --title "Feature 3.2 — Correlate coverage to graph symbols" \
  --milestone "$M3" \
  --body "## Description
Match covered lines to symbol boundaries; attach coverage % as node attribute in DB.

## Inputs
- \`db_path: str\`
- \`coverage: dict[str, dict[int, bool]]\` — from Feature 3.1

## Outputs
- \`sylva.apply_coverage(db_path: str, coverage: dict) -> int\` — count of updated symbols

## Testing
- **General:** symbol spanning covered lines gets correct %, uncovered symbol gets 0%
- **Edge:** symbol with no lines in coverage report gets \`null\` (not 0%), partial coverage
- **Negative:** symbol not in DB is skipped without error
- **Error control:** DB update failure rolls back per-symbol, not full batch"

gh issue create \
  --title "Feature 3.3 — Test-to-symbol mapping" \
  --milestone "$M3" \
  --body "## Description
Build edges from test functions to the symbols they exercise, using per-test coverage trace data.

## Inputs
- \`db_path: str\`
- \`trace: dict[str, dict[str, dict[int, bool]]]\` — test name → file → line → covered

## Outputs
- \`sylva.map_tests_to_symbols(db_path: str, trace: dict) -> int\` — count of edges written

## Testing
- **General:** test that calls function X produces a \`test_covers\` edge test→X
- **Edge:** test that calls nothing produces no edges, fixture-only test
- **Negative:** trace referencing unknown symbol is skipped
- **Error control:** duplicate edges are upserted, not duplicated"

gh issue create \
  --title "Feature 3.4 — Module-level coverage rollup" \
  --milestone "$M3" \
  --body "## Description
Aggregate symbol-level coverage into per-module and per-file totals.

## Inputs
- \`db_path: str\`

## Outputs
- \`sylva.get_module_coverage(db_path: str) -> dict[str, float | None]\`
- Maps module/file path → coverage % (or \`None\` if no coverage data)

## Testing
- **General:** module with mixed coverage returns correct weighted average
- **Edge:** module with no coverage data returns \`None\`, empty module
- **Negative:** DB not found raises clear error
- **Error control:** symbols with null coverage excluded from average, warning logged"

gh issue create \
  --title "Feature 3.5 — MCP tools for coverage queries" \
  --milestone "$M3" \
  --body "## Description
Expose coverage data over MCP alongside the existing tools from Feature 1.6.

## MCP Tools
- \`get_coverage(symbol: str) -> {coverage_pct: float | null}\`
- \`get_uncovered_paths(module: str) -> list[dict]\` — symbols with 0% or null coverage
- \`get_test_coverage(symbol: str) -> list[str]\` — test names that cover this symbol

## Testing
- **General:** each tool returns correct results for known coverage data
- **Edge:** \`get_uncovered_paths\` returns empty list when fully covered
- **Negative:** unknown symbol returns null coverage, not error
- **Error control:** DB unavailable returns MCP error response, not crash"

# ── Epic 4 ────────────────────────────────────────────────────────────────────

gh issue create \
  --title "Feature 4.1 — Call chain tracing" \
  --milestone "$M4" \
  --body "## Description
Traverse inbound/outbound call edges to N depth and return structured paths.

## Inputs
- \`db_path: str\`
- \`symbol: str\`
- \`direction: str\` — \`inbound\`, \`outbound\`, or \`both\`
- \`depth: int\` — max traversal depth

## Outputs
- \`sylva.trace_calls(db_path, symbol, direction, depth) -> list[dict]\`
- Each dict: \`{symbol, kind, distance}\`

## Testing
- **General:** traces direct caller, multi-hop chain, bidirectional
- **Edge:** depth=0 returns just the root symbol, circular calls don't loop forever
- **Negative:** unknown symbol returns empty list, invalid direction raises \`ValueError\`
- **Error control:** depth limit enforced strictly, cycle detection prevents infinite loop"

gh issue create \
  --title "Feature 4.2 — Blast radius analysis" \
  --milestone "$M4" \
  --body "## Description
Given a symbol, return all symbols that would break if its signature changed.

## Inputs
- \`db_path: str\`
- \`symbol: str\`

## Outputs
- \`sylva.blast_radius(db_path: str, symbol: str) -> list[dict]\`
- Each dict: \`{symbol, kind, distance}\` ordered by distance

## Testing
- **General:** widely-used utility returns all direct and indirect callers
- **Edge:** leaf symbol (nothing calls it) returns empty list
- **Negative:** unknown symbol returns empty list
- **Error control:** cycle detection prevents infinite traversal"

gh issue create \
  --title "Feature 4.3 — Architecture summary tool" \
  --milestone "$M4" \
  --body "## Description
Top-level codebase view: modules, entry points, most-connected symbols (hubs).

## Inputs
- \`db_path: str\`
- \`top_n: int\` (default 10) — number of hub symbols to return

## Outputs
- \`sylva.get_architecture(db_path: str, top_n: int = 10) -> dict\`
- Keys: \`modules: list[str]\`, \`hubs: list[dict]\`, \`entry_points: list[dict]\`

## Testing
- **General:** returns correct hub symbols for a known graph
- **Edge:** single-file repo, repo with no imports
- **Negative:** empty DB returns empty architecture dict, not error
- **Error control:** missing DB raises clear error"

gh issue create \
  --title "Feature 4.4 — Interactive visualisation UI (D3 force graph)" \
  --milestone "$M4" \
  --body "## Description
Local web UI served by the binary. Force-directed module graph built with D3.
The HTML file is embedded in the binary at compile time via \`include_str!\`.

## Behaviour
- Nodes = modules/symbols, edges = call/import relationships
- Force-directed layout, clickable nodes show symbol detail panel
- Filter by module, filter by depth from a selected node
- Served at \`http://localhost:7700\` by default

## Invocation
\`\`\`bash
sylva serve-ui --db .codemcp/sylva.db [--port 7700]
\`\`\`

## Outputs
- \`/graph.json\` endpoint returns full graph as \`{nodes: [...], edges: [...]}\`
- \`/\` serves the HTML UI

## Testing
- **General:** server starts, \`/graph.json\` returns valid JSON, HTML page loads
- **Edge:** empty graph renders without error, large graph (1000+ nodes) loads within 3s
- **Negative:** port in use raises clear error with suggestion to use \`--port\`
- **Error control:** DB not found returns HTTP 500 with JSON error body"

gh issue create \
  --title "Feature 4.5 — Coverage overlay in visualisation" \
  --milestone "$M4" \
  --body "## Description
Extend the module graph UI with a coverage mode. Requires Epic 3 data and Feature 4.4 UI.

## Behaviour
- Toggle button in UI header switches between structural and coverage views
- Coverage mode: nodes coloured red→green by coverage %, % shown on hover
- Untested logic paths highlighted with dashed edges
- Grey nodes = no coverage data

## Testing
- **General:** coverage mode colours nodes correctly for known coverage data
- **Edge:** node with null coverage shown as grey, fully covered node is green
- **Negative:** toggling when no coverage data shows informative empty state
- **Error control:** partial coverage data (some symbols null) renders without crash"

# ── Epic 5 ────────────────────────────────────────────────────────────────────

gh issue create \
  --title "Feature 5.1 — Pluggable language extractor trait" \
  --milestone "$M5" \
  --body "## Description
Refactor the Python extractor behind a clean Rust trait so new languages are drop-in modules.

## Trait
\`\`\`rust
trait Extractor {
    fn extensions(&self) -> &[&str];
    fn extract(&self, path: &Path) -> Result<Vec<Symbol>>;
}
\`\`\`

## Outputs
- \`sylva.list_languages() -> list[str]\` — returns registered language names

## Testing
- **General:** Python extractor still works after refactor, \`list_languages()\` returns \`[\"python\"]\`
- **Edge:** registering same language twice is idempotent
- **Negative:** unknown file extension returns empty symbols, not error
- **Error control:** extractor panic is caught and returned as error, not binary crash"

gh issue create \
  --title "Feature 5.2 — Rust extractor" \
  --milestone "$M5" \
  --body "## Description
tree-sitter-rust extractor: fns, structs, traits, impl blocks.

## Inputs
- \`.rs\` file path

## Outputs
- \`sylva.extract_symbols(path)\` works for \`.rs\` files (same interface as Python extractor)

## Testing
- **General:** extracts \`fn\`, \`struct\`, \`trait\`, \`impl\` from a real .rs file
- **Edge:** empty file, file with only comments, nested impl blocks
- **Negative:** non-Rust file passed raises clear error
- **Error control:** parse error returns partial results with error flag, not crash"

gh issue create \
  --title "Feature 5.3 — TypeScript/JavaScript extractor" \
  --milestone "$M5" \
  --body "## Description
tree-sitter-typescript extractor: functions, classes, interfaces, type aliases.

## Inputs
- \`.ts\` or \`.js\` file path

## Outputs
- \`sylva.extract_symbols(path)\` works for \`.ts\`/\`.js\` files

## Testing
- **General:** extracts function, class, interface from a real .ts file
- **Edge:** .jsx/.tsx files, empty file, ES modules vs CommonJS
- **Negative:** non-TS/JS file raises clear error
- **Error control:** parse error returns partial results with error flag"

gh issue create \
  --title "Feature 5.4 — Coverage support for Rust and TypeScript" \
  --milestone "$M5" \
  --body "## Description
Wire up cargo-tarpaulin (Rust) and Istanbul/c8 (TypeScript) coverage reports.
All emit LCOV — the parser from Feature 3.1 reuses unchanged.

## Outputs
- \`sylva.parse_coverage(path, \"lcov\")\` works for tarpaulin and Istanbul reports
- Document the commands to generate reports for each language in README

## Testing
- **General:** parses tarpaulin LCOV and Istanbul LCOV output correctly
- **Edge:** mixed-language LCOV report (multiple SF: entries for different languages)
- **Negative:** missing coverage file raises \`FileNotFoundError\`
- **Error control:** unrecognised file paths in multi-language report are skipped with warning"

# ── Epic 6 ────────────────────────────────────────────────────────────────────

gh issue create \
  --title "Feature 6.1 — Vector embeddings on symbols" \
  --milestone "$M6" \
  --body "## Description
Embed docstrings + signatures via sqlite-vec. Enables semantic symbol search.

## Inputs
- \`db_path: str\`
- \`model: str\` — model name or local path

## Outputs
- \`sylva.embed_symbols(db_path: str, model: str) -> int\` — count of embedded symbols

## Testing
- **General:** embedded symbols retrievable by vector similarity
- **Edge:** symbol with no docstring uses signature only, empty signature skipped
- **Negative:** invalid model name raises \`ValueError\`
- **Error control:** embedding failure retries once, then marks symbol as unembedded (not crash)"

gh issue create \
  --title "Feature 6.2 — Hybrid search: BM25 + vector (RRF)" \
  --milestone "$M6" \
  --body "## Description
Combine FTS5 (BM25) exact search and vector similarity search via Reciprocal Rank Fusion.

## Inputs
- \`db_path: str\`
- \`query: str\`
- \`k: int\` (default 10)

## Outputs
- \`sylva.search(db_path: str, query: str, k: int = 10) -> list[dict]\`
- Each dict: \`{name, kind, file, score}\` ordered by fused rank

## Testing
- **General:** exact name match ranks high, semantic match surfaces related symbols
- **Edge:** k larger than result set returns all results, empty query returns empty list
- **Negative:** unembedded DB falls back to FTS5 only with warning
- **Error control:** vector search failure falls back to BM25 only, not crash"

gh issue create \
  --title "Feature 6.3 — Graph export for external tools (JSON / GraphML)" \
  --milestone "$M6" \
  --body "## Description
Export the full graph to JSON or GraphML for Obsidian, Gephi, Graphviz, etc.

## Inputs
- \`db_path: str\`
- \`format: str\` — \`json\` or \`graphml\`
- \`output_path: str\`

## Outputs
- \`sylva.export_graph(db_path: str, format: str, output_path: str) -> dict\`
- Returns \`{nodes: int, edges: int}\`

## Testing
- **General:** JSON export is valid JSON with correct node/edge structure; GraphML is valid XML
- **Edge:** empty graph exports as valid empty structure, large graph exports without timeout
- **Negative:** unknown format raises \`ValueError\`, unwritable output path raises \`PermissionError\`
- **Error control:** partial export failure cleans up the partial file"

echo ""
echo "✓ Repo created: https://github.com/$OWNER/$REPO"
echo "✓ 6 milestones (epics) created"
echo "✓ 22 issues created"
echo ""
echo "Next steps:"
echo "  cd sylva"
echo "  gh repo clone vik000/claude-workflows /tmp/claude-workflows"
echo "  cp -r /tmp/claude-workflows/rust-pyo3/.claude ."
echo "  rm -rf /tmp/claude-workflows"
echo "  git add -f .claude && git commit -m 'chore: add rust-pyo3 slash commands' && git push"
echo "  claude  # then /features to verify slash commands"
