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
- **Feature Docs**: `/home/tunc/Sync/Programs/xmpd/docs/agent/search-and-tidal-quality`

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
|       +-- search-and-tidal-quality/   <- Your feature folder
|           +-- QUICKSTART.md              <- You are here
|           +-- PROJECT_PLAN.md            <- Project overview, architecture, cross-cutting
|           +-- STATUS.md                  <- Phase tracker + integrations + deploy config
|           +-- CODEBASE_CONTEXT.md        <- Cumulative codebase knowledge
|           +-- PHASE_SUMMARY_TEMPLATE.md  <- Summary template
|           +-- phase_plans/               <- Individual phase plans
|           |   +-- PHASE_01.md
|           |   +-- PHASE_02.md
|           |   +-- PHASE_03.md
|           |   +-- PHASE_04.md
|           +-- summaries/                 <- Completed phase summaries
```

**All paths in this guide are relative to `/home/tunc/Sync/Programs/xmpd`**

---

## Your Workflow

### Step 1: Find Your Phase

Read `docs/agent/search-and-tidal-quality/STATUS.md` to identify:
- Which phase is current (marked as CURRENT)
- Your phase number and name
- Integration settings (Git, Jira, Deployment, Safety Posture)

### Step 2: Get Context

**2a. Read the codebase context** (always, before anything else):
- Read `docs/agent/search-and-tidal-quality/CODEBASE_CONTEXT.md`
- This contains cumulative knowledge about the codebase from all previous phases
- Use this instead of re-exploring the codebase from scratch
- Only explore further if you need information not covered in this document

**2b. Read recent phase summaries** (up to 2 most recent):
- If you're on Phase 5, read `PHASE_04_SUMMARY.md` and `PHASE_03_SUMMARY.md`
- If you're on Phase 1 or 2, read what's available (or nothing if Phase 1)

**Location**: `docs/agent/search-and-tidal-quality/summaries/`

### Step 3: Read Your Phase Plan

Open `docs/agent/search-and-tidal-quality/phase_plans/PHASE_XX.md` where XX is your phase number (zero-padded: 01, 02, 03, 04).

This file contains everything you need for your phase:
- Objective and deliverables
- Detailed requirements
- Dependencies and completion criteria
- Testing requirements

If you also need the big picture (architecture, cross-cutting concerns), read the relevant sections of `docs/agent/search-and-tidal-quality/PROJECT_PLAN.md` -- but only as needed.

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
- **Live verification**: Actually run the code
  - Built a function? Call it with sample data and check the output
  - Fixed a daemon command? Start the daemon, send the command, check response
  - Fixed a keybinding? Run xmpd-search and test the binding
  - Fixed stream quality? Play a Tidal track and verify the stream quality in logs
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

**Format**: `[Phase {N}/4] {verb}: {what changed}`

**Verbs** (lowercase): `add`, `fix`, `update`, `refactor`, `remove`, `docs`

**Examples**:
- `[Phase 1/4] fix: register tracks in TrackStore before play/queue`
- `[Phase 2/4] remove: dead xmpctl search command and daemon handler`
- `[Phase 3/4] fix: ctrl-r radio targets fzf-selected track`

#### 4d. Repeat

Continue the cycle (4a-4c) until all deliverables for your phase are complete.

### Step 5: Document Your Work

**5a. Update the codebase context**:
- Edit `docs/agent/search-and-tidal-quality/CODEBASE_CONTEXT.md`
- Update the "Last updated by" line at the top to reflect your phase name and today's date
- Add any new files you created (to "Key Files & Modules")
- Add any new APIs, classes, or interfaces you built (to "Important APIs & Interfaces")
- Update any entries that changed due to your work (renamed files, modified APIs, etc.)
- Remove entries for things that no longer exist
- Keep updates incremental -- do not rewrite sections that are still accurate

**5b. Create your phase summary**:
- **Template**: `docs/agent/search-and-tidal-quality/PHASE_SUMMARY_TEMPLATE.md`
- **Output location**: `docs/agent/search-and-tidal-quality/summaries/PHASE_XX_SUMMARY.md`
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

Edit `docs/agent/search-and-tidal-quality/STATUS.md`:
1. Mark your phase as Complete
2. Update "Current Phase" to next phase number
3. Update "Phase Name" to next phase name
4. Update "Last Updated" to today's date (YYYY-MM-DD format)

### Step 7: Final Commit and Integration Updates

**Git**: Your code commits are already pushed from Step 4c. Now do a final commit for documentation:
1. Stage all doc changes (summary, STATUS.md, CODEBASE_CONTEXT.md)
2. Commit: `[Phase {N}/4] docs: phase summary and context updates`
3. Push: `git push origin bugfix/search-and-tidal-quality`

### Step 8: Stop

Your work is complete! The next agent will handle the next phase.

---

## Environment Setup

**CRITICAL: xmpd runs as a systemd --user service.** Do NOT spawn `python -m xmpd` for testing. The running service will conflict (port already in use). Instead, send commands to the already-running daemon via `bin/xmpctl`.

```bash
# Daemon management (systemd user service)
systemctl --user status xmpd       # Check if running
systemctl --user restart xmpd      # Restart after code changes
systemctl --user stop xmpd         # Stop before spawning a test instance (rare)

# Only if you MUST run a standalone instance (stop the service first!):
# systemctl --user stop xmpd && python -m xmpd

# Python environment with uv
source .venv/bin/activate

# Running tests (full suite hangs at collection -- use targeted list)
pytest tests/test_daemon.py tests/test_config.py tests/test_track_store.py tests/test_stream_proxy.py tests/test_providers_tidal.py tests/test_xmpctl.py tests/test_search_json.py -v

# CLI client (talks to the running systemd service)
bin/xmpctl status
bin/xmpctl search-json "query" --format fzf

# Interactive search (fzf)
bin/xmpd-search
```

**MPD port**: 6601 (not default 6600). Use `mpc -p 6601`.

**Check daemon logs:**
```bash
tail -f ~/.config/xmpd/xmpd.log
```

**Test tracks**: Use Pink Floyd tracks from Dark Side of the Moon (2023 Remaster) for manual playback verification. Track ID 283628184 (Breathe) is a known-good Tidal track.

---

## Project Helpers

No helpers configured for this feature.

---

## Live Verification

**Verify as you build, not just at the end.**

This project uses live verification: every logical chunk of code should be tested against reality before moving on. Do not wait until all deliverables are complete to run the program for the first time.

### Safety Posture

This project uses CAUTIOUS safety posture. Before performing any write operation to external systems, databases, or services -- even locally -- ASK the user for permission and explain why the operation could be risky. Read-only operations (GET requests, SELECT queries, log reading, running tests) can be performed freely without asking.

### What to Verify

- **Daemon commands**: Send commands via `bin/xmpctl`, check response
- **Search actions**: Run `bin/xmpd-search`, select a track, press the keybinding, observe result
- **Playback**: After play/queue, check MPD status (`mpc status`, `mpc playlist`) and logs
- **Stream proxy**: Check logs for 404 vs successful stream resolution
- **fzf keybindings**: Run xmpd-search and test each binding manually

### Verify Before Coding

If your phase involves investigating a bug:
1. Reproduce the bug first -- observe the actual failure
2. Add debug logging if needed to trace the issue
3. THEN write your fix based on observed behavior, not assumptions
4. Verify the fix actually resolves the reproduced bug

---

## Context Budget

You have approximately **120k tokens** total (input + output + thinking).

**Be strategic**:
- Read only what you need
- Follow the workflow above exactly
- Keep summaries concise
- Don't read entire files when you need one function
- Don't read all phase plans when you need one phase

---

## Important Notes

### Security -- No Credentials in Repository

**CRITICAL: Never store passwords, API keys, tokens, connection strings, or any secrets in repository files.**

### Secret Tagging in Documentation

When you need to reference infrastructure-specific values in agent framework documentation files under `docs/agent/`, use inline tags:

```
[LABEL]
```

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

---

## Quick Checklist

Before you begin:
- [ ] **FIRST: Run `pwd` and verify you're in `/home/tunc/Sync/Programs/xmpd`**
- [ ] Read `docs/agent/search-and-tidal-quality/STATUS.md` to identify your phase and check safety posture
- [ ] Read `docs/agent/search-and-tidal-quality/CODEBASE_CONTEXT.md` for codebase knowledge
- [ ] Read the 2 most recent phase summaries from `docs/agent/search-and-tidal-quality/summaries/`
- [ ] Read your phase plan from `docs/agent/search-and-tidal-quality/phase_plans/PHASE_XX.md`
- [ ] Understand your deliverables and completion criteria

During your work:
- [ ] Stay within your phase boundaries
- [ ] Activate environment before running commands (see Environment Setup section)
- [ ] Build incrementally -- verify each chunk before moving on
- [ ] Check logs after running or deploying code
- [ ] Commit after each verified chunk (multiple commits per phase is fine)
- [ ] Write tests if required

After completion:
- [ ] Update `docs/agent/search-and-tidal-quality/CODEBASE_CONTEXT.md` with new discoveries and changes
- [ ] Create phase summary using the template (include live verification sections)
- [ ] Verify all completion criteria are met (or document why not)
- [ ] Update `docs/agent/search-and-tidal-quality/STATUS.md`
- [ ] Final commit for docs, push if git is enabled (see Step 7)
- [ ] Do NOT start the next phase

---

## Ready to Start?

1. Read `docs/agent/search-and-tidal-quality/STATUS.md`
2. Follow the workflow above
3. Build, verify, commit -- repeat
4. Document and update status

---

*This quickstart is designed for AI agents working in a phased development workflow. For human developers, see the standard project README.*
