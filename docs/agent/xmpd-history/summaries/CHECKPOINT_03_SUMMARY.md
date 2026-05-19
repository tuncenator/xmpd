# Checkpoint 3: Post-Batch 3 Summary

**Date**: 2026-05-13
**Batch**: 3 -- HistorySyncer Real Impl + Receiver Script + WATCHTOWER Deploy
**Phases Merged**: Phase 3 (HistorySyncer Real Implementation), Phase 4 (Receiver Script + WATCHTOWER Deploy)
**Result**: PASSED WITH FIXES

---

## Merge Results

| Phase | Branch | Merge Status | Conflicts |
|-------|--------|-------------|-----------|
| 3 | phase-3-historysyncer-real-implementation | Clean | None |
| 4 | phase-4-receiver-script-watchtower-deploy | Clean | None |

---

## Test Results

```
$ .venv/bin/python -m pytest --tb=no -q
14 failed, 1200 passed, 13 skipped in 40.36s
```

- **Total tests**: 1227
- **Passed**: 1200
- **Failed**: 14 (all pre-existing)
- **Skipped**: 13

### Failed Tests

All 14 failures are pre-existing (present before this batch). No new regressions. Count increased from 1203 to 1227 total (24 new tests from Phases 3+4).

| Test | Error | Likely Cause | Phase |
|------|-------|-------------|-------|
| test_xmpd_status_integration (4 tests) | AssertionError: fixture mismatch | Pre-existing | N/A |
| test_like_toggle (3 tests) | Pre-existing failures | like_toggle module API changes | N/A |
| test_search_json (4 tests) | Pre-existing failures | search_json module API changes | N/A |
| test_xmpd_status (3 tests) | Pre-existing failures | xmpd_status module API changes | N/A |

---

## Deployment Results

> Spark deploy pipeline is disabled for this feature -- code propagates to
> STORMTREE/VICAR via Syncthing; the receiver script reaches WATCHTOWER via the
> receiver phase's inline `scp` step. This section is N/A for normal checkpoints.

WATCHTOWER receiver deploy was performed inline by Phase 4 (not via spark-deploy.sh); see PHASE_04_SUMMARY.md for the full deploy evidence (scp, chmod, version smoke on absolute and bare PATH).

- **Deployed**: `scripts/xmpd-history-receiver` to `WATCHTOWER:~/bin/` (Phase 4, inline)
- **Host**: WATCHTOWER (Debian 12, Python 3.11.2, sqlite3 3.40.1)
- **Commit**: 6c144f3 (Phase 4 implementation commit)
- **Service Status**: N/A (receiver is a one-shot SSH command, not a daemon)
- **Restart Method**: N/A

### Log Observations

N/A

---

## Verification Results

| Phase | Criterion | Status | Notes |
|-------|----------|--------|-------|
| 3+4 | `uv run pytest tests/test_history_syncer.py tests/test_xmpd_history_receiver.py -xvs` exits 0 with 24 tests green | Pass | 24 passed in 0.91s (13 + 11) |
| 4 | `python3 scripts/xmpd-history-receiver version` returns `schema=1\nprotocol=1` | Pass | Local invocation confirmed |
| 4 | `ssh WATCHTOWER ~/bin/xmpd-history-receiver version` returns `schema=1\nprotocol=1` | Pass | Verified via SSH heredoc |
| 3 | `tests/conftest.py` exports BOTH `history_store_temp` and `mock_ssh_bidir` | Pass | Line 28 and line 41 respectively |
| 3 | `uv run mypy xmpd/history_syncer.py` clean | Pass | `Success: no issues found in 1 source file` |
| 3+4 | `uv run ruff check` clean for batch files | Pass | `All checks passed!` |
| 3+4 | `uv run ruff format --check` clean for batch files | Pass | `4 files already formatted` (after fix) |
| ALL | Full regression: 14 pre-existing failures persist, nothing else regresses | Pass | 1227 total, 14 failed (same 4 test files), 13 skipped |

### Verification Details

ruff format: `tests/test_xmpd_history_receiver.py` required formatting (trailing comma wrapping). Fixed in commit `10cc88e`. All other files already formatted.

---

## Artifact Smoke

- **Status**: PASS
- **Command run**: `scripts/spark-smoke-artifact.sh xmpd/history_syncer.py scripts/xmpd-history-receiver tests/conftest.py tests/test_history_syncer.py tests/test_xmpd_history_receiver.py`
- **Surfaces probed**: history_modules (imported `xmpd.history_store`, `xmpd.history_syncer`, `xmpd.history_reporter`), receiver_script (`python3 scripts/xmpd-history-receiver version`)
- **Failure detail (if any)**: none
- **Fix attempts (if any)**: none needed

---

## Deploy Smoke

> Smoke Deploy Tier is disabled for this feature (CLI surface). Skip this section.

---

## Helper Repairs

No helpers needed repair. Neither phase reported helper issues. No unlisted helpers attempted.

---

## Code Review Results

- **Result**: REVIEW PASSED WITH NOTES
- **Reviewer**: spark-code-reviewer (claude-opus-4-6)
- **Diff range**: `e6664a9..bbae309`

All key invariants verified: frozen HistorySyncer `__init__` signature; single-flight lock; Tailscale precheck 5 failure modes; defensive `Peer: null` in syncer; NDJSON framing + `stdin.close()`; failure isolation (no state changes on rc != 0); success-only state updates (`mark_synced` + `insert_remote_rows` + cursor advance only when increasing); self-host from row's own field; defensive parse skip for rows missing `server_id`; BrokenPipeError/OSError on stdin write fall through. Receiver: stdlib-only (no xmpd.* import); stdout=wire; ON CONFLICT (host, local_id) DO NOTHING idempotency; `--as` -> `dest='as_'`; `_now_iso()` with offset; exit code trichotomy 0/1/2; `received_at` once per push; tailscale_peers graceful degradation. Wire format consistency: Phase 3's 12 push keys map exactly to Phase 4's INSERT column list; receiver emits 14 keys on pull (12 + server_id + received_at); syncer parser requires server_id and skips rows missing it. No file overlap between Phase 3 and Phase 4. Security clean. Format-only commit verified as trailing-comma adjustments only.

### Issues (4 minor, non-blocking)

| Severity | Location | Issue |
|----------|----------|-------|
| Minor | `scripts/xmpd-history-receiver` line 232 (cmd_doctor) | Missing `Peer: null` defensive guard (`ts_data.get("Peer", {})` would be None if Tailscale returns null). Syncer has the `... or {}` guard; receiver doesn't. The outer try/except catches the AttributeError and surfaces as `tailscale_error`, so low-probability blast radius, but inconsistent with syncer's pattern. |
| Minor | `scripts/xmpd-history-receiver` line 17 | Unused `import socket`. Dead import; no functional impact. |
| Minor | `tests/test_history_syncer.py` line 405 | Inline `import time` inside `test_bidir_coalesces_concurrent_calls` instead of top-level import. Cosmetic. |
| Minor | `scripts/xmpd-history-receiver` line 35 | `open_db` is the only public-named (no `_` prefix) helper alongside `_apply_migrations`, `_create_schema_v1`, `_now_iso`, `_parse_args`. Inconsistent naming, but receiver is never imported as a module. |

These are non-blocking and can be addressed opportunistically in a later phase.

---

## Fix Cycle History

| Attempt | Type | Target | Description | Result |
|---------|------|--------|-------------|--------|
| 1 | inline | `tests/test_xmpd_history_receiver.py` | Applied `ruff format` (trailing comma wrapping) | Success |

### Fix Details

Phase 4's test file had minor formatting deviations from ruff's style. Applied `ruff format`, re-ran 24 tests (all pass), re-verified format check (4 files already formatted).

---

## Functional QA Evidence Check

**Phase 3** (`Functional: yes`): 7 checks in PHASE_03_SUMMARY.md, matching the 7 items in the phase plan's Functional QA section. Each contains surface, invocation command, pasted observed output, and pass/fail verdict. Outputs are concrete (test runner lines, NDJSON payloads, sqlite3 results, log captures). No vague entries.

**Phase 4** (`Functional: yes`): 7 checks in PHASE_04_SUMMARY.md, matching the 7 items in the phase plan's Functional QA section. Each contains surface, invocation command, pasted observed output, and pass/fail verdict. Includes local subprocess tests, WATCHTOWER SSH heredoc commands, and SQL captures. No vague entries.

**Visual QA**: Both phases have `Visual: no`. N/A.

**Illegitimate deferral check**: Neither summary defers any criterion that could have been verified locally. Phase 4's WATCHTOWER deploy was performed and verified inline. Pass.

---

## Codebase Context Updates

### Added

- `scripts/xmpd-history-receiver`: stdlib-only Python 3 receiver script. Subcommands: bidir, doctor, version. Aggregator DB at `~/xmpd-history/history.db`. Schema v1 with `server_id AUTOINCREMENT`, `(host, local_id) UNIQUE`. Deployed to WATCHTOWER.
- `tests/test_history_syncer.py`: 13 tests in 5 classes covering precheck, wire format, single-flight, failure paths, nudge.
- `tests/test_xmpd_history_receiver.py`: 11 subprocess-based tests using inline `_run_receiver()` helper.
- WATCHTOWER deployment: receiver at `~/bin/xmpd-history-receiver`, both absolute and bare PATH invocation work. Python 3.11.2, sqlite3 3.40.1.

### Modified

- `xmpd/history_syncer.py`: stub replaced with real implementation. Public API unchanged. New private: `_tailscale_online()`, `_run_bidir()`. Constants: `PROTOCOL_VERSION=1`, `TAILSCALE_TIMEOUT_SECONDS=5`, `SSH_TIMEOUT_SECONDS=30`, `RECEIVER_STDERR_TRUNCATE=200`, `_WIRE_KEYS`.
- `tests/conftest.py`: extended with `mock_ssh_bidir` fixture factory and `_UnclosableBytesIO` helper.

### Removed

None.

---

## Notes for Next Batch

- Phase 5 (xmpctl history-json + bin/xmpd-history): real bidir sync is now functional, so cross-host rows can flow into the local DB for the fzf wrapper to display.
- Phase 6 (xmpctl history-backfill): the post-commit `bidir_push` triggered by backfill will actually sync now.
- Phase 7 (bin/xmpd-doctor): `last_received_server_id` and `synced_at` fields are populated by the real syncer; doctor can read them.
- Phase 8 (integration testing): both sides of the bidir protocol are now implemented and tested independently. Integration tests can exercise real round-trips.
- The receiver on WATCHTOWER can be re-deployed via `scp scripts/xmpd-history-receiver WATCHTOWER:~/bin/xmpd-history-receiver` (already executable).
- Pre-existing mypy errors (49 in unrelated files) and 14 pre-existing test failures unchanged.

---

## Status After Checkpoint

- **All phases in batch**: PASSED WITH FIXES (formatting only)
- **Cumulative project progress**: 50% (4/8 phases complete)
- **Ready for next batch**: Yes
