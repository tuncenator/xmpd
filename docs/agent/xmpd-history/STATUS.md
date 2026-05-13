# xmpd Project Status

## Project Location

**IMPORTANT: Verify your location before working!**

- **Project Root**: `/home/tunc/Sync/Programs/xmpd`
- **Feature Docs**: `/home/tunc/Sync/Programs/xmpd/docs/agent/xmpd-history`
- **Verify with**: `pwd` -> should output `/home/tunc/Sync/Programs/xmpd`

**Always work from the project root directory. All paths below are relative to project root.**

---

## Integrations

- **Git**: enabled
- **Branch**: feature/xmpd-history
- **Jira Issue**: disabled
- **GitHub Repo**: tuncenator/xmpd

### Deployment

- **Deploy Enabled**: disabled
- **SSH Host**: N/A
- **SSH User**: N/A
- **Target Path**: N/A
- **Service Name**: N/A
- **Restart Command**: N/A
- **Log Source**: N/A

> The Spark deploy pipeline is disabled. Code propagation to STORMTREE/VICAR
> happens via Syncthing replication of `~/Sync` (~60s). The
> `scripts/xmpd-history-receiver` script reaches WATCHTOWER via a one-shot
> `scp` step owned by the phase that authors it (see PROJECT_PLAN.md).

### Verification

- **Live Verification**: enabled
- **Safety Posture**: cautious
- **Runtime Model**: systemd (user service `xmpd` on each host)
- **Restart Command**: `systemctl --user restart xmpd` on `[TEST_HOST_1]` or `[TEST_HOST_2]` ONLY. NEVER on `[LIVE_HOST]` (the user is actively listening there).
- **Verification Command**: see QUICKSTART.md "Runtime Context" -- ssh heredoc pattern to a test peer, then `systemctl --user status xmpd`, `journalctl --user -u xmpd -n 50 --no-pager`, plus the phase-specific surface invocation.
- **Anti-Patterns**:
  - Never restart `xmpd` on `[LIVE_HOST]` (interrupts active playback).
  - Never spawn `python -m xmpd` directly (MPD port collision with the running daemon).
  - Never write to `~/.config/xmpd/history.db` on `[LIVE_HOST]` outside the daemon (race with the live writer).
  - Wait for Syncthing replication (compare `git rev-parse HEAD` on remote vs local) before restarting `[TEST_HOST_1]` / `[TEST_HOST_2]`.
  - Always use the SSH heredoc pattern from QUICKSTART -- `ssh HOST "command"` hangs without a TTY.
  - Never assume the WATCHTOWER receiver script is present; verify with `xmpd-doctor` (after the receiver phase lands).

### Smoke Harness

- **Smoke Harness**: enabled
- **Surface Type**: cli
- **Surface Markers**: `bin/xmpd-history`, `bin/xmpd-doctor`, `bin/xmpctl`, `xmpd/history_store.py`, `xmpd/history_syncer.py`, `xmpd/history_reporter.py`, `scripts/xmpd-history-receiver`

#### Smoke Artifact Tier

- **Smoke Artifact Tier**: enabled
- **Target**: local invocation of the CLI surfaces (`xmpctl history-json`, `bin/xmpd-history`, `scripts/xmpd-history-receiver`) against an isolated tmp `HOME` with a fixture-seeded SQLite DB. MUST NOT touch the live daemon, the live `~/.config/xmpd/history.db`, or MPD on `[LIVE_HOST]`.
- **Auth**: none (local filesystem + SQLite)
- **Prerequisites**: tmp `HOME` with `~/.config/xmpd/history.db` seeded from fixture rows; receiver script available in PATH for receiver round-trip; pytest + Python venv active.
- **Helper Script**: scripts/spark-smoke-artifact.sh (authored at end of setup)

#### Smoke Deploy Tier

- **Smoke Deploy Tier**: disabled
- **Target**: N/A
- **Auth**: N/A
- **Prerequisites**: N/A
- **Helper Script**: N/A (CLI surface; deploy tier is rejected per Spark tier matrix)

### Agentic Testing

- **Enabled**: disabled
- **Max Parallel Testers**: N/A
- **Destructive Scenario Safety**: N/A

### Conductor

- **Total Batches**: 6
- **Current Batch**: 3
- **Pacing**: auto-refresh
- **Batches Per Session**: 3
- **Execution Plan**: docs/agent/xmpd-history/EXECUTION_PLAN.md

---

**Last Updated:** 2026-05-13
**Current Phase:** 3 of 8
**Phase Name:** HistorySyncer Real Implementation
**Progress:** 25% (2/8 phases complete)

---

## Progress Bar

```
[##------] 25% (2/8)
```

---

## Quick Phase Reference

| Phase | Name | Status |
|-------|------|--------|
| 1 | HistoryStore Foundation + Config | `[Complete]` |
| 2 | HistoryReporter Wire-Up + Syncer Stub | `[Complete]` |
| 3 | HistorySyncer Real Implementation | `[Current]` |
| 4 | Receiver Script + WATCHTOWER Deploy | `[Current]` |
| 5 | xmpctl history-json + bin/xmpd-history | `[Pending]` |
| 6 | xmpctl history-backfill | `[Pending]` |
| 7 | bin/xmpd-doctor | `[Pending]` |
| 8 | Integration Testing on Test Peers | `[Pending]` |

---

## Instructions for Agents

1. Read `phase_plans/PHASE_02.md` for detailed requirements for Phase 2
2. Read the 2 most recent phase summaries (PHASE_01_SUMMARY.md)
3. Complete the phase following the build-verify-commit cycle
4. Create `summaries/PHASE_02_SUMMARY.md`
5. Update this file:
   - Mark Phase 2 as `[Complete]`
   - Set Phase 3 as `[Current]`
   - Update "Current Phase" to "3 of 8"
   - Update "Progress" percentage and count
   - Update progress bar (each `#` = completed phase, each `-` = remaining phase)

**Phase plans:** See `phase_plans/PHASE_XX.md`
**Project overview:** See `PROJECT_PLAN.md`

---

## Legend

- `[Complete]` - Phase finished and summary created
- `[Current]` - Phase currently being worked on
- `[Pending]` - Phase not yet started
- `[Blocked]` - Phase cannot proceed due to blocker
- `[InReview]` - Phase complete but needs review

---

## Notes

[Optional section for tracking blockers, decisions, or important notes. Conductor escalations write `BLOCKED:` lines here. Skipped gates are tracked separately in the next section.]

- Live multi-host verification is constrained: ARCHON is the active listener (no daemon restart); STORMTREE and VICAR are idle and free for live tests after Syncthing replicates code.
- WATCHTOWER is the central aggregator (always-on GCP VM, ssh alias WATCHTOWER). The receiver script lives at `~/bin/xmpd-history-receiver` on WATCHTOWER after the receiver phase deploys it.

---

## Skipped Gates

> Populated by `/spark-conductor` when a user opts out of a quality gate via 4e-escalate `skip`. Empty on a clean run.

| Batch | Gate | Date | Reason |
|-------|------|------|--------|

[No skipped gates -- delete this placeholder line when the first row is added.]
