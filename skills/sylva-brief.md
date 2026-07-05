---
name: sylva-brief
description: Generate (and optionally enrich) a project instruction brief from Sylva's code graph.
---

# sylva-brief — project instruction brief

Produce a durable `SYLVA.md` "how this system works" brief that a fresh agent
session can read instead of re-deriving the project's structure every time.

This skill is a thin wrapper over Sylva's **deterministic** brief generator —
Sylva does the mechanical analysis; the agent (optionally) adds the prose only it
can (purpose, conventions, gotchas).

## Steps

1. **Ensure the graph is current.** If `.codemcp/sylva.db` is missing or stale,
   index first:
   ```
   sylva analyze --root .
   ```

2. **Generate the deterministic brief:**
   ```
   sylva brief --db .codemcp/sylva.db --out SYLVA.md
   ```
   This writes a structured `SYLVA.md`: archetype + architectural layers,
   inferred entrypoints (primary first), the main execution spine, key modules,
   hubs / chokepoints / gateways, and how to run — all straight from the graph.

3. **(Optional) Enrich the prose.** The generated sections are factual but terse.
   Using Sylva's MCP tools for evidence, add what static analysis can't infer:
   - `get_source` on the primary entrypoint / key symbols to summarise *purpose*;
   - a short "Conventions" section (naming, error handling, testing) you observe;
   - any gotchas or non-obvious constraints.
   Keep additions below the generated sections; note that re-running
   `sylva brief` overwrites the file, so preserve manual prose separately (or
   `--out SYLVA.enriched.md`).

## Notes
- Deterministic core: `python -c "import sylva.report as r; print(r.generate_brief('.codemcp/sylva.db'))"`.
- The brief is reproducible — same graph → same output — so it is safe to
  regenerate in CI or on re-index.
