# Execution Plan: xmpd-history

**Created**: 2026-05-13
**Mode**: Conductor
**Total Phases**: 8
**Total Batches**: 6

---

## Model Configuration

The conductor dispatches every subagent by `subagent_type`. Each subagent file under `~/.claude/agents/` pins its own model in frontmatter, so the conductor never passes a `model` parameter and version drift is impossible.

| Role | Subagent | Pinned model | Context | Notes |
|------|----------|--------------|---------|-------|
| Orchestrator | (slash command, not a subagent) | inherits user session model | -- | Runs as `/spark-conductor`. NOT pinned via frontmatter on purpose: pinning would override session model and strip auto mode on Max (auto mode there requires Opus 4.7). Subagents pin their own models, so the conductor inheriting is fine. |
| Hard phases | `spark-coder-hard` | `claude-opus-4-6` | 1M | Routed when phase Difficulty = hard. Phases 3, 4, 5, 8 in this plan. |
| Easy/Medium phases | `spark-coder-easy` | `claude-sonnet-4-6` | 1M | Routed when phase Difficulty = easy or medium. Phases 1, 2, 6, 7 in this plan. |
| Checkpoint | `spark-checkpoint` | `claude-opus-4-6` | 1M | Merge, test, local verify, inline fix (up to 3 attempts). Does NOT push or deploy. Owns `scripts/spark-smoke-artifact.sh`. |
| Code review | `spark-code-reviewer` | `claude-opus-4-6` | 1M | Reviews batch diff after a successful checkpoint; must pass before any deploy step. |
| Deploy-verify | `spark-deploy-verify` | `claude-opus-4-6` | 1M | Push, deploy, verify-deploy, smoke. Disabled for this feature -- Spark deploy pipeline is OFF; no deploy-verify dispatch. |
| Dedicated fix | `spark-fix` | `claude-opus-4-6` | 1M | Fresh-context fix after a checkpoint or review failure. Routes back to code review on FIX COMPLETE. |
| Tester | `spark-tester` | `claude-sonnet-4-6` | 1M | Disabled for this feature (agentic testing OFF). |
| Phase planner | `spark-planner` | `claude-opus-4-7` | 1M | Already executed during `/spark-setup` step 7.5 -- the 8 phase plans on disk are the output. |

---

## Cache Strategy

**Shared Prefix** (identical across all coding agents in a batch -- cached after the first agent):

- Universal coding-agent system prompt (~6k tokens; includes QUICKSTART references and the "no ARCHON restart" anti-pattern)
- `CODEBASE_CONTEXT.md` (~9k tokens)
- Cross-cutting concerns from `PROJECT_PLAN.md` (~3k tokens)
- Previous checkpoint summary (~2k tokens after Batch 1; grows ~1-2k per batch as cumulative context accretes)
- **Estimated shared prefix**: ~20k tokens for Batches 1-2, ~22k for Batches 3-4, ~24k for Batches 5-6.

**Per-Agent Suffix** (unique to each coding agent):

- Phase plan from `phase_plans/PHASE_XX.md` (~4-8k tokens per phase)
- Phase-specific dispatch instructions from the conductor (~1-2k)
- **Estimated per-agent suffix**: ~5-10k tokens.

**Note**: All agents in a parallel batch are spawned in a single message to maximize prompt cache hits. The shared prefix must be byte-identical across all agent prompts -- same content, same ordering, same whitespace.

---

## File Contention Analysis

> Phases that touch the same files must NOT be in the same parallel batch.
> This analysis drives batch grouping decisions.

| File / Directory | Phases That Touch It | Risk | Mitigation |
|-----------------|---------------------|------|------------|
| `xmpd/history_store.py` | P1 (create); P6 may extend (e.g. `existing_play_keys`, `add_plays_bulk`) | LOW | P6 lands in Batch 5, long after P1's Batch 1 -- extension is additive, no parallel conflict. |
| `xmpd/config.py` | P1 (extend `_DEFAULTS` + `_validate_config`) | NONE | Single-phase. |
| `xmpd/history_reporter.py` | P2 (extend `_report_track` + constructor) | NONE | Single-phase. |
| `xmpd/history_syncer.py` | P2 (create stub); P3 (replace stub bodies) | LOW | P3 follows P2 sequentially in Batch 3 (after Batch 2 merged) -- the stub interface is the contract P3 implements. |
| `xmpd/history_backfill.py` | P6 (create) | NONE | Single-phase. |
| `xmpd/daemon.py` | P2 (constructor + `run()`); P5 (`history-json` IPC handler); P6 (`history-backfill` IPC handler) | MEDIUM | P2 lands in Batch 2; P5 in Batch 4; P6 in Batch 5 -- all sequential batches. The dispatcher is appended-to in three different batches; planners place new code blocks adjacently to minimize merge friction. |
| `bin/xmpctl` | P5 (`history-json` subcommand); P6 (`history-backfill` subcommand) | MEDIUM | Sequential batches (P5 in Batch 4, P6 in Batch 5). P6's plan instructs the coder to add the new subcommand BELOW P5's `history-json` to minimize diff friction. |
| `bin/xmpd-history` | P5 (create) | NONE | Single-phase. |
| `bin/xmpd-doctor` | P7 (create) | NONE | Single-phase. P7 is parallel with P5 in Batch 4 but they own different files. |
| `scripts/xmpd-history-receiver` | P4 (create) | NONE | Single-phase. P4 is parallel with P3 in Batch 3 but they own different files. |
| `tests/conftest.py` | P1 (create with `history_store_temp`); P3 (extend with `mock_ssh_bidir`) | LOW | Sequential batches. P3 appends a new fixture; no overlap with P1's content. |
| `tests/test_history_store.py` | P1 (create) | NONE | Single-phase. |
| `tests/test_history_reporter.py` | P2 (EXTEND -- file already exists in the project) | NONE | Single-phase extension. |
| `tests/test_history_syncer.py` | P3 (create) | NONE | Single-phase. |
| `tests/test_xmpd_history_receiver.py` | P4 (create) | NONE | Single-phase. |
| `tests/test_history_backfill.py` | P6 (create) | NONE | Single-phase. |
| `tests/test_xmpd_history.py` | P5 (create -- the bash wrapper smoke test in pytest) | NONE | Single-phase. |
| `tests/test_xmpctl_history_json.py` | P5 (create or extend `tests/test_xmpctl.py`) | NONE | Single-phase. |
| `tests/test_xmpd_doctor.py` | P7 (create) | NONE | Single-phase. |
| `tests/test_daemon.py` | P1 (extend with config); P2 (extend wiring); P5 (extend with `history-json` IPC); P6 (extend with `history-backfill` IPC) | LOW | Sequential batches (Batch 1, 2, 4, 5). Each batch appends new test cases additively. |
| `tests/test_config.py` | P1 (extend) | NONE | Single-phase. |
| `tests/fixtures/sample_mpd_log` | P6 (create) | NONE | Single-phase. |
| `pyproject.toml` | P4 may extend (only if ruff/mypy needs to lint `scripts/`) | LOW | Single-phase if at all; P4 plan says "check first". |

---

## Runtime Contention Analysis

> Worktree isolation covers the filesystem only. Parallel agents share running services, databases, external APIs, and system state. Unmitigated runtime contention causes spurious test failures and flaky checkpoints.

| Resource | Type | Phases That Use It | Mitigation |
|----------|------|--------------------|------------|
| Live xmpd daemon on `[LIVE_HOST]` | service (singleton, port-bound) | P2, P5 may live-verify on a test peer; P8 exercises live multi-host | Universal anti-pattern: NEVER restart on `[LIVE_HOST]`. All live verification flips to `[TEST_HOST_1]` / `[TEST_HOST_2]` via the SSH heredoc + Syncthing-wait pattern in `scripts/spark-restart-peer.sh`. The daemon on each peer is a singleton there too -- only ONE phase touches a given peer at a time (sequential live-verification within the phase, not across parallel phases). |
| Live xmpd daemon on test peers (`[TEST_HOST_1]`, `[TEST_HOST_2]`) | service | P2, P3, P5, P6, P8 may exercise | Within a single batch, only one phase performs live verification at a time. Parallel batches (3, 4) do not require live verification: P3 mocks subprocess; P4 spawns a local subprocess in tests; P5 exercises a tmp-HOME daemon; P7 stubs ssh/sqlite3 via PATH. Phase 8 is the only phase that exercises the real cluster end-to-end and runs in its own batch (Batch 6). |
| WATCHTOWER aggregator DB (`~/xmpd-history/history.db` on WATCHTOWER) | database | P4 may write during deploy verification; P8 reads/writes via real bidir | Single-writer guarantees from receiver script (one ssh session per call). Parallel risk only if P3 and P4 in Batch 3 both reach the live WATCHTOWER -- they don't: P3 mocks subprocess; P4's "live deploy" step is gated on user confirmation and runs after the unit-test probe. Document in the phase plan: P4's WATCHTOWER write step runs LAST in the phase, with user approval. |
| Local SQLite history DB on each test host | database | P1, P3, P5, P6, P7, P8 unit tests use tmp_path; live tests on peers use `~/.config/xmpd/history.db` on the peer | Unit tests use pytest's `tmp_path` -- per-test isolation, no contention. Live tests on a peer use the peer's real DB; only one phase exercises a given peer's DB at a time within the phase boundary. |
| Tailscale daemon | external service | P3 reads `tailscale status --json` in tests (mocked); P7 reads it for the doctor probe (live); P8 may simulate offline | Tests mock the binary via PATH stubs. Live tests are read-only against the user's Tailscale state. Phase 8's offline simulation is gated on user confirmation per cautious posture. |
| SSH to WATCHTOWER | external service | P4 (deploy + version probe); P7 (doctor); P8 (real bidir verification) | Sequential phases (P4 in Batch 3, P7 in Batch 4, P8 in Batch 6) -- no parallel ssh-WATCHTOWER contention within a batch. |
| MPD on `[LIVE_HOST]` | service (singleton) | P6 backfill reads the MPD log path (read-only); P8 may issue `mpc` commands on test peers | Read-only on `[LIVE_HOST]` (just reading the log file path; no MPD command issued). On peers: only one phase issues `mpc` at a time. |
| Syncthing | system service | All live-verifying phases | Helper `scripts/spark-restart-peer.sh` enforces the wait-for-HEAD-match contract before any peer restart -- prevents stale-code restarts. |

No unmitigated runtime contention identified.

---

## Batch Schedule

| Batch | Phases | Mode | Checkpoint Deploy | Checkpoint Verify | Testing |
|-------|--------|------|-------------------|-------------------|---------|
| 1 | Phase 1 (HistoryStore Foundation + Config) | sequential | No (deploy disabled) | `pytest tests/test_history_store.py tests/test_config.py` clean; `mypy xmpd/history_store.py xmpd/config.py` clean; `ruff check .` clean; `tests/conftest.py` exports `history_store_temp` | No (agentic testing disabled) |
| 2 | Phase 2 (HistoryReporter Wire-Up + Syncer Stub) | sequential | No | `pytest tests/test_history_reporter.py tests/test_daemon.py` clean; daemon constructs HistoryStore + stub HistorySyncer + executor when `history.enabled=true` | No |
| 3 | Phase 3 (HistorySyncer real impl), Phase 4 (Receiver script + WATCHTOWER deploy) | parallel | No | `pytest tests/test_history_syncer.py tests/test_xmpd_history_receiver.py` clean; receiver `version` returns `schema=1\nprotocol=1` from local invocation AND from WATCHTOWER (after deploy step) | No |
| 4 | Phase 5 (xmpctl history-json + bin/xmpd-history), Phase 7 (bin/xmpd-doctor) | parallel | No | `pytest tests/test_xmpd_history.py tests/test_xmpctl_history_json.py tests/test_xmpd_doctor.py tests/test_daemon.py` clean; `bash -n bin/xmpd-history bin/xmpd-doctor` clean | No |
| 5 | Phase 6 (xmpctl history-backfill) | sequential | No | `pytest tests/test_history_backfill.py tests/test_daemon.py` clean; idempotency rerun returns `inserted=0` on second invocation; dry-run produces no DB writes | No |
| 6 | Phase 8 (Integration Testing on Test Peers) | sequential | No | `docs/agent/xmpd-history/INTEGRATION_TEST_REPORT.md` exists with pass/fail per loop and pasted evidence; `xmpd-doctor` exits 0 on both `[TEST_HOST_1]` and `[TEST_HOST_2]` post-fix; all regression tests added during the phase pass | No |

**Smoke artifact tier** (`scripts/spark-smoke-artifact.sh`) runs at every checkpoint -- import probe + receiver `version` + bash-syntax probe. Skip-able when no surface-touching changes.

**Smoke deploy tier**: disabled (Smoke Deploy Tier is rejected for cli surface; deploy pipeline is also disabled).

**Testing column**: all `No` because agentic testing is disabled feature-wide. Coding agents perform their own Functional QA per the phase plan.

---

## Batch Details

### Batch 1: HistoryStore Foundation

**Mode**: sequential
**Rationale**: Foundation phase. Everything downstream depends on the HistoryStore module, the `history:` config block, and the `history_store_temp` fixture in `tests/conftest.py`. Critical batch -- failure here blocks all subsequent work.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 1 | HistoryStore Foundation + Config | medium | spark-coder-easy | ~85k | Mirrors `xmpd/track_store.py` pattern. Schema migrations via `PRAGMA user_version`. |

**Checkpoint**:
- **Deploy**: No -- deploy pipeline disabled feature-wide.
- **Verify**: HistoryStore unit tests pass (12+ cases); `tests/conftest.py` exports `history_store_temp`; `xmpd/config.py` accepts the new `history:` block; mypy + ruff clean.
- **Critical**: Yes -- foundation for all subsequent phases.
- **Testing**: No.

### Batch 2: HistoryReporter Wire-Up

**Mode**: sequential
**Rationale**: Phase 2 wires HistoryStore + a stub HistorySyncer into the existing HistoryReporter and the daemon constructor. Cannot run in parallel with Phase 1 (depends on it). Cannot batch with later phases (Phase 3 replaces the stub HistorySyncer body in the same module Phase 2 creates).

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 2 | HistoryReporter Wire-Up + Syncer Stub | medium | spark-coder-easy | ~65k | Extends existing `xmpd/history_reporter.py` and `xmpd/daemon.py`; creates stub `xmpd/history_syncer.py`. |

**Checkpoint**:
- **Deploy**: No.
- **Verify**: `_report_track` calls `add_play` and submits `bidir_push` to the executor on a successful provider report; existing provider report contract regression-tested; daemon constructs HistoryStore + stub HistorySyncer + executor when `history.enabled=true`.
- **Critical**: Yes -- subsequent phases extend or replace this scaffolding.
- **Testing**: No.

### Batch 3: Syncer Real + Receiver Script (parallel)

**Mode**: parallel
**Rationale**: Phase 3 (HistorySyncer real impl) and Phase 4 (Receiver script + WATCHTOWER deploy) both depend on Phase 2 but are independent of each other at the code level: P3 owns `xmpd/history_syncer.py` + tests; P4 owns `scripts/xmpd-history-receiver` + tests. NDJSON wire format is the contract between them, defined in PROJECT_PLAN.md Data Schemas. P3 mocks the receiver in tests; P4 verifies real subprocess behavior. No file contention. Runtime contention bounded: P4's WATCHTOWER deploy step runs last in the phase with user approval; P3 only mocks subprocess.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 3 | HistorySyncer Real Implementation | hard | spark-coder-hard | ~85k | Tailscale precheck + ssh subprocess + NDJSON + single-flight lock. |
| 4 | Receiver Script + WATCHTOWER Deploy | hard | spark-coder-hard | ~85k | Stdlib-only Python 3 receiver. WATCHTOWER deploy via scp + chmod (cautious: ASK USER). |

**Checkpoint**:
- **Deploy**: No.
- **Verify**: HistorySyncer test cases (10+) pass including failure paths; receiver round-trip tests pass via subprocess; live `~/bin/xmpd-history-receiver version` on WATCHTOWER returns `schema=1\nprotocol=1`; `tests/conftest.py` now exports `mock_ssh_bidir`.
- **Critical**: Yes -- Phase 5/6/8 require both pieces.
- **Testing**: No.

### Batch 4: xmpctl + Wrapper + Doctor (parallel)

**Mode**: parallel
**Rationale**: Phase 5 (xmpctl history-json + bin/xmpd-history) and Phase 7 (bin/xmpd-doctor) both depend on Phase 3 + Phase 4. They own non-overlapping files: P5 extends `xmpd/daemon.py` + `bin/xmpctl` and creates `bin/xmpd-history`; P7 creates `bin/xmpd-doctor` only. No file contention. Runtime contention bounded: P5's tests use tmp HOME for the daemon; P7's tests stub all externals via PATH.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 5 | xmpctl history-json + bin/xmpd-history | hard | spark-coder-hard | ~95k | New daemon IPC handler + xmpctl subcommand + fzf wrapper. |
| 7 | bin/xmpd-doctor | medium | spark-coder-easy | ~70k | Bash healthcheck script with structured stdout sections; exits 0/2/1. |

**Checkpoint**:
- **Deploy**: No.
- **Verify**: `xmpctl history-json --format json` returns valid NDJSON against a seeded daemon; `bin/xmpd-history` initial reload command matches expectation; `xmpd-doctor` renders all sections and exits per the green/yellow/red matrix in test scenarios.
- **Critical**: No -- a failure here doesn't block Phase 6's standalone work, but Phase 8 needs both before it can validate Loop C and Loop E.
- **Testing**: No.

### Batch 5: Backfill

**Mode**: sequential
**Rationale**: Phase 6 (xmpctl history-backfill) extends `bin/xmpctl` and `xmpd/daemon.py`, both touched by Phase 5 in Batch 4. Sequential isolation prevents merge conflicts at the dispatcher and CLI subcommand boundaries.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 6 | xmpctl history-backfill | medium | spark-coder-easy | ~75k | New `xmpd/history_backfill.py` module + IPC handler + CLI subcommand. |

**Checkpoint**:
- **Deploy**: No.
- **Verify**: `pytest tests/test_history_backfill.py` passes; idempotency rerun returns `inserted=0`; dry-run produces no DB writes; auto-detect of mpd.conf paths works for the standard locations.
- **Critical**: No -- backfill is a one-shot operator command; failure doesn't block Phase 8's runtime loops, only Loop D.
- **Testing**: No.

### Batch 6: Integration Testing

**Mode**: sequential
**Rationale**: Phase 8 (Integration Testing on Test Peers) exercises the entire feature end-to-end on `[TEST_HOST_1]` and `[TEST_HOST_2]`. Coordination across multiple peers, real ssh, real Tailscale, real WATCHTOWER aggregator. Cannot parallelize with anything else.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 8 | Integration Testing on Test Peers | hard | spark-coder-hard | ~70k | Loops A-E from FUNCTIONAL_QA_STRATEGY.md. Surgical fixes + regression tests for any bug found. Helper: `scripts/spark-restart-peer.sh`. |

**Checkpoint**:
- **Deploy**: No.
- **Verify**: `INTEGRATION_TEST_REPORT.md` written with pass/fail per loop; all five loops attempted; `xmpd-doctor` exits 0 on both test peers; any added regression tests pass.
- **Critical**: Yes -- this is the gate. Nothing ships unless the loops pass.
- **Testing**: No.

---

## Dependency Graph

```
=== Batch 1 (sequential) ===
Phase 1 (medium, easy-coder)
  |
--- Checkpoint 1 ---
  |
=== Batch 2 (sequential) ===
Phase 2 (medium, easy-coder)
  |
--- Checkpoint 2 ---
  |
=== Batch 3 (parallel) ===
Phase 3 (hard, hard-coder) --+
Phase 4 (hard, hard-coder) --+--> merge
  |
--- Checkpoint 3 ---  ====== AUTO-REFRESH BOUNDARY (3 batches/session) ======
  |
=== Batch 4 (parallel) ===
Phase 5 (hard, hard-coder)   --+
Phase 7 (medium, easy-coder)  --+--> merge
  |
--- Checkpoint 4 ---
  |
=== Batch 5 (sequential) ===
Phase 6 (medium, easy-coder)
  |
--- Checkpoint 5 ---
  |
=== Batch 6 (sequential) ===
Phase 8 (hard, hard-coder)
  |
--- Checkpoint 6 (final) ---
```

---

## Conductor Pacing

- **Mode**: auto-refresh
- **Batches Per Session**: 3

Session 1 runs Batches 1-3 (Foundation -> Wire-up -> Syncer + Receiver in parallel). Session 2 runs Batches 4-6 (CLI + Doctor in parallel -> Backfill -> Integration Testing). The user restarts `/spark-conductor xmpd-history` between sessions; the new session resumes from STATUS.md's Current Batch automatically.

The auto-refresh boundary lands AFTER Batch 3, which is the first parallel batch and the largest token consumer (two hard phases dispatched together with shared cache). Forcing a fresh context window before Batch 4 keeps prompt cache pressure manageable for the second parallel batch.

---

## Fix Strategy

- **Max inline fix attempts per checkpoint**: 3
- **Inline fix**: `spark-checkpoint` itself attempts fixes (it has full merge context).
- **Dedicated fix subagent**: `spark-fix` (fresh context, claude-opus-4-6 pinned).
- **Escalation path**: 3 inline fixes -> dedicated fix subagent -> human intervention.
- **Fix scope rules**:
  - Localized failure (one file, clear cause, <50 lines): inline fix in checkpoint.
  - Systemic failure (architectural incompatibility, missing interfaces): skip inline, dispatch `spark-fix` immediately.
  - Fix outcomes are appended to the checkpoint summary.

---

## Agentic Testing Configuration

Agentic testing: disabled. No `spark-tester` dispatch in any batch. Coding agents perform their own Functional QA against the surfaces named in their phase plans, capturing byte-for-byte evidence in the phase summary.

---

## Notes

- **Live verification multi-host inversion**: the user is actively listening on `[LIVE_HOST]` (the dev machine), so live verification is forbidden there. Tests run locally on `[LIVE_HOST]` against tmp HOME / tmp DBs. Anything that needs a real running daemon flips to `[TEST_HOST_1]` or `[TEST_HOST_2]` after Syncthing replicates code (~60s). The `scripts/spark-restart-peer.sh` helper enforces the wait-for-HEAD-match contract and is mandatory for every peer restart in Phase 8.
- **WATCHTOWER deploy step in Phase 4**: the receiver script lands on WATCHTOWER via `scp` + `chmod +x`. Cautious safety posture means the coder MUST ASK USER before each remote write. Plan accordingly.
- **No deploy pipeline**: Spark deploy is OFF for this feature. Code propagation to test peers is via Syncthing (out-of-band). The WATCHTOWER receiver deploy is handled inline by Phase 4's plan, not by `spark-deploy.sh`.
- **Smoke artifact runs always**: `scripts/spark-smoke-artifact.sh` is invoked by `spark-checkpoint` at every checkpoint. Skip-able when no surface-touching changes.
- **Borderline phases**:
  - Phase 5 was rated `hard` because of the three-surface scope (daemon IPC + xmpctl subcommand + bash fzf wrapper) and the muscle-memory binding requirement. If the planner's plan reads as straightforward at coder time, the difficulty could drop to medium -- but the assignment stays as-is for now.
  - Phase 8 was rated `hard` because of live multi-host coordination + judgment about what counts as a bug worth fixing inline vs. escalating. The actual code volume is small.
- **Suggested human review points**:
  - After Checkpoint 1 (foundation correctness anchors everything downstream).
  - After Checkpoint 3 (NDJSON wire format consistency between Phase 3 mocks and Phase 4 real).
  - After Checkpoint 4 (fzf binding strings + xmpctl IPC integration -- user-visible UX).
  - After Checkpoint 6 (final gate -- review INTEGRATION_TEST_REPORT.md before declaring victory).
- **Phase 4 helper question**: Phase 4 might want to propose `scripts/spark-deploy-receiver.sh` if the receiver gets redeployed during Phase 8 fix-cycles. Surface during Checkpoint 3 if so.
