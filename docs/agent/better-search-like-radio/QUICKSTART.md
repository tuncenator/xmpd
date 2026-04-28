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
- **Feature Docs**: `/home/tunc/Sync/Programs/xmpd/docs/agent/better-search-like-radio`

### Path Usage Rules

1. **Stay in project root** - Do NOT `cd` to other directories
2. **All paths are relative to project root** - When you see `docs/agent/...`, it means `/home/tunc/Sync/Programs/xmpd/docs/agent/...`
3. **If confused about location** - Run `pwd` to verify you're in `/home/tunc/Sync/Programs/xmpd`
4. **Use relative paths in your work** - Reference files as `docs/agent/...` not absolute paths

**Example Path Reference:**
```
Relative path: docs/agent/better-search-like-radio/STATUS.md
Absolute path: /home/tunc/Sync/Programs/xmpd/docs/agent/better-search-like-radio/STATUS.md
                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                Where pwd should output
```

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
|       +-- better-search-like-radio/   <- Your feature folder
|           +-- QUICKSTART.md              <- You are here
|           +-- PROJECT_PLAN.md            <- Project overview, architecture, cross-cutting
|           +-- STATUS.md                  <- Phase tracker + integrations + deploy config
|           +-- CODEBASE_CONTEXT.md        <- Cumulative codebase knowledge
|           +-- PHASE_SUMMARY_TEMPLATE.md  <- Summary template
|           +-- phase_plans/               <- Individual phase plans
|           |   +-- PHASE_01.md
|           |   +-- PHASE_02.md
|           |   +-- ...
|           +-- summaries/                 <- Completed phase summaries
|               +-- PHASE_01_SUMMARY.md
|               +-- PHASE_02_SUMMARY.md
|               +-- ...
```

**All paths in this guide are relative to `/home/tunc/Sync/Programs/xmpd`**

---

## Your Workflow

### Step 1: Find Your Phase

Read `docs/agent/better-search-like-radio/STATUS.md` to identify:
- Which phase is current (marked as CURRENT)
- Your phase number and name
- Integration settings (Git, Jira, Deployment, Safety Posture)

### Step 2: Get Context

**2a. Read the codebase context** (always, before anything else):
- Read `docs/agent/better-search-like-radio/CODEBASE_CONTEXT.md`
- This contains cumulative knowledge about the codebase from all previous phases
- Use this instead of re-exploring the codebase from scratch
- Only explore further if you need information not covered in this document

**2b. Read recent phase summaries** (up to 2 most recent):
- If you're on Phase 5, read `PHASE_04_SUMMARY.md` and `PHASE_03_SUMMARY.md`
- If you're on Phase 1 or 2, read what's available (or nothing if Phase 1)

**Location**: `docs/agent/better-search-like-radio/summaries/`

### Step 3: Read Your Phase Plan

Open `docs/agent/better-search-like-radio/phase_plans/PHASE_XX.md` where XX is your phase number (zero-padded: 01, 02, ..., 10, 11, ...).

This file contains everything you need for your phase:
- Objective and deliverables
- Detailed requirements
- Dependencies and completion criteria
- Testing requirements

If you also need the big picture (architecture, cross-cutting concerns), read the relevant sections of `docs/agent/better-search-like-radio/PROJECT_PLAN.md` -- but only as needed.

**Do NOT read all phase plan files** -- only read yours.

### Step 4: Build, Verify, Commit (Repeat)

Follow this cycle for each logical chunk of work in your phase. Do NOT code everything and test at the end -- build incrementally and verify as you go.

#### 4a. Code a Logical Chunk

Implement a coherent piece of functionality (a function, an endpoint, a module). Keep chunks small enough to verify independently.

#### 4b. Verify Locally

For every claim you make about your code ("tests pass", "endpoint returns 200", "function outputs X"), follow this verification gate:

1. **Identify** the command that proves the claim
2. **Run** it fresh -- not from a previous run, not from memory
3. **Read** the full output and check the exit code
4. **Confirm** the output actually proves what you claim

Never claim something works without running the verification command in this session and reading its output. Apply the gate to:

- **Tests**: Run them, read the output, paste the actual results in your summary
- **Live verification**: Actually run the code (see the Live Verification section below for details and safety rules)
  - Built a function? Call it with sample data and check the output
  - Built an endpoint? Hit it with curl and verify the response
  - Built a CLI command? Run it and check the output
  - Interacting with an external API? Make a safe read-only call first to verify connectivity and response format before writing integration code based on assumptions
- **Logs**: Verify your code produces appropriate log output. If something looks wrong, fix it before moving on

If something is wrong, fix it before continuing -- but follow the debugging protocol below. Do not guess-and-check.

#### When Verification Fails

When a test fails or code doesn't behave as expected, follow this protocol before attempting any fix:

1. **Investigate**: Read the full error output. Don't skim. Trace the failure to its origin -- which file, which line, which value was wrong.
2. **Compare**: Find working code in the same codebase that does something similar. Compare it against your failing code. The difference is usually the bug.
3. **Hypothesize**: Form one specific theory about the root cause. Test it minimally (add a log statement, check an intermediate value) before committing to a fix.
4. **Fix**: Apply a single targeted change. Re-run verification. If it fails again, return to step 1 with the new information -- do not retry the same fix.

Do not make multiple changes at once. One hypothesis, one fix, one verification cycle.

#### 4c. Commit

Stage the changes for this chunk and commit with a descriptive message.

**Format**: `[Phase {N}/5] {verb}: {what changed}`

**Verbs** (lowercase): `add`, `fix`, `update`, `refactor`, `remove`, `docs`

**Examples**:
- `[Phase 1/5] fix: connection counter leak on concurrent DASH cancellation`
- `[Phase 2/5] add: JSON search output with quality metadata`
- `[Phase 3/5] add: fzf-based interactive search with provider colors`

Get {N} from STATUS.md (e.g., "Current Phase: 3 of 5"). Multiple commits per phase is expected and encouraged.

#### 4d. Repeat

Continue the cycle (4a-4c) until all deliverables for your phase are complete.

### Step 5: Document Your Work

**5a. Update the codebase context**:
- Edit `docs/agent/better-search-like-radio/CODEBASE_CONTEXT.md`
- Update the "Last updated by" line at the top to reflect your phase name and today's date
- Add any new files you created (to "Key Files & Modules")
- Add any new APIs, classes, or interfaces you built (to "Important APIs & Interfaces")
- Add any new data models (to "Data Models")
- Update any entries that changed due to your work (renamed files, modified APIs, etc.)
- Remove entries for things that no longer exist
- Keep updates incremental -- do not rewrite sections that are still accurate

**5b. Create your phase summary**:
- **Template**: `docs/agent/better-search-like-radio/PHASE_SUMMARY_TEMPLATE.md`
- **Output location**: `docs/agent/better-search-like-radio/summaries/PHASE_XX_SUMMARY.md`
- **Length**: Keep it concise (~400-500 lines max)

Include:
- What you built
- Files created/modified
- Completion criteria status
- Any challenges or deviations
- Notes for future phases
- **Live Verification Results**: what you verified during development and how
- List of all commits made during this phase

### Step 6: Update Status

Edit `docs/agent/better-search-like-radio/STATUS.md`:
1. Mark your phase as Complete
2. Update "Current Phase" to next phase number
3. Update "Phase Name" to next phase name
4. Update "Last Updated" to today's date (YYYY-MM-DD format)

### Step 7: Final Commit and Integration Updates

**Git**: Your code commits are already pushed from Step 4c. Now do a final commit for documentation:
1. Stage all doc changes (summary, STATUS.md, CODEBASE_CONTEXT.md)
2. Commit: `[Phase {N}/5] docs: phase summary and context updates`
3. Push: `git push origin refactor/better-search-like-radio`

### Step 8: Stop

Your work is complete! The next agent will handle the next phase.

---

## Environment Setup

**Tech stack**: Python 3.11, managed with `uv`

**First-time setup** (should already be done):
```bash
uv sync
```

**Common commands**:
```bash
# Run tests
uv run pytest tests/ -q

# Run specific test file
uv run pytest tests/test_stream_proxy.py -q

# Run with verbose output
uv run pytest tests/test_stream_proxy.py -v

# Type checking
uv run mypy xmpd/

# Linting
uv run ruff check xmpd/ bin/ tests/

# Run the daemon (for manual testing)
uv run xmpd

# Use the CLI client
./bin/xmpctl search "radiohead"
./bin/xmpctl like
./bin/xmpctl radio --apply
```

**Live Tidal tests** (requires active session):
```bash
XMPD_TIDAL_TEST=1 uv run pytest tests/ -q
```

---

## Project Helpers

No helpers configured for this feature.

---

## Live Verification

**Verify as you build, not just at the end.**

This project uses live verification: every logical chunk of code should be tested against reality before moving on. Do not wait until all deliverables are complete to run the program for the first time.

### Safety Posture

This project uses RELAXED safety posture. This is a dev/sandbox environment where data loss is acceptable. You can freely test writes using safe patterns (create dummy data, verify, clean up) without asking. Still avoid anything clearly targeting production data. If the target or nature of a test changes significantly from what you have been doing, ask the user before proceeding.

**Specific constraint**: Do not delete songs from the user's playlists.

### What to Verify

- **Functions**: Call them with sample inputs, check outputs match expectations
- **Endpoints**: Hit them with curl, check responses and status codes
- **Search**: Actually run a search query and verify results display correctly
- **Playback**: If your phase touches playback, actually play a song and confirm it streams
- **fzf interface**: If your phase builds the search TUI, launch it and interact with it
- **Logs**: After every run, check that logs show expected output and no errors

### Write Operation Safety

When you need to test write operations (queue additions, playlist modifications, like/unlike):

1. **Prefer safe patterns**: Queue a song, verify it's there, skip to next
2. **Never delete playlist content**: The user explicitly prohibits this
3. **Like/unlike is safe**: toggling like state is reversible and acceptable for testing

### Verify Before Coding

If your phase involves interacting with an external API or service:
1. Check CODEBASE_CONTEXT.md first -- the "External Services & APIs" section may have research findings from setup
2. If needed, make a safe read-only call to verify connectivity and current response format
3. THEN write your integration code based on actual observed behavior, not assumptions
4. Do NOT code an entire API client based on training data and then discover the API has changed

---

## Context Budget

You have approximately **120k tokens** total (input + output + thinking).

**Be strategic**:
- Read only what you need
- Follow the workflow above exactly
- Keep summaries concise
- Don't read entire files when you need one function
- Don't read all phase plans when you need one phase
- Don't explore unrelated code

Each phase is designed to fit within one agent session. If you run out of context:
- Note this in your summary
- Document what's incomplete
- Suggest splitting the phase

---

## Important Notes

### Security -- No Credentials in Repository

**CRITICAL: Never store passwords, API keys, tokens, connection strings, or any secrets in repository files.**

- Do NOT hardcode credentials in source code, config files, documentation, or agent framework files
- If a phase requires credentials (database access, API keys, service tokens, etc.):
  1. Check your Claude memories -- credentials may already be stored there from a previous phase or session
  2. If not in memory, ask the user to provide them
  3. Save credentials to Claude memory so future agents can access them without the repo
  4. In code, reference credentials via environment variables or .env files (which must be gitignored)
- A pre-commit hook is active on this repository to catch accidental credential leaks and to redact `[LABEL]` markers in agent docs

#### Pre-commit hook block: bypass procedure

Most blocks are real. The hook redacts `[LABEL]` markers and matches secret-shaped patterns. False positives happen but are not the common case.

**Do NOT bypass with `git commit --no-verify` if any of these are true:**

- The blocked file is under `src/`, `lib/`, `config/`, `infra/`, `app/`, `services/`, `packages/`, or any path matching `**/secrets/**`
- The blocked file matches `.env*` (any dotenv variant)
- The matched value looks like a real token (AWS key, GitHub token, Slack token, private key, etc.)

If any of the above hold, stop. End your phase with a line naming the blocked file and the pattern (after redaction), so the conductor can surface it to a human. Do NOT bypass.

**Bypass procedure (only when none of the above hold):**

1. Print to your output: path, matched pattern, reason it's not a real secret
2. Add a `Bypass-reason:` trailer to the commit message body
3. Commit with `--no-verify`

### Secret Tagging in Documentation

When referencing infrastructure-specific values in agent doc files, use inline tags: `[LABEL]`. The pre-commit hook redacts these to `[LABEL]` before commit.

### Logging

**Always check logs.** After running code, deploying, or restarting a service:
1. Check application logs for errors, warnings, or unexpected behavior
2. If logs show issues, fix them before proceeding
3. Include relevant log observations in your phase summary

### Phase Boundaries

**Respect phase boundaries.** Do not:
- Work on multiple phases at once
- Skip phases
- Go back and refactor previous phases (unless your phase plan says to)

### Dependencies

If your phase depends on previous phases:
- Check that those phases are marked complete in STATUS.md
- Read their summaries to understand what was built
- Note any blockers in your summary if dependencies are incomplete

### Blockers

If you encounter blockers:
- Document them clearly in your summary
- Mark affected completion criteria as incomplete
- Suggest solutions or next steps
- Do NOT mark your phase as complete if critical items are blocked

---

## Quick Checklist

Before you begin:
- [ ] **FIRST: Run `pwd` and verify you're in `/home/tunc/Sync/Programs/xmpd`**
- [ ] Read `docs/agent/better-search-like-radio/STATUS.md` to identify your phase and check safety posture
- [ ] Read `docs/agent/better-search-like-radio/CODEBASE_CONTEXT.md` for codebase knowledge
- [ ] Read the 2 most recent phase summaries from `docs/agent/better-search-like-radio/summaries/`
- [ ] Read your phase plan from `docs/agent/better-search-like-radio/phase_plans/PHASE_XX.md`
- [ ] Understand your deliverables and completion criteria

During your work:
- [ ] Stay within your phase boundaries
- [ ] Activate environment before running commands (see Environment Setup section)
- [ ] Build incrementally -- verify each chunk before moving on
- [ ] Check logs after running or deploying code
- [ ] Use `[LABEL]` for sensitive values in doc files
- [ ] Commit after each verified chunk (multiple commits per phase is fine)
- [ ] Write tests if required

After completion:
- [ ] Update `docs/agent/better-search-like-radio/CODEBASE_CONTEXT.md` with new discoveries and changes
- [ ] Create phase summary using the template (include live verification sections)
- [ ] Verify all completion criteria are met (or document why not)
- [ ] Update `docs/agent/better-search-like-radio/STATUS.md`
- [ ] Final commit for docs, push if git is enabled (see Step 7)
- [ ] Do NOT start the next phase

---

## Ready to Start?

1. Read `docs/agent/better-search-like-radio/STATUS.md`
2. Follow the workflow above
3. Build, verify, commit -- repeat
4. Document and update status

**Good luck, Agent!**

---

*This quickstart is designed for AI agents working in a phased development workflow. For human developers, see the standard project README.*
