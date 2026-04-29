# AI Agent Quickstart Guide

**Welcome, AI Agent!** This guide will help you navigate and complete your assigned phase efficiently.

---

## Location & Paths

**CRITICAL: Verify your location before starting!**

```bash
pwd  # Should output: /home/tunc/Sync/Programs/xmpd
```

### Project Paths

- **Project Root**: `/home/tunc/Sync/Programs/xmpd`
- **Feature Docs**: `/home/tunc/Sync/Programs/xmpd/docs/agent/search-redesign-and-enhancements`

### Path Usage Rules

1. **Stay in project root** - Do NOT `cd` to other directories
2. **All paths are relative to project root** - When you see `docs/agent/...`, it means `/home/tunc/Sync/Programs/xmpd/docs/agent/...`
3. **If confused about location** - Run `pwd` to verify you're in `/home/tunc/Sync/Programs/xmpd`
4. **Use relative paths in your work** - Reference files as `docs/agent/...` not absolute paths

---

## Your Mission

You are part of a phased development workflow. Your job is to:
1. **Verify your location** (run `pwd` -- should be `/home/tunc/Sync/Programs/xmpd`)
2. Identify which phase you're responsible for
3. Gather minimal necessary context
4. Complete your phase according to the plan -- building, verifying, and committing as you go
5. Document your work
6. Update the status for the next agent

---

## File Structure

```
project-root/  <- /home/tunc/Sync/Programs/xmpd (where pwd outputs)
+-- docs/
|   +-- agent/
|       +-- search-redesign-and-enhancements/
|           +-- QUICKSTART.md              <- You are here
|           +-- PROJECT_PLAN.md            <- Project overview, architecture, cross-cutting
|           +-- STATUS.md                  <- Phase tracker + integrations + deploy config
|           +-- CODEBASE_CONTEXT.md        <- Cumulative codebase knowledge
|           +-- PHASE_SUMMARY_TEMPLATE.md  <- Summary template
|           +-- phase_plans/               <- Individual phase plans
|           |   +-- PHASE_01.md
|           |   +-- PHASE_02.md
|           |   +-- PHASE_03.md
|           +-- summaries/                 <- Completed phase summaries
```

**All paths in this guide are relative to `/home/tunc/Sync/Programs/xmpd`**

---

## Your Workflow

### Step 1: Find Your Phase

Read `docs/agent/search-redesign-and-enhancements/STATUS.md` to identify:
- Which phase is current (marked as CURRENT)
- Your phase number and name
- Integration settings (Git, Jira, Deployment, Safety Posture)

### Step 2: Get Context

**2a. Read the codebase context** (always, before anything else):
- Read `docs/agent/search-redesign-and-enhancements/CODEBASE_CONTEXT.md`

**2b. Read recent phase summaries** (up to 2 most recent):
- If you're on Phase 3, read `PHASE_02_SUMMARY.md` and `PHASE_01_SUMMARY.md`
- If you're on Phase 1, skip (no previous summaries)

**Location**: `docs/agent/search-redesign-and-enhancements/summaries/`

### Step 3: Read Your Phase Plan

Open `docs/agent/search-redesign-and-enhancements/phase_plans/PHASE_XX.md` where XX is your phase number (zero-padded: 01, 02, 03).

**Do NOT read all phase plan files** -- only read yours.

### Step 4: Build, Verify, Commit (Repeat)

#### 4a. Code a Logical Chunk

Implement a coherent piece of functionality. Keep chunks small enough to verify independently.

#### 4b. Verify Locally

1. **Identify** the command that proves the claim
2. **Run** it fresh
3. **Read** the full output and check the exit code
4. **Confirm** the output actually proves what you claim

Never claim something works without running the verification command.

#### When Verification Fails

1. **Investigate**: Read the full error output. Trace the failure to its origin.
2. **Compare**: Find working code in the same codebase that does something similar.
3. **Hypothesize**: Form one specific theory about the root cause.
4. **Fix**: Apply a single targeted change. Re-run verification.

#### 4c. Commit

**Format**: `[Phase {N}/3] {verb}: {what changed}`

**Verbs** (lowercase): `add`, `fix`, `update`, `refactor`, `remove`, `docs`

#### 4d. Repeat

Continue the cycle until all deliverables for your phase are complete.

### Step 5: Document Your Work

**5a. Update the codebase context**: Edit `docs/agent/search-redesign-and-enhancements/CODEBASE_CONTEXT.md`

**5b. Create your phase summary**: Template at `docs/agent/search-redesign-and-enhancements/PHASE_SUMMARY_TEMPLATE.md`, output to `docs/agent/search-redesign-and-enhancements/summaries/PHASE_XX_SUMMARY.md`

### Step 6: Update Status

Edit `docs/agent/search-redesign-and-enhancements/STATUS.md`.

### Step 7: Final Commit and Integration Updates

1. Stage all doc changes
2. Commit: `[Phase {N}/3] docs: phase summary and context updates`
3. Push: `git push origin feature/search-redesign-and-enhancements`

### Step 8: Stop

Your work is complete! The next agent will handle the next phase.

---

## Environment Setup

### First-Time Setup

```bash
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

### Before Each Session

```bash
source .venv/bin/activate
```

### Common Commands

```bash
# Run tests
uv run pytest tests/ -v

# Run specific test file
uv run pytest tests/test_specific.py -v

# Lint
uv run ruff check xmpd/

# Type check
uv run mypy xmpd/

# Run daemon (dev mode -- stop systemd service first!)
# systemctl --user stop xmpd && python -m xmpd

# Check running service
systemctl --user status xmpd
```

---

## Development Discipline

### Test-First Development

For every behavior you implement: write a failing test first, watch it fail, then write the minimal code to pass it. This is non-negotiable.

1. **RED**: Write test describing expected behavior. Run it. Confirm it fails because the feature is missing (not because of a typo or import error).
2. **GREEN**: Write the simplest code that passes. No extras.
3. **REFACTOR**: Clean up while tests stay green.

Test command: `uv run pytest tests/ -v`
Run after every implementation chunk. If tests fail after your change, debug systematically (see Workflow step 4b) before attempting fixes.

### Verification Honesty

Before claiming any task is done, run the verification command and read the output. "Should work" is not evidence. "Tests likely pass" is not evidence. Run it, read it, report what it actually says.

### Debugging Protocol

When something fails:
1. Read the FULL error (don't skim)
2. Trace backward to the source of the bad value
3. Form ONE hypothesis, test minimally
4. If 3 hypotheses fail: this is architectural, not a bug. Document and escalate.

---

## Project Helpers

No helpers configured for this feature.

---

## Live Verification

**Verify as you build, not just at the end.**

This project uses live verification: every logical chunk of code should be tested against reality before moving on.

### Safety Posture

This project uses CAUTIOUS safety posture. Before performing any write operation to external systems, databases, or services -- even locally -- ASK the user for permission and explain why the operation could be risky. Read-only operations (GET requests, SELECT queries, log reading, running tests) can be performed freely without asking.

### Runtime Context

- **How this runs**: systemd user service (`xmpd.service`)
- **Restart after changes**: `systemctl --user restart xmpd`
- **Verify it works**: `systemctl --user is-active xmpd && bin/xmpctl status`
- **Never do this**: Don't spawn `python -m xmpd` directly or mock the MPD client for live verification. Use the systemd service.

Always verify against the running system described above. Unit tests are necessary but NOT sufficient to claim a feature works. If you cannot test end-to-end, state that explicitly in your phase summary rather than substituting unit tests.

### What to Verify

- **Functions**: Call them with sample inputs, check outputs match expectations
- **CLI commands**: Run them, check output and exit codes
- **External API calls**: Make safe read-only calls first to verify connectivity and response format
- **Logs**: After every run, check that logs show expected output and no errors

### Write Operation Safety

1. **Prefer safe patterns**: Create a dummy/test resource, verify it exists, then clean it up
2. **Verify before touching**: Check that the target does not contain important data
3. **Never touch production data**: Even in relaxed mode, do not modify data that appears to be real/production
4. **If no safe method exists**: Discuss with the user

### Verify Before Coding

If your phase involves interacting with an external API or service:
1. Check CODEBASE_CONTEXT.md first
2. If needed, make a safe read-only call to verify connectivity and current response format
3. THEN write your integration code based on actual observed behavior

---

## Context Budget

You have approximately **120k tokens** total (input + output + thinking).

TDD discipline (test-first for every behavior) uses ~30% more tokens than implementation-only. This is budgeted into phase sizing. Don't skip tests to save context.

**Be strategic**:
- Read only what you need
- Follow the workflow above exactly
- Keep summaries concise

---

## Important Notes

### Security -- No Credentials in Repository

**CRITICAL: Never store passwords, API keys, tokens, connection strings, or any secrets in repository files.**

### Secret Tagging in Documentation

When referencing infrastructure-specific values in agent docs, use inline tags: `[LABEL]`. The pre-commit hook redacts these to `[LABEL]` before commit.

### Logging

**Always check logs.** After running code, deploying, or restarting a service:
1. Check application logs for errors, warnings, or unexpected behavior
2. If logs show issues, fix them before proceeding
3. Include relevant log observations in your phase summary

### Phase Boundaries

**Respect phase boundaries.** Do not work on multiple phases at once.

---

## Quick Checklist

Before you begin:
- [ ] **FIRST: Run `pwd` and verify you're in `/home/tunc/Sync/Programs/xmpd`**
- [ ] Read `docs/agent/search-redesign-and-enhancements/STATUS.md`
- [ ] Read `docs/agent/search-redesign-and-enhancements/CODEBASE_CONTEXT.md`
- [ ] Read recent phase summaries
- [ ] Read your phase plan from `docs/agent/search-redesign-and-enhancements/phase_plans/PHASE_XX.md`

During your work:
- [ ] Stay within your phase boundaries
- [ ] Activate environment before running commands
- [ ] Build incrementally -- verify each chunk before moving on
- [ ] Check logs after running or deploying code
- [ ] Commit after each verified chunk

After completion:
- [ ] Update `docs/agent/search-redesign-and-enhancements/CODEBASE_CONTEXT.md`
- [ ] Create phase summary
- [ ] Update `docs/agent/search-redesign-and-enhancements/STATUS.md`
- [ ] Final commit for docs, push

---

*This quickstart is designed for AI agents working in a phased development workflow.*
