# Phase 02: YT module relocation + YTMusicProvider scaffold - Summary

**Date Completed:** 2026-04-27
**Completed By:** claude-sonnet-4-6
**Actual Token Usage:** ~12k

---

## Objective

Move `xmpd/ytmusic.py` and `xmpd/cookie_extract.py` into the new `xmpd/providers/` and `xmpd/auth/` packages, update all import sites, prepend a `YTMusicProvider` scaffold class, wire it into `build_registry`, fix the `ytmpd_cookies_` prefix, and add 4 scaffold tests.

---

## Work Completed

### What Was Built

- Relocated `xmpd/ytmusic.py` -> `xmpd/providers/ytmusic.py` via `git mv` (history preserved).
- Relocated `xmpd/cookie_extract.py` -> `xmpd/auth/ytmusic_cookie.py` via `git mv` (history preserved).
- Updated all import sites: 13 files changed (xmpd/ source + tests/ + tests/integration/).
- Updated string-form `@patch("xmpd.ytmusic.*")` references in 4 test files.
- Added `YTMusicProvider` scaffold class to `xmpd/providers/ytmusic.py` with `name`, `is_enabled`, `is_authenticated`, `_ensure_client`.
- Rewrote `xmpd/providers/__init__.py` to Phase 2 spec: lazy-import YTMusicProvider when `yt` enabled; tidal branch as TODO comment; preserved `__all__` re-exports from Phase 1.
- Fixed `prefix="ytmpd_cookies_"` -> `prefix="xmpd_cookies_"` in `xmpd/auth/ytmusic_cookie.py`.
- Created `tests/test_providers_ytmusic.py` with 4 scaffold tests.
- Updated `tests/test_providers_registry.py` to reflect Phase 2 registry behavior (build_registry now returns `{"yt": ...}` when yt enabled).

### Files Created

- `xmpd/providers/ytmusic.py` -- relocated from `xmpd/ytmusic.py` + YTMusicProvider scaffold prepended
- `xmpd/auth/ytmusic_cookie.py` -- relocated from `xmpd/cookie_extract.py` + prefix fix
- `tests/test_providers_ytmusic.py` -- 4 scaffold tests for YTMusicProvider

### Files Modified

- `xmpd/providers/__init__.py` -- Phase 2 build_registry wiring, preserved __all__
- `xmpd/daemon.py` -- import path updated
- `xmpd/history_reporter.py` -- import path updated
- `xmpd/sync_engine.py` -- import path updated
- `tests/test_providers_registry.py` -- updated Phase 1 empty-registry test to Phase 2 reality
- `tests/test_ytmusic.py`, `tests/test_ytmusic_rating.py`, `tests/test_ytmusic_history.py` -- import + patch string updates
- `tests/test_auto_auth_daemon.py` -- patch string updates
- `tests/test_like_indicator.py`, `tests/test_sync_engine.py`, `tests/test_cookie_extract.py` -- import updates
- `tests/integration/test_auto_auth.py`, `tests/integration/test_full_workflow.py`, `tests/integration/test_rating_workflow.py` -- import + patch string updates

### Key Design Decisions

- `get_enabled_provider_names` in Phase 2 uses insertion order (iterates `("yt", "tidal")`) instead of Phase 1's sorted output. Updated `test_providers_registry.py` to assert set membership rather than ordered list for the both-enabled case.
- Preserved `__all__` re-exports (`Playlist`, `Provider`, `Track`, `TrackMetadata`) from Phase 1 -- Phase 2 spec said to note or preserve; preserved.
- `YTMusicProvider.is_authenticated` returns `bool` (not `tuple[bool, str]` like `YTMusicClient.is_authenticated`) -- matches Provider Protocol signature from Phase 1 base.py.
- Added `# type: ignore[assignment]` on registry line with comment explaining Phase 3 fixes it.
- Worktree did not have Phase 1 commits; merged `feature/tidal-init` with `--no-ff` before starting.

---

## Evidence Captured

No external interfaces consumed. Pure refactor phase.

---

## Completion Criteria Status

- [x] `git mv` performed on both files -- Verified: `git log --oneline --diff-filter=R -1` shows both renames in the single commit.
- [x] grep for legacy import paths returns zero lines -- Verified: `grep -rn "from xmpd\.ytmusic|import xmpd\.ytmusic|..."` returned empty.
- [x] All four `python -c` smoke imports exit 0 -- Verified: all 4 print their success message.
- [x] `pytest -q` passes (2 pre-existing status-widget failures unchanged) -- Verified: `2 failed, 646 passed, 4 skipped`.
- [x] `pytest tests/test_providers_ytmusic.py -v` -- 4 tests pass -- Verified: `4 passed in 0.10s`.
- [x] `grep -rn "ytmpd_cookies_" xmpd/ tests/` returns zero lines -- Verified: empty output.
- [x] `git log --oneline --diff-filter=R` shows the two renames -- Verified: commit `6b44f18` shown.
- [x] Single commit covers all changes -- Verified: single commit `6b44f18`.
- [x] `ruff check xmpd/ tests/` -- no NEW lint errors -- Verified: Phase 2 files all pass; pre-existing errors unchanged.
- [x] Phase summary at `docs/agent/tidal-init/summaries/PHASE_02_SUMMARY.md` -- this file.

### Deviations / Incomplete Items

- `test_providers_registry.py`: Phase 1 had `test_build_registry_phase1_returns_empty` asserting `build_registry` always returns `{}`. Phase 2 wires the YT branch, so that test was replaced with `test_build_registry_empty_config` and `test_build_registry_yt_enabled`. This is expected behavior -- the Phase 1 test was explicitly marked "Phase 1" and the plan says Phase 2 fills the branch.
- `get_enabled_provider_names` returns insertion order (`["yt"]` before `["tidal"]`) rather than sorted order. Phase 1 sorted; Phase 2 spec iterates a tuple. `build_registry` uses `logger.info` with `sorted(registry.keys())` for stable log output.
- Worktree required a `--no-ff` merge of `feature/tidal-init` before the phase could start (Phase 1 commits were not in the worktree branch). This is a normal worktree initialization step.

---

## Test Results

```
2 failed, 646 passed, 4 skipped in 16.19s
```

The 2 failures are the pre-existing position-indicator `[N/M]` display bug in status widget integration tests. Not introduced by Phase 2.

New tests: 5 added (4 in `test_providers_ytmusic.py`, 1 new in updated `test_providers_registry.py`).

---

## Helper Issues

No helpers invoked. None listed in phase plan.

---

## Codebase Context Updates

- `xmpd/ytmusic.py` entry in Key Files table: update path to `xmpd/providers/ytmusic.py`; note `YTMusicProvider` scaffold added (Phase 2), full methods in Phase 3.
- `xmpd/cookie_extract.py` entry: update path to `xmpd/auth/ytmusic_cookie.py`.
- `xmpd/providers/__init__.py`: `build_registry` now instantiates `YTMusicProvider` when `yt` enabled (no longer returns `{}`).
- Add `YTMusicProvider` to the Provider implementations section once Phase 3 completes the surface.
- Note: `get_enabled_provider_names` now returns insertion order (yt before tidal) not sorted.
