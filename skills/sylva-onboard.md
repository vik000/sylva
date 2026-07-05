---
name: sylva-onboard
description: Take an unknown codebase to a documented, navigable, agent-ready state in one pass.
---

# sylva-onboard — understand a project end to end

Turn an unfamiliar repo into a fully mapped, agent-ready workspace: a code graph,
a written brief, workflow diagrams, and an MCP scaffold — then (optionally) real
execution paths. This is the top-level orchestrator over Sylva's other skills.

## Deterministic pipeline (one command)

```
sylva onboard --root .
```

Runs, safely and without executing the target repo:
1. **Index** — parse Python + black-box foreign (Rust/PyO3) modules → `.codemcp/sylva.db`
2. **Brief** (10.1) → `SYLVA.md` — archetype, layers, entrypoints, spine, how-to-run
3. **Diagrams** (10.3) → `DIAGRAMS.md` — Mermaid system flow / module map / layer tiers
4. **MCP scaffold** (8.2) → `.codemcp/mcp.json` — wire the graph tools into an agent

Each step is isolated: a failure is reported but the rest still runs.

## Optional runtime step (agent-side)

Between indexing and the brief, when the project has a **runnable environment**
(deps installed, tests present), an agent may:
- generate missing **end-to-end tests** for the inferred entrypoints (skill
  `sylva-generate-tests` / Feature 10.2),
- run them with coverage, and re-apply it, so Sylva's **execution-path** views
  (Feature 4.11) show the *real* paths exercised — not just the static ones.

This step runs the target's code, so it is **opt-in** and environment-dependent —
skip it to keep the pipeline fully static.

## Result

An unknown repo becomes: a queryable graph + `SYLVA.md` + `DIAGRAMS.md` + an MCP
config — everything a fresh agent session needs to understand and work in the
codebase, produced deterministically.
