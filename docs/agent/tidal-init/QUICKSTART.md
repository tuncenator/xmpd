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
- **Feature Docs**: `/home/tunc/Sync/Programs/xmpd/docs/agent/tidal-init`

### Path Usage Rules

1. **Stay in project root** - Do NOT `cd` to other directories
2. **All paths are relative to project root** - When you see `docs/agent/...`, it means `/home/tunc/Sync/Programs/xmpd/docs/agent/...`
3. **If confused about location** - Run `pwd` to verify you're in `/home/tunc/Sync/Programs/xmpd`
4. **Use relative paths in your work** - Reference files as `docs/agent/...` not absolute paths

**Example Path Reference:**
```
Relative path: docs/agent/tidal-init/STATUS.md
Absolute path: /home/tunc/Sync/Programs/xmpd/docs/agent/tidal-init/STATUS.md
                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
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
|       +-- tidal-init/                    <- Your feature folder
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

Read `docs/agent/tidal-init/STATUS.md` to identify:
- Which phase is current (marked as CURRENT)
- Your phase number and name
- Integration settings (Git, Jira, Deployment, Safety Posture)

### Step 2: Get Context

**2a. Read the codebase context** (always, before anything else):
- Read `docs/agent/tidal-init/CODEBASE_CONTEXT.md`
- This contains cumulative knowledge about the codebase from all previous phases
- Use this instead of re-exploring the codebase from scratch
- Only explore further if you need information not covered in this document

**2b. Read recent phase summaries** (up to 2 most recent):
- If you're on Phase 5, read `PHASE_04_SUMMARY.md` and `PHASE_03_SUMMARY.md`
- If you're on Phase 1 or 2, read what's available (or nothing if Phase 1)

**Location**: `docs/agent/tidal-init/summaries/`

### Step 3: Read Your Phase Plan

Open `docs/agent/tidal-init/phase_plans/PHASE_XX.md` where XX is your phase number (zero-padded: 01, 02, ..., 10, 11, ...).

This file contains everything you need for your phase:
- Objective and deliverables
- Detailed requirements
- Dependencies and completion criteria
- Testing requirements

If you also need the big picture (architecture, cross-cutting concerns), read the relevant sections of `docs/agent/tidal-init/PROJECT_PLAN.md` -- but only as needed.

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

**Format**: `[Phase {N}/{TOTAL}] {verb}: {what changed}`

**Verbs** (lowercase): `add`, `fix`, `update`, `refactor`, `remove`, `docs`

**Examples**:
- `[Phase 3/14] add: YTMusicProvider.list_playlists wrapping existing client`
- `[Phase 3/14] add: TrackMetadata art_url field`
- `[Phase 3/14] fix: liked-state lookup off-by-one in get_like_state`
- `[Phase 3/14] docs: phase summary and context updates`

Get {N} and {TOTAL} from STATUS.md (e.g., "Current Phase: 3 of 14"). Multiple commits per phase is expected and encouraged.

#### 4d. Deploy and Verify on Target

Deployment is **disabled** for this feature. Skip step 4d.

#### 4e. Repeat

Continue the cycle (4a-4c) until all deliverables for your phase are complete.

### Step 5: Document Your Work

**5a. Update the codebase context**:
- Edit `docs/agent/tidal-init/CODEBASE_CONTEXT.md`
- Update the "Last updated by" line at the top to reflect your phase name and today's date
- Add any new files you created (to "Key Files & Modules")
- Add any new APIs, classes, or interfaces you built (to "Important APIs & Interfaces")
- Add any new data models (to "Data Models")
- Update any entries that changed due to your work (renamed files, modified APIs, etc.)
- Remove entries for things that no longer exist
- Keep updates incremental -- do not rewrite sections that are still accurate

**5b. Create your phase summary**:
- **Template**: `docs/agent/tidal-init/PHASE_SUMMARY_TEMPLATE.md`
- **Output location**: `docs/agent/tidal-init/summaries/PHASE_XX_SUMMARY.md`
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

Edit `docs/agent/tidal-init/STATUS.md`:
1. Mark your phase as Complete
2. Update "Current Phase" to next phase number
3. Update "Phase Name" to next phase name
4. Update "Last Updated" to today's date (YYYY-MM-DD format)

### Step 7: Final Commit and Integration Updates

**Git**: Your code commits are already pushed from Step 4c. Now do a final commit for documentation:
1. Stage all doc changes (summary, STATUS.md, CODEBASE_CONTEXT.md)
2. Commit: `[Phase {N}/{TOTAL}] docs: phase summary and context updates`
3. Push: `git push origin feature/tidal-init`

Jira is not configured for this feature. Skip the Jira comment step.

### Step 8: Stop

Your work is complete! The next agent will handle the next phase.

---

## Environment Setup

This is a Python 3.11+ project managed with `uv` (PEP 621 / `pyproject.toml`).

**First-time setup** (already done in this checkout, but in case the venv goes stale):

```bash
cd /home/tunc/Sync/Programs/xmpd
uv venv                                    # creates .venv/
source .venv/bin/activate
uv pip install -e '.[dev]'                 # installs xmpd in editable mode + dev tools
```

**Activate before each session** (sub-agent shells inherit the parent activation, but if you spawn a fresh subprocess that needs the venv, activate first):

```bash
source /home/tunc/Sync/Programs/xmpd/.venv/bin/activate
```

**Common commands:**

```bash
# Test suite (all tests, quiet)
pytest -q

# Test a single module
pytest tests/test_providers_base.py -v

# Single test by name
pytest tests/test_providers_ytmusic.py::test_list_playlists_returns_playlist_objects -v

# Lint + format check (project uses ruff)
ruff check xmpd/ tests/

# Type-check (mypy with strict settings per pyproject)
mypy xmpd/

# Run the daemon locally (foreground; stops on Ctrl-C)
python -m xmpd

# CLI controller
xmpctl --help
```

**Notes on the existing codebase:**

- Source lives in `xmpd/` (Python package), CLI scripts in `bin/`, tests in `tests/`.
- The systemd unit `xmpd.service` ships in the repo root; user installs it via `install.sh`.
- AirPlay bridge is a separate sub-project at `extras/airplay-bridge/` -- relevant for Phase D (Tidal album art).
- After a Stage B/C phase that renames files (`xmpd/icy_proxy.py` -> `xmpd/stream_proxy.py`, `xmpd/cookie_extract.py` -> `xmpd/auth/ytmusic_cookie.py`), run `pytest -q` to confirm imports updated cleanly across the codebase.

---

## Project Helpers

This project ships verified helper scripts under `scripts/` that wrap mechanical tasks (Jira, deploy, smoke probes, etc.) so agents don't need to reconstruct them from scratch each phase.

**Coding agents:** consult ONLY the helpers listed in your phase plan's "Helpers Required" section. Do NOT scan this catalog by default -- it exists for reference, for the planner, and for the user. Reaching past your phase plan's allocation usually means the planner under-specified the phase; if you genuinely need a helper that isn't listed, do the work manually this time and record it under your phase summary's "Helper Issues -> Unlisted helpers attempted" subsection.

**Checkpoint agent:** uses deploy/verify-deploy/smoke helpers automatically when configured -- the helper names are in STATUS.md.

No helpers configured for this feature.

(Jira, deploy, and smoke integrations are all disabled, so no integration-time helpers were authored. None of the 13 phase planners proposed reusable helpers either -- every mechanical task is one-shot in its phase.)

---

## Live Verification

**Verify as you build, not just at the end.**

This project uses live verification: every logical chunk of code should be tested against reality before moving on. Do not wait until all deliverables are complete to run the program for the first time.

### Safety Posture

Check `docs/agent/tidal-init/STATUS.md` for the current safety posture.

This project uses **RELAXED safety posture with Tidal-account guardrails**. The user has a personal Tidal HiFi account and an existing YouTube Music account; both are available for testing real provider behavior end-to-end.

**Free to do without asking:**

- Anything read-only against either provider (search, get_radio, list_playlists, get_playlist_tracks, get_favorites, get_track_metadata, resolve_stream)
- Calling `tidalapi.Session.search`, `track.get_url(...)`, `user.playlists()`, `user.favorites.tracks()`, etc.
- Running the daemon locally and exercising the proxy
- Running tests including ones that hit real APIs (mark them with `@pytest.mark.tidal_integration` per the plan; gate behind an env var so they're opt-in for CI)
- Favoriting a sentinel test track on Tidal as part of testing the like flow, **then unfavoriting that same sentinel** to clean up

**HARD GUARDRAIL: do NOT touch the user's existing Tidal favorites or playlists destructively.**

- Do NOT call `unlike` / `dislike` / `unfavorite` against tracks you didn't first favorite within the same test run. The dislike-maps-to-unfavorite semantics in this codebase mean a stray dislike call removes a song from the user's real library. That is data loss.
- Do NOT call any "delete playlist" / "remove track from playlist" API against existing playlists. There is no current legitimate test reason to do that.
- When testing the like/unlike round trip, choose a sentinel track that is NOT already in the user's favorites. Verify it's added, then remove it. If the sentinel happens to already be favorited, pick a different one.
- The same applies to YouTube Music's like/dislike toggle and removeLikedSong APIs.

**If you need to do something not clearly covered:** ASK the user first. Better to lose 30 seconds than a song from a playlist.

### What to Verify

- **Functions**: Call them with sample inputs, check outputs match expectations
- **Endpoints**: Hit them with curl, check responses and status codes
- **Database operations**: Run queries against `~/.config/xmpd/track_mapping.db` (read-only `sqlite3 -readonly` for inspection; let the daemon do writes)
- **External API calls**: Make safe read-only calls first to verify connectivity and response format
- **CLI commands**: Run `xmpctl ...` against the actual daemon
- **Logs**: After every run, check `~/.config/xmpd/xmpd.log` for expected output and no errors

### Write Operation Safety

When you need to test write operations:

1. **Prefer safe patterns**: favorite a sentinel track on Tidal, verify, unfavorite the same sentinel.
2. **Verify before touching**: Check that the target does not contain important data (e.g., is the track you're about to favorite already in favorites? if so, choose a different one).
3. **Never touch the user's real liked-songs library destructively** (see HARD GUARDRAIL above).
4. **If no safe method exists**: Discuss with the user. Explain what you want to test, what the risks are, and ask for guidance.

### Verify Before Coding

If your phase involves interacting with an external API or service:
1. Check CODEBASE_CONTEXT.md and your phase plan's Technical Reference section -- they may already have research findings from setup.
2. If needed, make a safe read-only call to verify connectivity and current response format.
3. THEN write your integration code based on actual observed behavior, not assumptions.
4. Do NOT code an entire API client based on training data and then discover the API has changed. tidalapi in particular is unofficial and changes more often than its docstrings imply.

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
- If a phase requires credentials (Tidal session token, YT Music browser cookies, etc.):
  1. Check your Claude memories -- credentials may already be stored there from a previous phase or session
  2. If not in memory, ask the user to provide them
  3. Save credentials to Claude memory so future agents can access them without the repo
  4. In code, reference credentials via environment variables or config files outside the repo (`~/.config/xmpd/...`)
- A pre-commit hook is active on this repository to catch accidental credential leaks and to redact `[LABEL]` markers in agent docs

#### Pre-commit hook block: bypass procedure

Most blocks are real. The hook redacts `[LABEL]` markers and matches secret-shaped patterns. False positives happen but are not the common case.

**Do NOT bypass with `git commit --no-verify` if any of these are true:**

- The blocked file is under `xmpd/`, `bin/`, `scripts/`, `extras/`, `tests/` (real source paths)
- The blocked file matches `.env*` (any dotenv variant)
- The matched value looks like a real token:
  - 40+ characters of base64 (`[A-Za-z0-9+/=]`)
  - 3-part JWT (`xxx.yyy.zzz` with base64 segments)
  - AWS key ID prefixes: `AKIA`, `ASIA`
  - GitHub token prefixes: `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`
  - Stripe live-key prefix: `sk_live_`
  - Slack token prefixes: `xoxb-`, `xoxp-`, `xapp-`
  - Anything that looks like a private key block (`-----BEGIN ... PRIVATE KEY-----`)

If any of the above hold, stop. End your phase with a line naming the blocked file and the pattern (after redaction), so the conductor can surface it to a human. Do NOT bypass.

**Bypass procedure (only when none of the above hold):**

1. Print to your output:
   - **Path**: full path of the blocked file (relative to project root)
   - **Matched pattern**: the value the hook flagged, redacted to `[LABEL]` form (replace any actual token tail with `***` if you cannot tag it)
   - **Reason it is not a real secret**: one sentence (e.g. "test fixture string in `tests/fixtures/sample-jwt.json`, not used at runtime")
2. Add a `Bypass-reason:` trailer to the commit message body using `git commit --no-verify -m "..." -m "Bypass-reason: <one-line reason>"`. The trailer is what `/spark-status` will count when surfacing bypass usage; commits without the trailer are flagged as unaudited.
3. Commit. The bypass and reason are now in git history for review.

If you bypass without the trailer, the next reviewer (and `/spark-status`) will treat it as an unaudited bypass. Don't.

### Secret Tagging in Documentation

When you need to reference infrastructure-specific values (hostnames, paths, ports, account names) in agent framework documentation files under `docs/agent/`, use inline tags:

```
[LABEL]
```

Examples:
- `[CONFIG_DIR]` -- the pre-commit hook redacts this to `[CONFIG_DIR]` before commit
- `[TIDAL_USER]` -- becomes `[TIDAL_USER]` in the committed file

You always see the real values in your local working copy. Only the committed version is redacted.

**Rules:**
- Use this for ALL sensitive or infrastructure-specific values in files under `docs/agent/`
- The label should be descriptive (e.g., `CONFIG_DIR`, not `SECRET1`)
- Do NOT put secrets in code files -- use config files outside the repo (`~/.config/xmpd/`) for runtime
- This tagging is ONLY for documentation/agent framework files

### Logging

**Always check logs.** After running code, deploying, or restarting a service:
1. Check `~/.config/xmpd/xmpd.log` (the project's log file) for errors, warnings, or unexpected behavior
2. If logs show issues, fix them before proceeding
3. Include relevant log observations in your phase summary

If you are Phase 1 and logging is not yet adapted to the new package layout, your phase plan will mark logging-config touch-ups as a deliverable.

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
- [ ] Read `docs/agent/tidal-init/STATUS.md` to identify your phase and check safety posture
- [ ] Read `docs/agent/tidal-init/CODEBASE_CONTEXT.md` for codebase knowledge
- [ ] Read the 2 most recent phase summaries from `docs/agent/tidal-init/summaries/`
- [ ] Read your phase plan from `docs/agent/tidal-init/phase_plans/PHASE_XX.md`
- [ ] Understand your deliverables and completion criteria

During your work:
- [ ] Stay within your phase boundaries
- [ ] Activate environment before running commands (see Environment Setup section)
- [ ] Build incrementally -- verify each chunk before moving on
- [ ] Check `~/.config/xmpd/xmpd.log` after running code
- [ ] Use `[LABEL]` for sensitive values in doc files
- [ ] Commit after each verified chunk (multiple commits per phase is fine)
- [ ] Write tests if required
- [ ] Respect the Tidal HARD GUARDRAIL -- never destroy user library data

After completion:
- [ ] Update `docs/agent/tidal-init/CODEBASE_CONTEXT.md` with new discoveries and changes
- [ ] Create phase summary using the template (include live verification section)
- [ ] Verify all completion criteria are met (or document why not)
- [ ] Update `docs/agent/tidal-init/STATUS.md`
- [ ] Final commit for docs, push (see Step 7)
- [ ] Do NOT start the next phase

---

## Ready to Start?

1. Read `docs/agent/tidal-init/STATUS.md`
2. Follow the workflow above
3. Build, verify, commit -- repeat
4. Document and update status

**Good luck, Agent!**

---

*This quickstart is designed for AI agents working in a phased development workflow. For human developers, see the standard project README.*
