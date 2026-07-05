---
name: sylva-understand
description: Point an agent at an unknown repo and have it produce the interactive block diagrams — including real execution-path and data-flow views, writing e2e tests for any entrypoint that lacks them.
---

# sylva-understand — understand a repo, then draw it (for real)

The one-button orchestration. An agent triggers this to take an unfamiliar repo
to a **navigable, diagrammed, agent-ready** state: it indexes the code, learns
the real structure, and produces the interactive block diagrams in
`sylva serve-ui` — including the **execution-path** diagrams, which it makes real
by writing end-to-end tests for any entrypoint that has none.

Sylva stays deterministic and runs no LLM. The deterministic steps are `sylva`
commands; the only agent-authored step is **writing the missing e2e tests**. The
project's own tests are the only thing that executes code (opt-in, your env).

## Steps

1. **Understand the repo + get the plan.** This indexes the code and lists the
   real entrypoints, flagging which have **no** test exercising them:
   ```
   sylva understand --root .
   ```
   The `-> ... [NO TEST]` rows are your targets. (Add `--json` for machine form;
   the `suggest_test_targets` / `infer_entrypoints` MCP tools give the same data.)

2. **Write end-to-end tests for the untested entrypoints.** Use the `get_source`
   MCP tool to read each entrypoint, then drive it the way a user would — a
   request to a route, invoking a CLI command, calling the public API — and
   assert observable behaviour, not internals. One test that drives an entrypoint
   exercises the whole path beneath it.

3. **Turn the runs into execution-path block diagrams** — one command runs the
   project's tests with per-test coverage and ingests the result:
   ```
   sylva logic-paths --run "coverage run -m pytest" --source <your_package>
   ```
   (If you prefer to run tests yourself: run them with
   `dynamic_context = test_function` set, then `sylva logic-paths --coverage-file .coverage`.)

4. **(Optional) Colour the map by coverage:**
   ```
   sylva coverage --run "coverage run -m pytest && coverage lcov"
   ```

5. **Open the interactive diagrams:**
   ```
   sylva serve-ui
   ```
   - **▤ System flow (whole project)** — the high-level block diagram, rooted at
     the inferred entrypoint, main spine highlighted.
   - **Logic paths (from tests)** (sidebar) — each test you wrote → click it for
     the **execution-path** block diagram it actually ran.
   - **◆ data flow** (from any node) — how a symbol's parameter data flows.
   - **Coverage mode** — red→green if you did step 4.

## What's deterministic vs agent-authored
- **Deterministic (Sylva):** indexing, entrypoint/spine/layer inference, the
  test plan, coverage ingest, and every rendered block diagram.
- **Agent-authored:** only the e2e tests in step 2. Everything else is a `sylva`
  command.

## Notes
- **Order matters if the project's tests clean up aggressively:** run the tests
  (step 3) with the graph db kept outside the repo (`--db /tmp/x.db` on every
  command) if a test suite deletes working directories.
- Data flow shown is **static parameter flow** (deterministic). Runtime
  data-flow is a future step.
- Skip steps 2–4 to stay fully static — the System-flow and data-flow diagrams
  need no tests.
