Run tests for the specified feature, or the active feature if none is passed.

Usage: /feature-test <id-or-name>
Example: /feature-test 2.1
Example: /feature-test (uses active feature from .claude/active_feature)

Steps:
1. Identify the target feature — from argument or from .claude/active_feature
2. Identify the test file for this feature (e.g. python/tests/test_epic2_mesh_struct.py)
3. Build the current code:
   ```bash
   maturin develop
   ```
4. Run the full test suite first to check for regressions:
   ```bash
   pytest python/tests/ -v
   ```
5. Then run only this feature's tests for focused output:
   ```bash
   pytest python/tests/test_epicN_*.py -v
   ```
6. Report:
   - Total tests passed / failed
   - Any regressions (tests that were passing before and now fail)
   - Any new failures
   - Compilation errors if any

If any regressions are found, flag them clearly and do not proceed.
The user must manually confirm tests pass before the feature is considered done.
