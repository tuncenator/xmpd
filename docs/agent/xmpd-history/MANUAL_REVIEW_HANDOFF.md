# Manual Review Handoff -- xmpd-history

> **Audience: the next-session orchestrator (you, Claude, in a fresh context).**
> This document is self-contained. Read it top-to-bottom before doing anything else. The user's prior session ended after Batch 6 of /spark-conductor; the feature is code-complete and waiting on one or more rounds of human review with spark-fix repair cycles in between. You are NOT /spark-conductor; you are running a smaller dedicated user-review-loop protocol described below.

---

## Current State

- **Project root**: `/home/tunc/Sync/Programs/xmpd`
- **Feature branch**: `feature/xmpd-history`
- **Branch HEAD** at handoff write time: `8fd9ffe` (commit `[Checkpoint 6/6] update: code review results`). The actual current HEAD when you arrive may be later if the user committed something between sessions; verify with `git rev-parse HEAD` and `git log --oneline -5`.
- **Pushed to origin?**: NO. The user wants a manual review round (or rounds) + any resulting spark-fix repairs BEFORE the final push.
- **Diff base for the full review**: `git merge-base main feature/xmpd-history`. Use this only for the first round; subsequent rounds review only the new fix commits since the previous round.

The feature is the xmpd-history multi-host listening-history system:

- Local SQLite play history (`xmpd/history_store.py`)
- HistoryReporter wire-up (`xmpd/history_reporter.py`)
- Bidirectional sync to WATCHTOWER aggregator (`xmpd/history_syncer.py`)
- WATCHTOWER receiver script (`scripts/xmpd-history-receiver`, already scp'd to `WATCHTOWER:~/bin/`)
- fzf browser (`bin/xmpd-history`)
- Backfill from MPD log (`xmpd/history_backfill.py` + daemon IPC + `xmpctl history-backfill`)
- Healthcheck (`bin/xmpd-doctor`)
- xmpctl IPC subcommand (`history-json` for the read path)
- Daemon wiring in `xmpd/daemon.py`
- Live integration tests run against STORMTREE + WATCHTOWER under Phase 8

All 8 phases passed automated `spark-code-reviewer` review at every batch checkpoint (PASSED or PASSED WITH NOTES; minor notes only). Phase 8 surfaced 2 real bugs in live runs which were surgically fixed with regression tests (Loop A `-F` ssh config issue in `history_syncer.py`; Loop C silent-exit + `--tabstop` rename in `bin/xmpd-history`; Loop E `-F` issue in `bin/xmpd-doctor`).

For the full story, in order:

- `docs/agent/xmpd-history/PROJECT_PLAN.md` -- feature overview, scope, architecture, data schemas
- `docs/agent/xmpd-history/CODEBASE_CONTEXT.md` -- living-doc map of every module/file the feature touches
- `docs/agent/xmpd-history/EXECUTION_PLAN.md` -- the 6-batch schedule and what each batch did
- `docs/agent/xmpd-history/summaries/PHASE_0{1..8}_SUMMARY.md` -- per-phase summaries
- `docs/agent/xmpd-history/summaries/CHECKPOINT_0{1..6}_SUMMARY.md` -- per-batch checkpoint records (test results, code review verdicts, fix cycle history)
- `docs/agent/xmpd-history/INTEGRATION_TEST_REPORT.md` -- the live Phase 8 evidence (5 loops, byte-for-byte stdout)

---

## Your Role

You are the **manual-review-loop orchestrator**. You do NOT spawn `spark-coder-*`, `spark-checkpoint`, `spark-code-reviewer`, or `spark-deploy-verify`. The only subagent you dispatch is `spark-fix` (when the user's review surfaces a fix-worthy finding).

Each round of the loop:

1. **Identify what to review** -- compute the diff range, gather context for the user
2. **Present the diff** -- structured summary + offer to fetch specific files/functions on demand
3. **Collect verdict + findings** -- use AskUserQuestion for the verdict; use natural conversation for finding details
4. **If FAIL: dispatch spark-fix** -- assemble findings into the fix prompt, wait for FIX COMPLETE, append round to the log, GO TO 1 with a new diff range
5. **If PASS: finalize** -- mark the gate cleared, push to origin, post the completion summary

The user is the only reviewer. You don't add automated review on top.

---

## Round Protocol

### Step 1 -- Read prior state

Run in parallel:

```bash
git rev-parse HEAD                 # confirm current HEAD
git log --oneline -10              # see recent commits
git status                         # confirm clean working tree (it should be)
git merge-base main HEAD           # the diff base for the FIRST round
```

Then check if `docs/agent/xmpd-history/MANUAL_REVIEW_LOG.md` exists:

- **If it does NOT exist**: this is Round 1. The diff base is `git merge-base main feature/xmpd-history`. Create the log with the template at the end of this document.
- **If it DOES exist**: read it. Find the most recent round; its `End HEAD` is the base for THIS round.

### Step 2 -- Compute and present the diff

```bash
git diff $BASE..HEAD --stat                                    # files-changed summary
git log --oneline $BASE..HEAD                                  # commits-included summary
```

Display both to the user. Then describe what's there, grouping by surface:

- **Core Python**: `xmpd/history_store.py`, `xmpd/history_syncer.py`, `xmpd/history_reporter.py`, `xmpd/history_backfill.py`, `xmpd/daemon.py`, `xmpd/config.py`, `xmpd/exceptions.py`
- **Receiver (deployed to WATCHTOWER)**: `scripts/xmpd-history-receiver`
- **CLI surfaces**: `bin/xmpctl` (extended), `bin/xmpd-history` (new), `bin/xmpd-doctor` (new), `install.sh` (one-line symlink adds)
- **Tests**: `tests/test_history_*.py`, `tests/test_xmpd_history*.py`, `tests/test_xmpd_doctor.py`, `tests/test_xmpd_history_receiver.py`, `tests/test_xmpctl_history_json.py`, `tests/test_daemon.py` extensions, `tests/conftest.py` fixtures, `tests/fixtures/sample_mpd_log`
- **Docs**: `docs/agent/xmpd-history/INTEGRATION_TEST_REPORT.md` (the Phase 8 deliverable), phase summaries, checkpoint summaries

Offer specific reading paths and tell the user which surface you'd recommend starting with. Sample suggestion to the user: *"Start with `xmpd/history_syncer.py` (Phase 3 hard implementation, NDJSON wire format + single-flight lock) and `scripts/xmpd-history-receiver` (Phase 4 aggregator-side stdlib-only Python). These are the load-bearing pieces."* Adapt to the actual round's diff.

If the round is a re-review (Round 2+), the diff range is much narrower (just the fix commit(s) plus whatever else moved). Highlight the fixed surface explicitly.

### Step 3 -- Collect verdict + findings

Ask the user via `AskUserQuestion`:

- Question: "How does the diff look on this round?"
- Header: "Review N"
- Options:
  - "PASS -- ship it" -- nothing to change; go to finalization
  - "PASS WITH NOTES" -- minor notes to record; go to finalization
  - "FAIL -- I have findings" -- one or more issues; go to fix dispatch
  - "More to read -- ask me again later" -- pause without verdict

If the user picks "More to read", offer to fetch specific files/functions on demand. Re-ask once they're ready.

If FAIL: collect each finding via natural conversation. Press for:

- **Severity**: critical / important / minor
- **Location**: `path/to/file.py:LINE` or `function_name()` or "general"
- **Description**: what's wrong, why
- **Suggested fix direction**: optional but useful

If the user gives vague feedback ("this feels off"), drill in: "Where specifically? File:line? Or a function?" Don't dispatch spark-fix with mush -- the fixer needs concrete coordinates. If after pressing the user still can't pin it down, suggest you read the file with them and refine the finding.

### Step 4 -- Dispatch spark-fix (only on FAIL)

Use the `Agent` tool with `subagent_type: "spark-fix"`. Prompt template:

```markdown
## Context

xmpd is a Python 3.11 daemon. The xmpd-history feature adds local SQLite history + WATCHTOWER bidir sync + fzf browser + healthcheck + backfill. All 8 phases are code-complete and merged onto feature/xmpd-history. The branch is in a USER-driven manual review gate. The user surfaced the following finding(s) in their review.

For full architecture see `docs/agent/xmpd-history/CODEBASE_CONTEXT.md`. The integration test evidence is in `docs/agent/xmpd-history/INTEGRATION_TEST_REPORT.md`.

## Checkpoint

**Checkpoint**: Manual Review Round N (this round number)
**Feature**: xmpd-history
**Stage that failed**: manual-review

## Problem Description

[Verbatim findings from the user. One block per finding with severity / location / description / suggested fix direction.]

## What Was Already Tried

No prior fix attempts for this manual-review issue set -- these are direct user findings from a manual diff review.

## Instructions

Apply the minimal fix for each finding. Add a regression test ONLY if the user's finding is a runtime behavior bug (not a style/naming/comment issue). Follow the test-first protocol for runtime bugs: write the failing test, watch it fail, make the fix, watch it pass. For style/naming/comment issues, skip the test.

Each fix commit: `[Manual Review N] fix: <module>: <one-line description>`. If multiple findings fit in one commit (same file, related), bundle them; otherwise one commit per finding.

After ALL findings are addressed:

1. Run `uv run pytest -xvs` -- no new failures beyond the 14 pre-existing baseline (4 test_xmpd_status_integration, 3 test_like_toggle, 4 test_search_json, 3 test_xmpd_status).
2. Run `uv run ruff check . && uv run ruff format --check .` -- clean on all touched files.
3. Run `uv run mypy xmpd/` -- no new errors (49 pre-existing baseline).
4. Commit per the format above.
5. Do NOT push. Do NOT update STATUS.md or any docs/agent/* file except as directly required by a finding.
6. End with `FIX COMPLETE` or `FIX FAILED: <reason>`.
```

Wait for completion. Read the agent's return summary. If FIX COMPLETE: proceed to Step 5. If FIX FAILED: surface the failure to the user via `AskUserQuestion` (retry / accept-as-is / abort).

### Step 5 -- Log and loop or finalize

After the round closes (PASS, PASS_WITH_NOTES, or FAIL+FIX_COMPLETE):

1. Read the current HEAD.
2. Append an entry to `MANUAL_REVIEW_LOG.md` using the template below.
3. Commit the log update: `[Manual Review N] log: round N verdict and notes`.
4. **If FAIL+FIX_COMPLETE**: go back to Step 1 for Round N+1 (diff base = the End HEAD of Round N, i.e. the just-recorded HEAD).
5. **If PASS or PASS_WITH_NOTES**: go to Finalization (Step 6).

### Step 6 -- Finalization (only when PASS)

```bash
git status                                                     # must be clean
git push origin feature/xmpd-history                           # the deferred push
```

Update STATUS.md: remove the `## ACTION REQUIRED -- MANUAL REVIEW GATE` section (it's at the top of the file, just below "## Project Location"). Add a one-line entry in the `## Notes` section: `2026-MM-DD: manual review complete after N round(s); branch pushed to origin.` Commit the STATUS update: `[Manual Review] complete: user PASS after N round(s)`. Push.

Post the completion summary to the user:

```
================================================================
  FEATURE COMPLETE: xmpd-history
================================================================

All 8 phases done under /spark-conductor.
Manual review passed after N round(s).
Branch: feature/xmpd-history pushed to origin.

Review log:         docs/agent/xmpd-history/MANUAL_REVIEW_LOG.md
Integration report: docs/agent/xmpd-history/INTEGRATION_TEST_REPORT.md
Project status:     docs/agent/xmpd-history/STATUS.md
================================================================
```

Stop. Do not offer further actions unless the user asks.

---

## MANUAL_REVIEW_LOG.md template

If the log doesn't exist when you start Round 1, create it with this exact top-of-file:

```markdown
# Manual Review Log -- xmpd-history

> Tracks the user-driven review rounds that gate the final push to origin. Each round records the diff base, verdict, findings (if any), the fix commit(s) that resulted, and the new HEAD. Maintained by the manual-review-loop orchestrator per `MANUAL_REVIEW_HANDOFF.md`.

---
```

Append one section per round:

```markdown
## Round N

- **Date**: YYYY-MM-DD
- **Start HEAD**: <full sha>
- **Diff base**: <full sha or "merge-base main" with sha>
- **Files in scope**: <comma-separated list or "see git diff --stat output below">
- **Verdict**: PASS / PASS_WITH_NOTES / FAIL_WITH_FINDINGS
- **Findings** (omit on PASS):
  - **Finding 1**: <severity> -- <location> -- <description>. Suggested: <fix direction>.
  - **Finding 2**: ...
- **Notes** (PASS_WITH_NOTES only):
  - <note>
- **Fix commit(s)** (FAIL only):
  - <sha> <one-line commit message>
- **End HEAD**: <full sha>
- **Duration**: <human-readable, e.g. "12 minutes" or "spanning two sessions">

---
```

---

## Boundaries

- The user is the only reviewer. You do NOT dispatch `spark-code-reviewer` or any auto-reviewer.
- You do NOT spawn coder agents. The feature is implementation-complete. Only `spark-fix` is in scope.
- You do NOT modify production code yourself. spark-fix does the fixing.
- You do NOT touch files outside the feature scope, except STATUS.md (banner removal at finalization) and MANUAL_REVIEW_LOG.md (round entries).
- If the user surfaces a finding that requires substantial new work (a new feature, a refactor of unrelated code, a redesign), DO NOT silently expand scope. Surface the scope concern back to the user: "this looks like it's beyond a review fix; do you want to scope a new task or accept-as-is?".
- ARCHON safety still applies: never spawn `python -m xmpd` on ARCHON, never write `~/.config/xmpd/history.db` on ARCHON outside the daemon, never restart xmpd on ARCHON. STORMTREE/VICAR are free for live verification with `scripts/spark-restart-peer.sh`.
- WATCHTOWER aggregator is read-only by default (no DELETE / DROP).
- Don't push to origin until the user PASSes.

---

## Quick Reference

| Thing | Where |
|------|------|
| Architecture overview | `docs/agent/xmpd-history/PROJECT_PLAN.md` |
| Living module map | `docs/agent/xmpd-history/CODEBASE_CONTEXT.md` |
| Per-phase summaries | `docs/agent/xmpd-history/summaries/PHASE_0{1..8}_SUMMARY.md` |
| Per-batch checkpoint records | `docs/agent/xmpd-history/summaries/CHECKPOINT_0{1..6}_SUMMARY.md` |
| Live integration evidence | `docs/agent/xmpd-history/INTEGRATION_TEST_REPORT.md` |
| This handoff doc | `docs/agent/xmpd-history/MANUAL_REVIEW_HANDOFF.md` |
| Round-by-round log | `docs/agent/xmpd-history/MANUAL_REVIEW_LOG.md` (you create it on Round 1) |
| Status (with the gate banner) | `docs/agent/xmpd-history/STATUS.md` |
| Pre-commit hook redaction tags | `&lt;{LABEL:value}&gt;` form for hostnames in agent docs (see existing examples) |

When in doubt, ask the user. When the user is in doubt, suggest reading a specific file and re-asking. Don't push until they say PASS.
