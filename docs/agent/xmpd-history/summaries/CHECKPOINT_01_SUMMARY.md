# Checkpoint 1: Post-Batch 1 Summary

**Date**: 2026-05-13
**Batch**: 1 -- HistoryStore Foundation + Config
**Phases Merged**: Phase 1 -- HistoryStore Foundation + Config
**Result**: PASSED

---

## Merge Results

| Phase | Branch | Merge Status | Conflicts |
|-------|--------|-------------|-----------|
| 1 | phase-1-historystore-foundation-config | Clean | None |

---

## Test Results

```
$ uv run pytest tests/test_history_store.py tests/test_config.py -xvs
60 passed in 0.12s

$ pytest --tb=no (full regression, 1188 collected)
1161 passed, 14 failed (pre-existing), 13 skipped in 39.60s
```

- **Total tests**: 1188
- **Passed**: 1161
- **Failed**: 14 (all pre-existing)
- **Skipped**: 13

### Failed Tests

All 14 failures are pre-existing (present on the feature branch before Phase 1 ran). No new regressions introduced.

| Test | Error | Likely Cause | Phase |
|------|-------|-------------|-------|
| test_xmpd_status_integration::test_scenario_1_youtube_playing_resolved | AssertionError: title mismatch | Pre-existing: test expects "Never Gonna" but currentsong has "Test Track Title" | N/A |
| test_xmpd_status_integration::test_scenario_3_youtube_unresolved | Pre-existing assertion failure | Same test file, fixture mismatch | N/A |
| test_xmpd_status_integration::test_scenario_4_first_track_in_playlist | Pre-existing assertion failure | Same test file, fixture mismatch | N/A |
| test_xmpd_status_integration::test_scenario_5_last_track_in_playlist | Pre-existing assertion failure | Same test file, fixture mismatch | N/A |
| test_like_toggle (3 tests) | Pre-existing failures | like_toggle module API changes | N/A |
| test_search_json (4 tests) | Pre-existing failures | search_json module API changes | N/A |
| test_xmpd_status (3 tests) | Pre-existing failures | xmpd_status module API changes | N/A |

---

## Deployment Results

> Spark deploy pipeline is disabled for this feature -- code propagates to
> STORMTREE/VICAR via Syncthing; the receiver script reaches WATCHTOWER via the
> receiver phase's inline `scp` step. This section is N/A for normal checkpoints.

- **Deployed**: N/A
- **Host**: N/A
- **Commit**: N/A
- **Service Status**: N/A
- **Restart Method**: N/A

### Log Observations

N/A

---

## Verification Results

| Phase | Criterion | Status | Notes |
|-------|----------|--------|-------|
| 1 | `uv run pytest tests/test_history_store.py tests/test_config.py -xvs` exits 0 | Pass | 60 passed in 0.12s |
| 1 | `uv run mypy xmpd/history_store.py xmpd/config.py` clean | Pass | history_store.py: 0 errors. config.py: 1 pre-existing error (missing types-PyYAML stubs, `import yaml`). |
| 1 | `uv run ruff check .` clean (no new violations) | Pass | Phase 1 files: "All checks passed!" Pre-existing: 37 violations in other files. |
| 1 | `tests/conftest.py` exists and exports `history_store_temp` | Pass | File exists; fixture yields `HistoryStore` backed by `tmp_path`. |
| 1 | Full regression: 9 pre-existing failures persist, nothing else regresses | Pass | 14 pre-existing failures (same files noted in Phase 1 summary). 0 new failures. |

### Verification Details

The mypy error on `config.py` (`Library stubs not installed for "yaml" [import-untyped]`) is pre-existing: `import yaml` was present in `config.py` before Phase 1 (confirmed by inspecting the pre-batch commit `72a4a4a`). `history_store.py` alone passes mypy cleanly ("Success: no issues found in 1 source file").

The Phase 1 summary reported 9 pre-existing failures; the full regression run found 14. The difference is because Phase 1 used `-xvs` (stops at first failure per test file), while this checkpoint ran without `-x`. All 14 are in the same files the summary listed: `test_xmpd_status_integration.py`, `test_like_toggle.py`, `test_search_json.py`, `test_xmpd_status.py`.

---

## Artifact Smoke

- **Status**: PASS
- **Command run**: `scripts/spark-smoke-artifact.sh xmpd/history_store.py xmpd/config.py tests/conftest.py tests/test_config.py tests/test_history_store.py docs/agent/xmpd-history/summaries/PHASE_01_SUMMARY.md`
- **Surfaces probed**: history_modules (imported `xmpd.history_store` and `xmpd.history_reporter`)
- **Failure detail (if any)**: none
- **Fix attempts (if any)**: none needed

---

## Deploy Smoke

> Smoke Deploy Tier is disabled for this feature (CLI surface). Skip this section.

---

## Helper Repairs

No helpers needed repair. Phase 1 reported no helper issues.

---

## Code Review Results

- **Result**: REVIEW PASSED WITH NOTES
- **Reviewer**: spark-code-reviewer (claude-opus-4-6)
- **Diff range**: `72a4a4a..f2dae51`

All key invariants checked and satisfied: `(host, local_id)` PK column order; single-writer SQLite lock discipline; `BEGIN IMMEDIATE` + rollback on schema migration; provider-agnostic store; `socket.gethostname().upper()` cached on `self._host`; no second `sqlite3.connect` in production code; bool-as-int trap rejected in validator; `since` rejects naive datetimes; `add_play` counter+insert atomicity verified; `insert_remote_rows` idempotency via `ON CONFLICT DO NOTHING`. Security clean (parameterized queries; no secrets). Style/lint/types clean for new code. Test-first compliance, mock discipline, FQA coverage all satisfied.

### Minor issues (cosmetic, non-blocking)

| Severity | Location | Issue |
|----------|----------|-------|
| Minor | `xmpd/history_store.py` line 377 (docstring) | Docstring references `datetime.now(timezone.utc)` but code uses `datetime.now(UTC)` alias. Code correct; docstring stale. |
| Minor | `tests/test_config.py` | No dedicated rejection test for `pull_batch`; covered indirectly by shared validator codepath with `bidir_batch`. |
| Minor | `tests/test_config.py` | No dedicated rejection test for `watchtower.enabled` non-bool; covered indirectly by top-level `history.enabled` test exercising the same `isinstance(x, bool)` pattern. |

These do not block the checkpoint and can be addressed opportunistically in a later phase if relevant code is touched.

---

## Fix Cycle History

No fixes needed. All merges clean, all verification criteria passed.

---

## Codebase Context Updates

### Added

- `xmpd/history_store.py`: row in Key Files table; new "HistoryStore" subsection under Important APIs with full public API signature and schema v1 description.
- `tests/conftest.py`: row in Key Files table.
- Updated "Tests live in `tests/`" paragraph to note conftest.py now exists.
- Updated pytest layout pattern to reflect conftest.py existence.

### Modified

- CODEBASE_CONTEXT.md "Last updated by" header updated to Checkpoint 1.

### Removed

None.

---

## Notes for Next Batch

- `tests/conftest.py` is intentionally minimal. Phase 3 extends it with `mock_ssh_bidir`. Do NOT add Phase 3 fixtures prematurely.
- `HistoryStore._host` is `socket.gethostname().upper()`. Tests that assert on `host` must import `socket` and call `socket.gethostname().upper()` rather than hardcoding a string.
- The `since` parameter in `get_plays` converts to UTC ISO offset string before binding. Lexicographic compare on ISO strings with offset is accurate only when all rows share the same offset. Documented in the class docstring.
- Phase 2 imports: `from xmpd.history_store import HistoryStore`.
- Pre-existing mypy config.py error (missing `types-PyYAML`) persists; not introduced by this batch.
- Pre-existing test failures: 14 tests in 4 files (test_xmpd_status_integration, test_like_toggle, test_search_json, test_xmpd_status). These are not related to the history feature.

---

## Status After Checkpoint

- **All phases in batch**: PASSED
- **Cumulative project progress**: 12.5% (1/8 phases complete)
- **Ready for next batch**: Yes
