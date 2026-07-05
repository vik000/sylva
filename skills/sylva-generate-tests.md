---
name: sylva-generate-tests
description: Write end-to-end tests for a project's key logic, then turn the real runs into verified logic-path diagrams.
---

# sylva-generate-tests — verified logic paths

Turn *possible* execution paths (static analysis) into *verified* ones. This
skill guides an agent to write end-to-end tests for the logic that matters, run
them in the project's own test workflow, and feed the coverage back into Sylva —
so the visualisation shows the paths that **actually executed** as block diagrams.

Sylva runs nothing here. It tells you **what to test**, then **ingests** the
coverage your normal test run produces.

## Steps

1. **Find the targets.** Ask Sylva what untested logic matters most:
   ```
   python -c "import sylva, json; print(json.dumps(sylva.suggest_test_targets('.codemcp/sylva.db'), indent=2))"
   ```
   (or the `suggest_test_targets` MCP tool). Each item has a `symbol`, `file`,
   a `reason` (primary entrypoint / chokepoint / gateway …), and a `score`.
   Prioritise the top-scoring, untested symbols — especially entrypoints.

2. **Write end-to-end tests** for those targets. Use `get_source` to read the
   code, exercise the real entrypoints (a request to a route, invoking a CLI
   command, calling the public API), and assert observable behaviour — not
   internals. Aim for tests that drive execution *through* the chokepoints and
   gateways Sylva flagged.

3. **Run them with per-test coverage** — in the project's own environment (this
   is the only step that executes code, and it's your normal test run):
   ```
   coverage run --context=test -m pytest
   coverage lcov            # or a context-annotated report
   ```

4. **Ingest the coverage** so the paths become queryable:
   ```python
   import sylva
   cov = sylva.parse_coverage("coverage.lcov", "lcov")
   sylva.apply_coverage(".codemcp/sylva.db", cov)
   # per-test trace: {test_name: {source_path: [lines]}}
   sylva.map_tests_to_symbols(".codemcp/sylva.db", trace)
   ```

5. **See the verified logic paths.** Re-open `sylva serve-ui`: each test now
   appears under **"Logic paths (from tests)"** in the sidebar — click one to
   render the execution path it actually exercised as a block diagram.

## Why it matters
Static views show what *could* run; these show what *did*. The result is a set of
verified logic-path diagrams grounded in real execution — and the foundation for
runtime data-flow (a future step).

## Notes
- **Opt-in / environment-dependent:** step 3 runs the target's tests; skip this
  skill to keep everything static.
- Targets come from Sylva deterministically (`suggest_test_targets`); the test
  *writing* is the agent's job.
