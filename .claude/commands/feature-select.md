Set the active feature by ID or name.

Usage: /feature-select <id-or-name>
Example: /feature-select 2.1
Example: /feature-select "Mesh Struct"

Read CLAUDE.md and find the matching feature by ID or by partial name match.
If no argument is passed, list all features and ask the user to pick one.

Once identified:
1. Write the feature ID and name to .claude/active_feature:
   ```bash
   echo "2.1 — Mesh Struct" > .claude/active_feature
   ```
2. Display the full feature spec (description, inputs, process, outputs, testing strategy)
3. Confirm: "Feature 2.1 — Mesh Struct is now active."

If no match is found, list available features and ask the user to clarify.
