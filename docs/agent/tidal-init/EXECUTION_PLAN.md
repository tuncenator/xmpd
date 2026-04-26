# Execution Plan: tidal-init

**Created**: 2026-04-27
**Mode**: Conductor
**Total Phases**: 13
**Total Batches**: 9

---

## Model Configuration

The conductor dispatches every subagent by `subagent_type`. Each subagent file under `~/.claude/agents/` pins its own model in frontmatter, so the conductor never passes a `model` parameter and version drift is impossible.

| Role | Subagent | Pinned model | Context | Notes |
|------|----------|--------------|---------|-------|
| Orchestrator | (slash command, not a subagent) | inherits user's session model | -- | Runs as `/spark-conductor`. Recommended outer model: `claude-opus-4-6`. |
| Hard phases | `spark-coder-hard` | `claude-opus-4-6` | 1M | Routed for Phases 5, 8, 10. |
| Easy/Medium phases | `spark-coder-easy` | `claude-sonnet-4-6` | 1M | Routed for Phases 1, 2, 3, 4, 6, 7, 9, 11, 12, 13. |
| Checkpoint | `spark-checkpoint` | `claude-opus-4-6` | 1M | Merge, test, local verify, inline fix (up to 3 attempts). Does NOT push or deploy. |
| Code review | `spark-code-reviewer` | `claude-opus-4-6` | 1M | Reviews batch diff after a successful checkpoint; must pass before any deploy step (deploy is disabled here, so review just gates state advancement). |
| Deploy-verify | `spark-deploy-verify` | `claude-opus-4-6` | 1M | Disabled for this feature (deploy is off). Only runs if smoke is enabled, which it isn't. |
| Dedicated fix | `spark-fix` | `claude-opus-4-6` | 1M | Fresh-context fix after a checkpoint or review failure. Routes back to code review on FIX COMPLETE. |
| Phase planner | `spark-planner` | `claude-opus-4-7` | 1M | One-shot during `/spark-setup` step 7.5. Already done for this feature. |

---

## Cache Strategy

**Shared Prefix** (identical across all coding agents in a batch -- cached after the first agent):

- CODEBASE_CONTEXT.md (~3.5k tokens; see `docs/agent/tidal-init/CODEBASE_CONTEXT.md`)
- Cross-cutting concerns + glossary from PROJECT_PLAN.md (~2k tokens)
- Previous checkpoint summary (~1-2k tokens; only present from Batch 2 onward)
- QUICKSTART.md universal agent instructions (~3k tokens)
- **Estimated shared prefix**: ~9-11k tokens

**Per-Agent Suffix** (unique to each coding agent):

- That phase's `phase_plans/PHASE_XX.md` (~10-25k tokens depending on difficulty; Phase 10's plan is the largest)
- Phase-specific instructions threaded by the conductor (~1-2k tokens)
- **Estimated per-agent suffix**: ~12-27k tokens

**Note**: All agents in a parallel batch are spawned in a single message to maximize prompt cache hits. The shared prefix must be byte-identical across all agent prompts -- same content, same ordering, same whitespace. Parallel batches in this plan: Batch 2 (2 phases), Batch 3 (3 phases), Batch 8 (2 phases).

---

## File Contention Analysis

> Phases that touch the same files must NOT be in the same parallel batch.
> All actual conflicts are sequenced into different batches; the table below lists only files
> with multi-phase touches (single-phase files omitted for brevity).

| File / Directory | Phases | Risk | Mitigation |
|-----------------|--------|------|------------|
| `xmpd/providers/__init__.py` | 1, 2, 9 | LOW | Sequential batches; each phase owns specific edits (1 stubs, 2 adds yt branch, 9 adds tidal branch). |
| `xmpd/providers/ytmusic.py` | 2, 3 | LOW | Sequential. Phase 2 moves + scaffolds; Phase 3 fills method bodies. Different sections. |
| `xmpd/providers/tidal.py` | 9, 10 | LOW | Sequential. Phase 9 scaffold (stubs); Phase 10 replaces stubs with bodies. |
| `xmpd/stream_proxy.py` | 4, 11 | LOW | Sequential (Batch 3 vs Batch 8). Phase 4 renames + reroutes; Phase 11 adds per-provider TTL config wiring. |
| `xmpd/daemon.py` | 4, 8 | LOW | Sequential (Batch 3 vs Batch 5). Phase 4 changes only the import line + one TODO comment; Phase 8 does the full rewire. |
| `bin/xmpctl` | 8, 11 | LOW | Sequential (Batch 5 vs Batch 8). Phase 8 establishes subcommand structure; Phase 11 fills in `auth tidal` body and adds `--provider` to search. |
| `tests/test_stream_proxy.py` | 4, 11 | LOW | Sequential. Phase 4 rewrites; Phase 11 appends per-provider-TTL test cases. |
| `pyproject.toml` | 9, 10, 13 | LOW | Sequential (Batches 6, 7, 9). Phase 9 adds `tidalapi`; Phase 10 may register the `tidal_integration` pytest marker (skip if Phase 9 already did); Phase 13 adds `ruamel.yaml` to dev deps. Each touches different sections. |
| `tests/*.py` (sed-touched in Phase 2) | 2, others | LOW | Phase 2's sed only rewrites `from xmpd.ytmusic` / `from xmpd.cookie_extract` import lines; only test files that imported those modules are touched. Phase 5's `tests/test_track_store.py` doesn't import from those. |

**No HIGH-risk file contention.** Every parallel batch (2, 3, 8) has phases with disjoint file sets. The two parallel pairs that share a directory but not a file: Phase 3 (`tests/test_providers_ytmusic.py`) vs Phase 4 (`tests/test_stream_proxy.py`); Phase 11 (`tests/test_config.py`, `tests/test_stream_proxy.py`) vs Phase 12 (`tests/test_airplay_bridge_track_store_reader.py`).

---

## Batch Schedule

| Batch | Phases | Mode | Checkpoint Deploy | Checkpoint Verify |
|-------|--------|------|-------------------|-------------------|
| 1 | Phase 1 | sequential | No (deploy disabled) | Provider Protocol + dataclasses + registry skeleton; logging audit |
| 2 | Phase 2, Phase 5 | parallel | No | Module relocations preserve git history; track_store schema migrated and idempotent |
| 3 | Phase 3, Phase 4, Phase 7 | parallel | No | YTMusicProvider full Protocol coverage; stream_proxy rerouted; history+rating registry-aware |
| 4 | Phase 6 | sequential | No | Sync engine iterates registry; YT-only behavior byte-identical to pre-refactor |
| 5 | Phase 8 | sequential | No | Daemon registry wiring + xmpctl restructure; **Stage B keystone** |
| 6 | Phase 9 | sequential | No | Tidal foundation: tidalapi installed; OAuth flow runs end-to-end; TidalProvider scaffolded |
| 7 | Phase 10 | sequential | No | TidalProvider 14 methods working; live tests pass against user's Tidal account |
| 8 | Phase 11, Phase 12 | parallel | No | `xmpctl auth tidal` end-to-end; new config shape final; AirPlay shows Tidal art |
| 9 | Phase 13 | sequential | No | install.sh migrates legacy config; README/MIGRATION/CHANGELOG rewritten; final integration push |

Deploy is disabled feature-wide (no remote target). Smoke is disabled (local daemon, live verification covers it). The "Checkpoint Deploy" column is therefore "No" everywhere.

---

## Batch Details

### Batch 1: Foundation

**Mode**: sequential
**Rationale**: Phase 1 has no upstream dependencies and creates the package layout (Provider Protocol, dataclasses, registry skeleton) that every subsequent phase imports. It must complete before any other work begins.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 1 | Provider abstraction foundation | easy | spark-coder-easy | ~25k | All-new files; logging-infra audit by grep. |

**Checkpoint**:
- **Deploy**: No (disabled for this feature).
- **Verify**: `pytest -q` passes (8 new tests + existing suite still green); `python -c "from xmpd.providers.base import Track, Playlist, TrackMetadata, Provider; from xmpd.providers import build_registry, get_enabled_provider_names; assert build_registry({'yt': {'enabled': True}}) == {}"` runs cleanly; `mypy xmpd/providers/` passes.
- **Critical**: Yes -- this is the foundation; failure blocks all downstream batches.

### Batch 2: Move + Track Store

**Mode**: parallel
**Rationale**: Phase 2 (file moves + YTMusicProvider scaffold) and Phase 5 (track store schema migration) both depend only on Phase 1 and have disjoint file sets. Phase 2 owns `xmpd/providers/ytmusic.py` (moved from `xmpd/ytmusic.py`), `xmpd/auth/ytmusic_cookie.py` (moved from `xmpd/cookie_extract.py`), `xmpd/providers/__init__.py` (yt branch), and tests; Phase 5 owns `xmpd/track_store.py`, `tests/test_track_store.py`, and the migration test fixture/spec.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 2 | YT module relocation + YTMusicProvider scaffold | easy | spark-coder-easy | ~30k | `git mv` preserves history; sed updates imports; scaffold class only. |
| 5 | Track store schema migration | hard | spark-coder-hard | ~50k | Idempotent migration via PRAGMA user_version. Real-data preservation. Hard. |

**Checkpoint**:
- **Deploy**: No.
- **Verify**: `pytest -q` passes; `grep -rn 'from xmpd.ytmusic\|from xmpd.cookie_extract' --include='*.py' .` returns no matches; `sqlite3 ~/.config/xmpd/track_mapping.db "PRAGMA user_version"` returns 1; legacy DB rows tagged `provider='yt'` and counts preserved; running migration twice is a no-op.
- **Critical**: Yes -- both phases set up the foundation that Batch 3 depends on.

### Batch 3: YT Wrapper + Stream Proxy + History/Rating

**Mode**: parallel
**Rationale**: Three phases that depend on Phases 1, 2, 5 (already merged via earlier batches) but are mutually independent in file ownership.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 3 | YTMusicProvider methods | medium | spark-coder-easy | ~70k | All 14 Protocol methods on YTMusicProvider; mocks for unit tests, live YT for evidence capture. Largest medium phase. |
| 4 | Stream proxy rename + provider-aware routing | medium | spark-coder-easy | ~50k | File rename, class rename, route change, build_proxy_url helper, callers updated. Daemon import line touched (full rewire is Phase 8). |
| 7 | Provider-aware history reporter + rating module | medium | spark-coder-easy | ~30k | Regex change, dispatch via registry; rating's state machine stays pure. |

**Checkpoint**:
- **Deploy**: No.
- **Verify**: `pytest -q` passes; `isinstance(YTMusicProvider({}), Provider)` is True; the proxy serves `/proxy/yt/<id>` correctly with 307; per-provider regex validation works (404/400 on bad provider/id); HistoryReporter parses both yt and tidal URL prefixes; RatingManager's apply_to_provider helper dispatches correctly.
- **Critical**: Yes -- Phase 6 (next batch) depends on all three.

### Batch 4: Sync Engine

**Mode**: sequential
**Rationale**: Phase 6 depends on Phases 3 (provider methods), 4 (build_proxy_url), and 5 (track_store API). All three are merged by end of Batch 3, so Phase 6 can run.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 6 | Provider-aware sync engine | medium | spark-coder-easy | ~40k | Constructor signature change; iterate registry; per-provider failure isolation. |

**Checkpoint**:
- **Deploy**: No.
- **Verify**: `pytest -q` passes; with only YT enabled, `xmpctl sync` produces byte-identical playlist files vs pre-refactor baseline (compare `~/.config/mpd/playlists/YT: *.m3u`); track_store rows tagged `provider='yt'`.
- **Critical**: Yes -- Phase 8 wires the daemon to use the new sync engine.

### Batch 5: Daemon + CLI Keystone

**Mode**: sequential
**Rationale**: Phase 8 is the **Stage B keystone**: it's the single point where the daemon switches from direct `YTMusicClient` injection to provider-registry construction. It depends on every prior phase. Failure here means Stage B cannot complete.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 8 | Daemon registry wiring + xmpctl auth subcommand restructure | hard | spark-coder-hard | ~50k | Cross-cutting integration; daemon + CLI + tests; stub for `xmpctl auth tidal` until Phase 11. |

**Checkpoint**:
- **Deploy**: No.
- **Verify**: `pytest -q` passes; `python -m xmpd` starts; `xmpctl sync|status|stop` work as before; `xmpctl auth yt` runs the existing browser-cookie flow; `xmpctl auth tidal` prints stub. End-to-end YT sync + playback through proxy + history reporting verified live.
- **Critical**: Yes -- Stage B verification happens here. Tidal phases follow.

### Batch 6: Tidal Foundation

**Mode**: sequential
**Rationale**: First Tidal-touching phase. Adds the dep, the OAuth flow, the scaffold, and the registry-tidal-branch. Phase 8 already accommodates Tidal in the registry; flipping the config flag activates it after Phase 9 lands.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 9 | Tidal foundation (tidalapi dep, OAuth, scaffold) | medium | spark-coder-easy | ~50k | Live OAuth verification against the user's real Tidal account; clipboard helper; token persistence at 0600. |

**Checkpoint**:
- **Deploy**: No.
- **Verify**: `pytest -q` passes; `tidalapi` imports cleanly; OAuth flow runs end-to-end against user's account during live verification; `~/.config/xmpd/tidal_session.json` created with right shape and 0600 mode; load_session validates via check_login.
- **Critical**: Yes -- Phase 10 depends on this.

### Batch 7: TidalProvider Methods

**Mode**: sequential
**Rationale**: All 14 Protocol methods on TidalProvider replace the Phase 9 stubs. Hard phase: pagination, quality clamping, HARD GUARDRAIL discipline, real-API integration tests.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 10 | TidalProvider methods (full Protocol coverage) | hard | spark-coder-hard | ~80k | Largest phase. Hard. Live tests gated behind `@pytest.mark.tidal_integration`. HARD GUARDRAIL: sentinel-track-only mutations. |

**Checkpoint**:
- **Deploy**: No.
- **Verify**: `pytest -q` passes (skips tidal_integration by default); `pytest -m tidal_integration` (with `XMPD_TIDAL_TEST=1`) passes against real account; isinstance(TidalProvider({}), Provider) is True; HARD GUARDRAIL: pre-test favorites count == post-test favorites count.
- **Critical**: Yes -- Phase 11 (CLI) depends on Tidal methods being live.

### Batch 8: Tidal CLI/Config + AirPlay Bridge

**Mode**: parallel
**Rationale**: Two phases with disjoint file sets. Phase 11 owns `bin/xmpctl`, `xmpd/config.py`, `xmpd/stream_proxy.py`, `examples/config.yaml`, plus their tests; Phase 12 owns `extras/airplay-bridge/mpd_owntone_metadata.py` and its new test file. Both depend on Phase 10 (TidalProvider working) and Phase 5 (track_store schema with art_url) which are merged.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 11 | Tidal CLI + per-provider config + stream-proxy wiring | medium | spark-coder-easy | ~40k | `xmpctl auth tidal` end-to-end; new config shape final; legacy rejected with clear error. |
| 12 | AirPlay bridge: Tidal album art | medium | spark-coder-easy | ~40k | Regex update, SQLite reader for art_url, classifier emits xmpd-yt/xmpd-tidal. Manual verification required (play tracks over real AirPlay). |

**Checkpoint**:
- **Deploy**: No.
- **Verify**: `pytest -q` passes; `xmpctl auth tidal` walkthrough completes end-to-end; daemon with both providers enabled+authenticated produces YT and TD playlists; legacy config produces clear ConfigError; AirPlay bridge displays Tidal art (manual verification on user's actual receiver).
- **Critical**: Yes -- Phase 13 depends on both.

### Batch 9: Install + Docs + Final

**Mode**: sequential
**Rationale**: Final phase. install.sh / uninstall.sh / README / MIGRATION / CHANGELOG / migrate-config.py. Single phase, sequential.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 13 | Install / migration / docs / final integration | medium | spark-coder-easy | ~50k | ruamel.yaml-based config rewrite; install.sh idempotent; README rewrite; CHANGELOG entry. Final push to origin/feature/tidal-init. Merge command suggested but NOT executed. |

**Checkpoint**:
- **Deploy**: No.
- **Verify**: `pytest -q` passes; `scripts/migrate-config.py` round-trips legacy -> new without comment loss; install.sh runs cleanly on user's actual machine after backup; daemon starts cleanly post-migration; README/MIGRATION/CHANGELOG are accurate.
- **Critical**: Yes -- this is the final acceptance gate.

---

## Dependency Graph

```
=== Batch 1: Foundation ===
Phase 1 (easy, sequential)
  |
--- Checkpoint 1 ---
  |
=== Batch 2: Move + Track Store ===
Phase 2 (easy, parallel) --+
Phase 5 (hard, parallel) --+--> merge
  |
--- Checkpoint 2 ---
  |
=== Batch 3: YT Wrapper + Stream Proxy + History/Rating ===
Phase 3 (medium, parallel) --+
Phase 4 (medium, parallel) --+
Phase 7 (medium, parallel) --+--> merge
  |
--- Checkpoint 3 ---
  |
=== Batch 4: Sync Engine ===
Phase 6 (medium, sequential)
  |
--- Checkpoint 4 ---
  |
=== Batch 5: Daemon + CLI Keystone ===
Phase 8 (hard, sequential)  [Stage B verification ends here]
  |
--- Checkpoint 5 ---
  |
=== Batch 6: Tidal Foundation ===
Phase 9 (medium, sequential)
  |
--- Checkpoint 6 ---
  |
=== Batch 7: TidalProvider Methods ===
Phase 10 (hard, sequential)
  |
--- Checkpoint 7 ---
  |
=== Batch 8: Tidal CLI/Config + AirPlay Bridge ===
Phase 11 (medium, parallel) --+
Phase 12 (medium, parallel) --+--> merge
  |
--- Checkpoint 8 ---
  |
=== Batch 9: Install + Docs + Final ===
Phase 13 (medium, sequential)
  |
--- Checkpoint 9 (final) ---
```

Total parallel time saved: ~3 batches' worth of sequential work (Batches 2, 3, 8 each parallelize 2-3 phases). Without parallelism this would be ~13 sequential batches; with the plan above it's 9. Roughly a 30% throughput gain.

---

## Conductor Pacing

- **Mode**: auto-refresh
- **Batches Per Session**: 5

Default rationale: 9 batches > 5, so per `/spark-setup` defaults the conductor refreshes context after every 5 batches. The user can interrupt and switch modes via the `/spark-conductor` flow at any checkpoint.

Alternative modes if the user wants to change:

- `full-auto`: run all 9 batches without pausing. Best if confidence is high after a few batches succeed.
- `confirm-each-batch`: pause after every checkpoint; user types "continue" to advance. Best for high-stakes phases (5, 8, 10, 13). The user can also switch to this mode partway through.

To change mode, edit this section before starting the conductor (or interrupt mid-run and restart with the new mode).

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

## Notes

**Risk areas** (where the conductor should slow down and verify carefully):

1. **Phase 5 (track store migration, hard)**: real-data preservation. The phase plan requires backing up `~/.config/xmpd/track_mapping.db` before running the migration on the live DB. The checkpoint should verify both the test-suite migration AND the live-DB migration succeed with row count preserved.

2. **Phase 8 (daemon keystone, hard)**: cross-cutting integration. Failure here means Stage B is broken and Phases 9-13 can't proceed cleanly. The checkpoint should run end-to-end: full daemon start, `xmpctl sync`, proxy serves, history reports, rating round-trip. If anything regresses vs pre-refactor, escalate to `spark-fix`.

3. **Phase 10 (TidalProvider methods, hard)**: HARD GUARDRAIL discipline. Every test that mutates favorites MUST use a sentinel track and clean up. Pre-test and post-test favorites counts must match. The reviewer should verify this in the diff.

4. **Phase 13 (install.sh migration, medium)**: idempotency and comment preservation. `scripts/migrate-config.py` must be safe to run on an already-migrated config; a hand-edited config with comments must round-trip without losing comments.

**Live verification touchpoints** (live-API calls that the user should be aware of when these run):

- Phase 1: grep + import smoke (no API).
- Phase 2: pytest only (no API).
- Phase 3: live YT call to capture ytmusicapi response shapes (read-only).
- Phase 4: local proxy 307 round-trip.
- Phase 5: schema migration on the user's actual `~/.config/xmpd/track_mapping.db` (after backup).
- Phase 6-8: full daemon end-to-end with YT enabled.
- Phase 9: live Tidal OAuth flow (interactive; user authorizes in browser once).
- Phase 10: live Tidal API calls (read favorites, search, radio); ONE live like+unlike round trip on a sentinel track.
- Phase 11: full daemon end-to-end with both providers enabled+authenticated.
- Phase 12: AirPlay bridge plays Tidal track on user's real receiver (manual; HARD GUARDRAIL irrelevant).
- Phase 13: install.sh dry-run on a config-dir copy first; then live.

**Borderline difficulty ratings**:

- Phase 6 (sync engine): rated medium because the file is large but the change is well-scoped. Could be hard if the existing `SyncEngine.__init__` has more cross-cutting state than expected; the conductor should re-rate to hard after Phase 8 if Phase 6's checkpoint required `spark-fix`.

- Phase 13: rated medium for the install.sh complexity. ruamel.yaml comment-preserving rewrites are the main risk. Could be hard if real user configs have edge cases the migration doesn't handle. The plan's Evidence Captured section requires capturing the actual config first, so the coder is grounded.
