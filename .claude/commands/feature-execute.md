Implement the specified feature, or the active feature if none is passed.

Usage: /feature-execute <id-or-name>
Example: /feature-execute 2.1
Example: /feature-execute (uses active feature from .claude/active_feature)

Steps:
1. Identify the target feature — from argument or from .claude/active_feature
2. Read the full feature spec from CLAUDE.md
3. Identify which source files this feature belongs to (e.g. src/mesh.rs for Epic 2)
4. Implement the feature — confined ONLY to the files specified in the feature spec
   DO NOT touch any other files unless explicitly required by the spec
5. Export all new Rust functions via PyO3 in src/lib.rs
6. Write Python tests in the correct test file (one file per feature, never overwrite existing)
7. Run:
   ```bash
   maturin develop
   pytest python/tests/ -v
   ```
8. Report: how many tests passed, how many failed, any compilation errors

If tests fail, fix them before reporting completion.
If compilation fails, fix it before running tests.
Do not declare the feature done until all tests pass.
