---
name: sylva-diagram
description: Generate (and optionally annotate) whole-workflow Mermaid diagrams from Sylva's code graph.
---

# sylva-diagram — whole-workflow diagrams

Produce a `DIAGRAMS.md` of Mermaid block diagrams — the system flow, the package
module map, and the architectural-layer tiers — straight from the code graph.
GitHub renders the Mermaid blocks inline.

This is a thin wrapper over Sylva's **deterministic** diagram generator: Sylva
draws the *structure*; the agent (optionally) adds the *meaning* static layout
can't caption (what a flow does, business steps, data lineage).

## Steps

1. **Ensure the graph is current** (`sylva analyze --root .` if stale).

2. **Generate the diagrams:**
   ```
   sylva diagram --db .codemcp/sylva.db --out DIAGRAMS.md
   ```
   Writes `DIAGRAMS.md` with:
   - **System flow** — the reachable call flow from the inferred primary
     entrypoint, main spine highlighted;
   - **Module map** — package dependencies with weights;
   - **Architectural layers** — interface/business/data/transport tiers.

3. **(Optional) Annotate.** The diagrams are structural. Using `get_source` /
   `get_architecture` for evidence, add captions describing what each flow/tier
   *means*, or add a hand-written Mermaid diagram of a key business workflow
   below the generated ones. Re-running overwrites `DIAGRAMS.md`, so keep manual
   diagrams in a separate file (or `--out DIAGRAMS.annotated.md`).

## Notes
- Pairs with the `sylva-brief` skill: brief (text) + diagrams (visual) =
  a complete onboarding pack.
- Deterministic core: `python -c "import sylva.diagrams as d; print(d.generate_diagrams('.codemcp/sylva.db'))"`.
