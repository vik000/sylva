List all features defined in CLAUDE.md.

Read CLAUDE.md and extract all Epics and Features. Present them in a clear table showing:
- Feature ID (e.g. 1.1, 2.3)
- Feature name
- Epic it belongs to
- Status: mark as ✅ Done if its GitHub issue is closed, 🔄 In Progress if the branch exists, ⬜ Pending otherwise

Check GitHub issue status using:
```bash
gh issue list --state all --json number,title,state,milestone
```

Present the result as a clean table. Highlight the currently active feature if one is set in .claude/active_feature.
