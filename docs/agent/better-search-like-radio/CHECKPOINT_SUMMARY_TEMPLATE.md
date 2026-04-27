# Checkpoint [NUMBER]: Post-Batch [NUMBER] Summary

**Date**: YYYY-MM-DD
**Batch**: [Batch number and name]
**Phases Merged**: [List of phase numbers and names]
**Result**: [PASSED | PASSED WITH FIXES | ESCALATED]

---

## Merge Results

| Phase | Branch | Merge Status | Conflicts |
|-------|--------|-------------|-----------|
| [N] | phase-[N]-[name] | Clean / Conflict / Failed | None / [details] |

### Conflict Resolutions

> Skip this section if all merges were clean.

[For each conflict, describe: which files conflicted, what each side intended, how you resolved it, and why that resolution is correct.]

---

## Test Results

```
[Paste test suite output summary]
```

- **Total tests**: [N]
- **Passed**: [N]
- **Failed**: [N]
- **Skipped**: [N]

### Failed Tests

> Skip this section if all tests passed.

| Test | Error | Likely Cause | Phase |
|------|-------|-------------|-------|
| [test name] | [error summary] | [analysis] | Phase [N] |

---

## Deployment Results

> Filled in by `spark-deploy-verify` after code review passes. Skip this section if
> deployment is not enabled for this checkpoint. While review is pending or in progress,
> this section stays blank or marked `pending deploy-verify`.

- **Deployed**: Yes / No
- **Host**: [hostname]
- **Commit**: [short hash]
- **Service Status**: Running / Failed
- **Restart Method**: [systemd/custom/none]

### Log Observations

[Paste relevant log excerpts after deployment, or "No errors observed"]

---

## Verification Results

> Verify each phase's completion criteria from the execution plan.

| Phase | Criterion | Status | Notes |
|-------|----------|--------|-------|
| [N] | [Criterion from phase plan] | Pass / Fail | [details if failed] |

### Verification Details

[For any failed verifications, describe what was expected vs. what was observed.]

---

## Smoke Probe

> Skip this section -- smoke harness is disabled for this feature.

---

## Helper Repairs

> Skip this section if no helpers needed repair this checkpoint and no phase summary
> reported a Helper Issue.

### Helpers Repaired

| Helper | Reported by | Failure observed | Repair type | Commit |
|--------|-------------|------------------|-------------|--------|
| `scripts/spark-<name>.sh` | Phase [N] / Self (deploy/smoke) | [the FAIL line that was printed] | minimal patch / regenerated | [short hash] |

### Repair Notes

[For each repaired helper, one or two sentences: what the root cause turned out to be, why the chosen repair (patch vs regeneration) was right, and whether the underlying problem is likely to recur for other helpers.]

### Unlisted Helper Suggestions

> From phase summaries' "Helper Issues -> Unlisted helpers attempted" subsection.

| Phase | What was needed | Suggested helper |
|-------|-----------------|------------------|
| [N] | [what the worker did manually] | [proposed name + one-line purpose] |

---

## Code Review Results

> Skip this section if no code review was conducted for this checkpoint.

- **Reviewer**: [Code review agent model and ID]
- **Diff reviewed**: `{pre-batch-commit}..{post-merge-commit}`
- **Result**: [PASSED | PASSED WITH NOTES | FAILED]

### Issues Found

| Severity | File | Description | Resolution |
|----------|------|-------------|------------|
| [Critical/Important/Minor] | [file path] | [description] | [Fixed / Noted / N/A] |

### Review Notes

[Any additional observations from the code review agent.]

---

## Fix Cycle History

> Skip this section if no fixes were needed.

| Attempt | Type | Target | Description | Result |
|---------|------|--------|-------------|--------|
| 1 | inline | [file/function] | [what was tried] | Success / Failed |
| 2 | inline | [file/function] | [what was tried] | Success / Failed |
| 3 | inline | [file/function] | [what was tried] | Success / Failed |
| 4 | dedicated phase | [scope] | [what was tried] | Success / Failed / Escalated |

### Fix Details

[Describe the root cause of each failure, what each fix attempt tried, and the final outcome.]

---

## Codebase Context Updates

> Consolidate CODEBASE_CONTEXT.md updates from all phase summaries in this batch.

### Added

- [New files, APIs, patterns, data models added by this batch's phases]

### Modified

- [Existing entries that changed due to this batch's work]

### Removed

- [Entries that no longer exist after this batch's work]

---

## Notes for Next Batch

[Observations, warnings, or context that the next batch's coding agents should know.]

---

## Status After Checkpoint

- **All phases in batch**: [PASSED / FAILED WITH FIXES / ESCALATED]
- **Cumulative project progress**: [X]% ([N]/[M] phases complete)
- **Ready for next batch**: [Yes / No -- if No, explain what must be resolved first]
