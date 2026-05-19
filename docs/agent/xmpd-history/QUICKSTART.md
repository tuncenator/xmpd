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
- **Feature Docs**: `/home/tunc/Sync/Programs/xmpd/docs/agent/xmpd-history`

### Path Usage Rules

1. **Stay in project root** - Do NOT `cd` to other directories
2. **All paths are relative to project root** - When you see `docs/agent/...`, it means `/home/tunc/Sync/Programs/xmpd/docs/agent/...`
3. **If confused about location** - Run `pwd` to verify you're in `/home/tunc/Sync/Programs/xmpd`
4. **Use relative paths in your work** - Reference files as `docs/agent/...` not absolute paths

**Example Path Reference:**
```
Relative path: docs/agent/xmpd-history/STATUS.md
Absolute path: /home/tunc/Sync/Programs/xmpd/docs/agent/xmpd-history/STATUS.md
                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
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
|       +-- xmpd-history/                  <- Your feature folder
|           +-- QUICKSTART.md              <- You are here
|           +-- PROJECT_PLAN.md            <- Project overview, architecture, cross-cutting
|           +-- STATUS.md                  <- Phase tracker + integrations + deploy config
|           +-- CODEBASE_CONTEXT.md        <- Cumulative codebase knowledge
|           +-- FUNCTIONAL_QA_STRATEGY.md  <- Surface inventory, user loops, anti-patterns
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

---

## Your Workflow

### Step 1: Find Your Phase

Read `docs/agent/xmpd-history/STATUS.md` to identify:
- Which phase is current (marked as CURRENT)
- Your phase number and name
- Integration settings (Git, Jira, Deployment, Safety Posture)

### Step 2: Get Context

**2a. Read the codebase context** (always, before anything else):
- Read `docs/agent/xmpd-history/CODEBASE_CONTEXT.md`
- This contains cumulative knowledge about the codebase from all previous phases
- Use this instead of re-exploring the codebase from scratch
- Only explore further if you need information not covered in this document

**2b. Read recent phase summaries** (up to 2 most recent):
- If you're on Phase 5, read `PHASE_04_SUMMARY.md` and `PHASE_03_SUMMARY.md`
- If you're on Phase 1 or 2, read what's available (or nothing if Phase 1)

**Location**: `docs/agent/xmpd-history/summaries/`

### Step 3: Read Your Phase Plan

Open `docs/agent/xmpd-history/phase_plans/PHASE_XX.md` where XX is your phase number (zero-padded: 01, 02, ..., 10, 11, ...).

This file contains everything you need for your phase. **Do NOT read all phase plan files** -- only read yours.

### Step 4: Build, Verify, Commit (Repeat)

Follow this cycle for each logical chunk of work in your phase. Do NOT code everything and test at the end.

#### 4a. Code a Logical Chunk

Implement a coherent piece of functionality. Keep chunks small enough to verify independently.

#### 4b. Verify Locally

For every claim you make about your code ("tests pass", "function outputs X"), follow this verification gate:

1. **Identify** the command that proves the claim
2. **Run** it fresh -- not from a previous run, not from memory
3. **Read** the full output and check the exit code
4. **Confirm** the output actually proves what you claim

Apply the gate to:

- **Tests**: Run `uv run pytest -xvs path/to/test_<module>.py`, read the output, paste the actual results in your summary
- **Live verification on a test peer**: see the Live Verification section below for the multi-host story
- **Logs**: Verify your code produces appropriate log output. After live restart on `[TEST_HOST_1]`/`[TEST_HOST_2]`, check `journalctl --user -u xmpd -n 50 --no-pager`

If something is wrong, fix it before continuing -- but follow the debugging protocol below. Do not guess-and-check.

#### When Verification Fails

When a test fails or code doesn't behave as expected:

1. **Investigate**: Read the full error output. Don't skim. Trace the failure to its origin.
2. **Compare**: Find working code in the same codebase that does something similar (e.g., `xmpd/track_store.py` for HistoryStore patterns, `bin/xmpd-search` for fzf wrapper patterns). Compare it against your failing code.
3. **Hypothesize**: Form one specific theory about the root cause. Test it minimally.
4. **Fix**: Apply a single targeted change. Re-run verification.

One hypothesis, one fix, one verification cycle.

#### 4c. Commit

Stage the changes for this chunk and commit with a descriptive message.

**Format**: `[Phase {N}/{TOTAL}] {verb}: {what changed}`

**Verbs** (lowercase): `add`, `fix`, `update`, `refactor`, `remove`, `docs`

**Examples**:
- `[Phase 1/8] add: HistoryStore module with add_play and schema migration`
- `[Phase 3/8] add: HistorySyncer bidir_push with tailscale precheck`
- `[Phase 3/8] fix: NDJSON line buffering in bidir_push`
- `[Phase 5/8] docs: phase summary and context updates`

Get {N} and {TOTAL} from STATUS.md. Multiple commits per phase is expected.

#### 4d. Deploy and Verify on Target

Deployment is DISABLED for this feature in the Spark pipeline. Code propagates to `[TEST_HOST_1]` / `[TEST_HOST_2]` via Syncthing replication of `~/Sync`. You do not push to a remote target.

The exception is `scripts/xmpd-history-receiver`, which the receiver phase deploys to WATCHTOWER via a one-shot `scp` step -- the phase plan for that phase contains the exact commands.

If your phase needs LIVE verification on a test peer (most phases do not), see the Live Verification section below.

#### 4e. Repeat

Continue the cycle (4a-4c) until all deliverables for your phase are complete.

### Step 5: Document Your Work

**5a. Update the codebase context**:
- Edit `docs/agent/xmpd-history/CODEBASE_CONTEXT.md`
- Update the "Last updated by" line at the top to reflect your phase name and today's date
- Add any new files you created (to "Key Files & Modules")
- Add any new APIs, classes, or interfaces you built (to "Important APIs & Interfaces")
- Add any new data models (to "Data Models")
- Update any entries that changed due to your work
- Remove entries for things that no longer exist
- Keep updates incremental -- do not rewrite sections that are still accurate

**5b. Create your phase summary**:
- **Template**: `docs/agent/xmpd-history/PHASE_SUMMARY_TEMPLATE.md`
- **Output location**: `docs/agent/xmpd-history/summaries/PHASE_XX_SUMMARY.md`
- **Length**: Keep it concise (~400-500 lines max)

Include:
- What you built
- Files created/modified
- Completion criteria status
- Any challenges or deviations
- Notes for future phases
- **Functional QA Results** (if `Functional: yes`): one entry per check from your phase plan's "Functional QA" section, with surface, invocation, observed outcome (pasted byte-for-byte), and pass/fail
- **Live Verification Results** (if you exercised a test peer): the ssh heredoc commands you ran and what `journalctl` showed
- List of all commits made during this phase

### Step 6: Update Status

Edit `docs/agent/xmpd-history/STATUS.md`:
1. Mark your phase as Complete
2. Update "Current Phase" to next phase number
3. Update "Phase Name" to next phase name
4. Update "Last Updated" to today's date (YYYY-MM-DD format)

### Step 7: Final Commit and Integration Updates

**Git**: Your code commits are already made from Step 4c. Now do a final commit for documentation:
1. Stage all doc changes (summary, STATUS.md, CODEBASE_CONTEXT.md)
2. Commit: `[Phase {N}/{TOTAL}] docs: phase summary and context updates`
3. Push: `git push origin feature/xmpd-history`

**Jira**: not configured for this feature. Skip.

### Step 8: Stop

Your work is complete. The next agent will handle the next phase.

---

## Environment Setup

### First-time setup

```bash
cd /home/tunc/Sync/Programs/xmpd
uv sync --all-extras   # creates .venv and installs all deps including dev extras
```

### Activate before each session

```bash
cd /home/tunc/Sync/Programs/xmpd
source .venv/bin/activate
```

Most agents do NOT need to activate the venv -- prefer `uv run <cmd>` which uses the project's lockfile without polluting the shell.

### Common commands

| Action | Command |
|--------|---------|
| Run all tests | `uv run pytest -xvs` |
| Run specific test file | `uv run pytest tests/test_history_store.py -xvs` |
| Run a single test | `uv run pytest tests/test_history_store.py::test_add_play -xvs` |
| Lint (project-wide) | `uv run ruff check .` |
| Format check | `uv run ruff format --check .` |
| Auto-format | `uv run ruff format .` |
| Type check the package | `uv run mypy xmpd/` |
| Run the daemon (DO NOT on `[LIVE_HOST]`) | `uv run python -m xmpd` |

Project conventions per `pyproject.toml`:

- Python 3.11, line length 100, ruff selectors `E, F, W, I, N, UP`.
- mypy `disallow_untyped_defs = true` -- every new function needs annotations.
- pytest excludes `tests/research/`.
- Live Tidal tests are gated by `XMPD_TIDAL_TEST=1` and marked `tidal_integration` -- this feature does not add new Tidal-live tests.

### Important paths

- Repository root: `/home/tunc/Sync/Programs/xmpd`
- Module: `xmpd/` (new modules go here)
- Tests: `tests/` (one `test_<module>.py` per new module)
- CLI binaries: `bin/` (e.g., new `bin/xmpd-history`, `bin/xmpd-doctor`)
- Standalone scripts: `scripts/` (e.g., new `scripts/xmpd-history-receiver`)
- Live config (DO NOT modify on `[LIVE_HOST]`): `~/.config/xmpd/config.yaml`
- Live local history DB (DO NOT modify on `[LIVE_HOST]`): `~/.config/xmpd/history.db` (created by the daemon after Phase 1 wires it in)

---

## Development Discipline

### Test-First Development

For every behavior you implement: write a failing test first, watch it fail, then write the minimal code to pass it. This is non-negotiable.

1. **RED**: Write test describing expected behavior. Run it with `uv run pytest tests/test_<module>.py::test_<name> -xvs`. Confirm it fails because the feature is missing (not because of a typo or import error).
2. **GREEN**: Write the simplest code that passes. No extras.
3. **REFACTOR**: Clean up while tests stay green.

Test command: `uv run pytest -xvs`

Run after every implementation chunk. If tests fail after your change, debug systematically (see Workflow step 4b) before attempting fixes.

### Verification Honesty

Before claiming any task is done, run the verification command and read the output. 'Should work' is not evidence. 'Tests likely pass' is not evidence. Run it, read it, report what it actually says.

### Debugging Protocol

When something fails:
1. Read the FULL error (don't skim)
2. Trace backward to the source of the bad value
3. Form ONE hypothesis, test minimally
4. If 3 hypotheses fail: this is architectural, not a bug. Document and escalate.

---

## Project Helpers

This project ships verified helper scripts under `scripts/` that wrap mechanical tasks (smoke probes, etc.) so agents don't need to reconstruct them from scratch each phase.

**Coding agents:** consult ONLY the helpers listed in your phase plan's "Helpers Required" section. Do NOT scan this catalog by default.

**Checkpoint agent:** uses smoke helpers automatically when configured -- the helper names are in STATUS.md.

| Helper | Purpose | Invocation | Failure mode |
|--------|---------|------------|--------------|
| `scripts/spark-smoke-artifact.sh` | Artifact-tier smoke probe owned by `spark-checkpoint`: imports the new `xmpd/history_*.py` modules, runs `xmpd-history-receiver version`, syntax-checks `bin/xmpd-history` and `bin/xmpd-doctor`, and runs `xmpctl --help`. Does NOT touch the live daemon or `~/.config/xmpd/*`. | `scripts/spark-smoke-artifact.sh [<changed-path>...]` (no args -> probe all known surfaces; with paths -> probe only surfaces whose markers match). | One `FAIL: <surface> -- <reason>` line per failed surface on stdout, exit 1. Read `# MANUAL FALLBACK:` block; do NOT edit (the checkpoint owns repairs). |
| `scripts/spark-restart-peer.sh` | Wait for Syncthing to replicate the local git HEAD to a peer, then `systemctl --user restart` the named service, then dump fresh `is-active` + last 20 journalctl lines. Used in Phase 8 (Integration Testing) for every peer restart. | `scripts/spark-restart-peer.sh <peer> [service] [timeout_seconds]`. `peer` is an SSH alias from `~/.ssh/config` (e.g. `[TEST_HOST_1]`); `service` defaults to `xmpd`; `timeout_seconds` defaults to 60. | One `FAIL: <reason>` line on stdout, exit 1. Read `# MANUAL FALLBACK:` block in the script; record the failure in your phase summary's "Helper Issues" section. |

**On any helper failure:** read the script's `# MANUAL FALLBACK:` comment block, do the work manually, record the failure in your phase summary's "Helper Issues" section. Never edit the helper yourself.

**Self-check:** every helper supports `--self-check` to verify its prerequisites.

---

## Live Verification

**Verify as you build, not just at the end.**

This project uses live verification with a multi-host twist.

### Safety Posture

This project uses CAUTIOUS safety posture. Before performing any write operation to external systems, databases, or services -- even locally -- ASK the user for permission and explain why the operation could be risky. Read-only operations (GET requests, SELECT queries, log reading, running tests against tmp DBs) can be performed freely without asking.

### Runtime Context

This project runs as a `systemd --user` service named `xmpd` on every host. The daemon is a singleton: it binds an IPC socket and connects to MPD on the user's well-known port. A second instance fails to start (port collision).

**Hosts and their roles**:

| Host | Role | Live restart allowed? |
|------|------|------------------------|
| `[LIVE_HOST]` (this machine) | The user is actively listening here. | **NO.** Never restart `xmpd` on `[LIVE_HOST]`. Never modify `~/.config/xmpd/*` on `[LIVE_HOST]`. Unit tests against isolated tmp DBs are fine. |
| `[TEST_HOST_1]` | Idle test peer. | **YES.** Free to restart and verify. |
| `[TEST_HOST_2]` | Idle test peer. | **YES.** Free to restart and verify. |
| WATCHTOWER | Always-on GCP aggregator. | No xmpd daemon runs here. SSH target for receiver deploy. |

**Code replication**: the project lives at `~/Sync/Programs/xmpd` and Syncthing replicates from `[LIVE_HOST]` to `[TEST_HOST_1]` / `[TEST_HOST_2]` within ~60s. Always verify replication finished before restarting a peer.

**Verification flow for any phase that needs live daemon contact**:

1. **Commit code on `[LIVE_HOST]`**:

   ```bash
   git add <files>
   git commit -m "[Phase N/M] add: ..."
   ```

2. **Wait for Syncthing to replicate**. Confirm by comparing HEAD on the remote peer:

   ```bash
   LOCAL_HEAD=$(git rev-parse HEAD)
   ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   cd ~/Sync/Programs/xmpd && git rev-parse HEAD
   EOF
   ```

   Loop until the remote HEAD matches `$LOCAL_HEAD`. Typical wait: 10-60 seconds.

3. **Restart `xmpd` on the test peer** and read fresh logs:

   ```bash
   ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   systemctl --user restart xmpd
   sleep 2
   systemctl --user is-active xmpd
   journalctl --user -u xmpd -n 50 --no-pager
   EOF
   ```

4. **Exercise the surface and verify the observable outcome** (the specifics belong in your phase plan's "Functional QA" section).

**SSH transport rule**: Claude Code has no TTY. `ssh HOST "command"` connects but hangs without output. ALWAYS use the heredoc pattern above. The `__START__` marker strips the MOTD banner so only command output comes through. `2>/dev/null` suppresses the "Pseudo-terminal will not be allocated" warning.

**Never do**:
- Restart `xmpd` on `[LIVE_HOST]` (interrupts the user's active playback).
- Spawn `python -m xmpd` or `uv run python -m xmpd` directly on `[LIVE_HOST]` (port collision with the live daemon).
- Write to `~/.config/xmpd/history.db` on `[LIVE_HOST]` outside the daemon (race with the live writer).
- Restart `xmpd` on `[TEST_HOST_1]` or `[TEST_HOST_2]` before Syncthing replication completes (you'll restart against stale code and waste a cycle).
- Assume `~/bin/xmpd-history-receiver` exists on WATCHTOWER until the receiver phase deploys it; verify with `xmpd-doctor` after that phase lands.
- Use `ssh HOST "command"` -- always use the heredoc pattern above.

### What to Verify

The project-specific answer lives in `docs/agent/xmpd-history/FUNCTIONAL_QA_STRATEGY.md`. Read it once at the start of the phase. It captures:

- **Surface Inventory** -- what this feature exposes (HistoryStore API, HistoryReporter side effect, xmpctl subcommands, fzf wrapper, receiver script, doctor)
- **User Loop** -- the minimal sequence a real user/consumer performs and what they observe
- **Verification Mechanics** -- the concrete harness (pytest fixtures + tmp HOME + temp SQLite + mocked subprocess; SSH heredoc helper for live STORMTREE/VICAR runs)
- **Anti-Patterns** -- project-specific traps
- **Required Harness Deliverables** -- scaffolding owned by Phase 1 (or later if the surface itself comes later)

Your phase plan's "Functional QA" section names the specific checks for THIS phase. Run each one, capture the actual command and actual output byte-for-byte, and record pass/fail in your phase summary.

### Write Operation Safety

This is cautious mode. Treat any write to `~/.config/xmpd/*` on `[LIVE_HOST]` as forbidden. Writes against isolated tmp `HOME` + temp SQLite DB are fine. Writes on `[TEST_HOST_1]` / `[TEST_HOST_2]` need a brief explanation in your phase summary of what got written and where.

WATCHTOWER's `~/xmpd-history/history.db` is acceptable to write to once the receiver phase ships, but verify the receiver protocol matches BEFORE running a non-dry test push (schema mismatch = corrupted aggregator).

### Verify Before Coding

This project does not consume new external APIs as part of this feature -- the Tidal and YT Music integrations are unchanged. The only external interface to verify shape against is the receiver wire format, which the receiver phase defines.

---

## Context Budget

You have approximately **120k tokens** total (input + output + thinking).

TDD discipline (test-first for every behavior) uses ~30% more tokens than implementation-only. Don't skip tests to save context.

**Be strategic**:
- Read only what you need
- Follow the workflow above exactly
- Keep summaries concise
- Don't read entire files when you need one function
- Don't read all phase plans when you need one phase
- Don't explore unrelated code

If you run out of context:
- Note this in your summary
- Document what's incomplete
- Suggest splitting the phase

---

## Important Notes

### Security -- No Credentials in Repository

**CRITICAL: Never store passwords, API keys, tokens, connection strings, or any secrets in repository files.**

The xmpd project stores OAuth tokens at `~/.config/xmpd/oauth.json` (gitignored) and a Tidal session in the user's local state. No new secrets are introduced by this feature -- SSH to WATCHTOWER uses the existing key authority and the user's ssh config alias.

A pre-commit hook is active on this repository to catch accidental credential leaks and to redact `[LABEL]` markers in agent docs.

#### Pre-commit hook block: bypass procedure

Most blocks are real. The hook redacts `[LABEL]` markers and matches secret-shaped patterns. False positives happen but are not the common case.

**Do NOT bypass with `git commit --no-verify` if any of these are true:**

- The blocked file is under `xmpd/`, `bin/`, `scripts/`, `tests/`, or any path matching `**/secrets/**`
- The blocked file matches `.env*` (any dotenv variant)
- The matched value looks like a real token (40+ char base64, JWT, AKIA/ASIA, ghp_/gho_, sk_live_, xoxb-/xoxp-, private key block).

**Bypass procedure (only when none of the above hold):**

1. Print to your output:
   - **Path**: full path of the blocked file
   - **Matched pattern**: the value the hook flagged, redacted to `[LABEL]` form
   - **Reason it is not a real secret**: one sentence (e.g. "test fixture string in `tests/fixtures/sample.json`, not used at runtime")
2. Add a `Bypass-reason:` trailer to the commit message body using `git commit --no-verify -m "..." -m "Bypass-reason: <one-line reason>"`.
3. Commit.

### Secret Tagging in Documentation

When you need to reference infrastructure-specific values (hostnames, IPs, paths) in agent framework documentation files under `docs/agent/`, use inline tags:

```
[LABEL]
```

Examples used in this feature:
- `[LIVE_HOST]` -- redacted to `[LIVE_HOST]` in commits.
- `[TEST_HOST_1]` -- redacted to `[TEST_HOST_1]` in commits.
- `[TEST_HOST_2]` -- redacted to `[TEST_HOST_2]` in commits.

You always see the real values in your local working copy. Only the committed version is redacted.

**Rules:**
- Use this for ALL hostnames, IPs, paths in agent docs
- Do NOT put secrets in code files -- use environment variables and `.env` files (gitignored) for code

### Logging

**Always check logs.** After running code, deploying, or restarting a service:
1. Check application logs for errors, warnings, or unexpected behavior
2. If logs show issues, fix them before proceeding
3. Include relevant log observations in your phase summary

After restarting `xmpd` on `[TEST_HOST_1]` / `[TEST_HOST_2]`:
```bash
ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
echo '__START__'
journalctl --user -u xmpd -n 50 --no-pager
EOF
```

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
- [ ] Read `docs/agent/xmpd-history/STATUS.md` to identify your phase and check safety posture
- [ ] Read `docs/agent/xmpd-history/CODEBASE_CONTEXT.md` for codebase knowledge
- [ ] Read `docs/agent/xmpd-history/FUNCTIONAL_QA_STRATEGY.md` for the verification model
- [ ] Read the 2 most recent phase summaries from `docs/agent/xmpd-history/summaries/`
- [ ] Read your phase plan from `docs/agent/xmpd-history/phase_plans/PHASE_XX.md`
- [ ] Understand your deliverables and completion criteria

During your work:
- [ ] Stay within your phase boundaries
- [ ] Build incrementally -- verify each chunk before moving on
- [ ] Check logs after running or deploying code
- [ ] Use `[LABEL]` for sensitive values in doc files
- [ ] Commit after each verified chunk
- [ ] Never restart `xmpd` on `[LIVE_HOST]`
- [ ] Use ssh heredoc pattern for any remote command
- [ ] Wait for Syncthing replication before restarting test peers

After completion:
- [ ] Update `docs/agent/xmpd-history/CODEBASE_CONTEXT.md` with new discoveries and changes
- [ ] Create phase summary using the template (include Functional QA Results and Live Verification Results)
- [ ] Verify all completion criteria are met (or document why not)
- [ ] Update `docs/agent/xmpd-history/STATUS.md`
- [ ] Final commit for docs, push (`git push origin feature/xmpd-history`)
- [ ] Do NOT start the next phase

---

## Ready to Start?

1. Read `docs/agent/xmpd-history/STATUS.md`
2. Follow the workflow above
3. Build, verify, commit -- repeat
4. Document and update status

**Good luck, Agent!**

---

*This quickstart is designed for AI agents working in a phased development workflow. For human developers, see the standard project README.*
