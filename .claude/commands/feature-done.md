Mark the specified feature as complete, or the active feature if none is passed.

Usage: /feature-done <id-or-name>
Example: /feature-done 2.1
Example: /feature-done (uses active feature from .claude/active_feature)

Prerequisites — verify before proceeding:
1. All tests pass (run `pytest python/tests/ -v` to confirm)
2. No uncommitted changes to files outside the feature scope
3. User has confirmed tests pass in their real project (ask explicitly)

Steps:
1. Identify the target feature — from argument or from .claude/active_feature
2. Ask: "Have you confirmed tests pass in your real project? (yes/no)"
   Do NOT proceed until the user confirms.
3. Ask: "How should the version be bumped?"
   - patch (default) — e.g. 0.2.0 → 0.2.1
   - minor — e.g. 0.2.0 → 0.3.0
   - major — e.g. 0.2.0 → 1.0.0
   - none — skip version bump
   If the user does not answer, default to patch.
4. Bump the version in BOTH Cargo.toml and pyproject.toml.
   Read the current version, increment the correct part, write it back.
5. Commit all changes. Use EXACTLY this commit message format, do not substitute 
   your own format, do not use conventional commits format:
```bash
   git add -A
   git commit -m "CLOSES: Feature X.Y — [feature name] (vX.X.X) Closes #"
```
6. Close the corresponding GitHub issue:
```bash
   gh issue close <issue-number> --comment "CLOSES feature. All tests passing."
```
   Find the issue number by matching the feature name:
```bash
   gh issue list --state open --json number,title
```
7. Clear the active feature:
```bash
   rm -f .claude/active_feature
```
8. Push to remote:
```bash
   git push
```
9. Report: "Feature X.Y — [name] is complete. Version bumped to X.X.X. Ready for next feature."
10. Show the next pending feature from CLAUDE.md as a suggestion.