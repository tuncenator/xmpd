# Phase [NUMBER]: [PHASE NAME] - Summary

**Date Completed:** YYYY-MM-DD
**Completed By:** [Agent Session ID or identifier if available]
**Actual Token Usage:** ~XXk tokens

---

## Objective

[Copy the objective from your phase plan (phase_plans/PHASE_XX.md)]

---

## Work Completed

### What Was Built

[Describe what was implemented in this phase. Be specific but concise.]

### Files Created

- `path/to/file1.py` - [Brief description]

### Files Modified

- `path/to/existing.py` - [What changed and why]

### Key Design Decisions

[Explain any important choices made during implementation]

---

## Completion Criteria Status

[Copy the completion criteria checklist from your phase plan (phase_plans/PHASE_XX.md) and mark each item. For each checked item, state how it was verified -- what command was run and what the output confirmed.]

- [x] Criterion 1 - Verified: `command` -- result
- [ ] Criterion 2 - NOT completed (explain below)

### Deviations / Incomplete Items

[If any criteria were not met or implementation differs from plan, explain here]

---

## Testing

### Tests Written

[List test files/functions created]

### Test Results

**Paste the actual command and its output from your final test run. Do not summarize or paraphrase.**

```
[Paste actual test command and full output here]
```

### Manual Testing

[Describe any manual testing performed]

---

## Evidence Captured

> One entry per external interface this phase consumed (HTTP response, file format,
> library return shape, DB row, third-party message). Types and mocks in the diff
> must mirror the pasted sample, not a declared contract.

### [Interface name]

- **How captured**: [exact command]
- **Captured on**: YYYY-MM-DD against [environment]
- **Consumed by**: [file:line(s)]
- **Sample**:

  ```
  [Paste the actual response/sample byte-for-byte.]
  ```

### Interfaces Not Observed

- **[Interface name]**: could not observe because [reason]. Types/mocks were written from [documentation source].

---

## Helper Issues

> Skip this section entirely if every helper you invoked succeeded or no helpers were used.

---

## Live Verification Results

### Verifications Performed

[List what was verified during development, not just at the end]

### External API Interactions

[Document any external API calls made during development for verification]

---

## Challenges & Solutions

### Challenge 1: [Brief description]
**Solution:** [How it was resolved]

---

## Code Quality

### Formatting
- [ ] Code formatted per project conventions
- [ ] Imports/dependencies organized
- [ ] No unused imports or dependencies

### Linting
```
[Paste linter output if run]
```

---

## Dependencies

### Required by This Phase
[List phases that had to be complete before this one]

### Unblocked Phases
[List phases that can now proceed because this one is complete]

---

## Codebase Context Updates

[List what you added or changed in CODEBASE_CONTEXT.md during this phase]

## Notes for Future Phases

[Any important information, warnings, or suggestions for agents working on future phases]

---

## Next Steps

**Next Phase:** [Number and Name]

---

## Approval

**Phase Status:** COMPLETE

---

*This summary was generated following the PHASE_SUMMARY_TEMPLATE.md structure.*
