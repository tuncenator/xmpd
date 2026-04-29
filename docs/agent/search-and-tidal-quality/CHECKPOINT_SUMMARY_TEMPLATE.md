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

> Skip -- deployment is disabled for this feature.

---

## Verification Results

| Phase | Criterion | Status | Notes |
|-------|----------|--------|-------|
| [N] | [Criterion from phase plan] | Pass / Fail | [details if failed] |

---

## Smoke Probe

> Skip -- smoke harness is disabled for this feature.

---

## Helper Repairs

> Skip this section if no helpers needed repair.

---

## Code Review Results

- **Reviewer**: [Code review agent model and ID]
- **Diff reviewed**: `{pre-batch-commit}..{post-merge-commit}`
- **Result**: [PASSED | PASSED WITH NOTES | FAILED]

### Issues Found

| Severity | File | Description | Resolution |
|----------|------|-------------|------------|
| [Critical/Important/Minor] | [file path] | [description] | [Fixed / Noted / N/A] |

---

## Fix Cycle History

> Skip this section if no fixes were needed.

---

## Codebase Context Updates

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
- **Cumulative project progress**: [X]% ([N]/4 phases complete)
- **Ready for next batch**: [Yes / No]
