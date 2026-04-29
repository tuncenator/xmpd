# Execution Plan: search-and-tidal-quality

**Created**: 2026-04-29
**Mode**: Conductor
**Total Phases**: 4
**Total Batches**: 3

---

## Model Configuration

The conductor dispatches every subagent by `subagent_type`. Each subagent file under `~/.claude/agents/` pins its own model in frontmatter, so the conductor never passes a `model` parameter and version drift is impossible.

| Role | Subagent | Pinned model | Context | Notes |
|------|----------|--------------|---------|-------|
| Orchestrator | (slash command, not a subagent) | inherits user's session model | -- | Runs as `/spark-conductor`. Recommended outer model: `claude-opus-4-6`. |
| Hard phases | `spark-coder-hard` | `claude-opus-4-6` | 1M | No hard phases in this feature. |
| Easy/Medium phases | `spark-coder-easy` | `claude-sonnet-4-6` | 1M | All 4 phases route here. |
| Checkpoint | `spark-checkpoint` | `claude-opus-4-6` | 1M | Merge, test, local verify, inline fix (up to 3 attempts). |
| Code review | `spark-code-reviewer` | `claude-opus-4-6` | 1M | Reviews batch diff after a successful checkpoint. |
| Deploy-verify | `spark-deploy-verify` | `claude-opus-4-6` | 1M | Not used (deploy disabled). |
| Dedicated fix | `spark-fix` | `claude-opus-4-6` | 1M | Fresh-context fix after checkpoint/review failure. |

---

## Cache Strategy

**Shared Prefix** (identical across all coding agents in a batch -- cached after the first agent):
- CODEBASE_CONTEXT.md (~3k tokens)
- Cross-cutting concerns from PROJECT_PLAN.md (~1k tokens)
- Universal agent instructions (~8k tokens)
- **Estimated shared prefix**: ~12k tokens

**Per-Agent Suffix** (unique to each coding agent):
- Phase plan from PHASE_XX.md (~2k tokens)
- **Estimated per-agent suffix**: ~2k tokens

---

## File Contention Analysis

| File / Directory | Phases That Touch It | Risk | Mitigation |
|-----------------|---------------------|------|------------|
| `xmpd/daemon.py` | Phase 1 (1145-1212), Phase 2 (844-895), Phase 3 (897-1021), Phase 4 (1054-1059) | HIGH (all 4 phases, different sections) | Never more than 2 phases touching it in the same batch. Batch 2 has Phases 2+4: lines 844-895 vs 1054-1059 (~160 lines apart, clean merge). Phase 3 (batch 3) runs after Phase 2's deletion. |
| `bin/xmpctl` | Phase 2 (331-486), Phase 3 (648-728) | MEDIUM (same file, different sections) | Phases 2 and 3 are in separate batches. |
| `bin/xmpd-search` | Phase 2 (ctrl-q rebind), Phase 3 (ctrl-r investigation) | MEDIUM (same file, different keybindings) | Phases 2 and 3 are in separate batches. |
| `xmpd/stream_proxy.py` | Phase 4 only | LOW | No contention. |
| `tests/` | Phases 1, 2, 4 | LOW (different test files) | Different test files per phase. |

---

## Batch Schedule

| Batch | Phases | Mode | Checkpoint Deploy | Checkpoint Verify |
|-------|--------|------|-------------------|-------------------|
| 1 | Phase 1 | sequential | No | Play/queue from search produces audio, not 404 |
| 2 | Phase 2, Phase 4 | parallel | No | Dead search command removed, ctrl-e queues track, Tidal streams use highest quality, quality labels reflect reality |
| 3 | Phase 3 | sequential | No | ctrl-r creates radio from fzf-selected track |

---

## Batch Details

### Batch 1: TrackStore Foundation

**Mode**: sequential
**Rationale**: Most critical bug fix. Must verify play/queue works before other phases depend on a working playback pipeline for their manual testing.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 1 | TrackStore Registration | easy | spark-coder-easy | ~50k | Copy pattern from _cmd_radio |

**Checkpoint**:
- **Deploy**: No -- deploy disabled
- **Verify**: Search -> play produces audio (proxy resolves, MPD plays). Queue adds playable track.
- **Critical**: Yes -- foundational fix, validates the proxy URL pipeline

### Batch 2: Cleanup + Quality

**Mode**: parallel
**Rationale**: Phase 2 and Phase 4 are independent with no file contention (daemon.py sections are 160+ lines apart, no overlapping files otherwise). Parallel execution saves wall clock time.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 2 | Dead Code Removal + Key Rebind | easy | spark-coder-easy | ~40k | Code deletion + keybinding change |
| 4 | Tidal Quality Fixes | medium | spark-coder-easy | ~55k | ffmpeg -map + quality labels |

**Checkpoint**:
- **Deploy**: No -- deploy disabled
- **Verify**: `xmpctl search` returns unknown command. ctrl-e queues track in fzf. ffmpeg command includes -map. Quality labels not hardcoded "CD".
- **Critical**: No

### Batch 3: Radio Investigation

**Mode**: sequential
**Rationale**: Phase 3 needs investigation and debugging. Benefits from Phases 2's dead code removal being done (cleaner daemon.py near the radio handler). Sequential is appropriate for investigation work.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 3 | Radio Targeting Fix | medium | spark-coder-easy | ~60k | Investigation + fix |

**Checkpoint**:
- **Deploy**: No -- deploy disabled
- **Verify**: ctrl-r from search creates radio from selected track (not currently playing).
- **Critical**: No

---

## Dependency Graph

```
=== Batch 1 ===
Phase 1 (easy, sequential) -- TrackStore Registration
  |
--- Checkpoint 1 ---
  |
=== Batch 2 ===
Phase 2 (easy)   --+-- Dead Code + Key Rebind
Phase 4 (medium) --+-- Tidal Quality Fixes
  |                   --> merge
--- Checkpoint 2 ---
  |
=== Batch 3 ===
Phase 3 (medium, sequential) -- Radio Targeting Fix
  |
--- Checkpoint 3 (final) ---
```

---

## Conductor Pacing

- **Mode**: full-auto
- **Batches Per Session**: N/A

3 batches, all run in one session without pausing. full-auto is appropriate for this size.

---

## Fix Strategy

- **Max inline fix attempts per checkpoint**: 3
- **Inline fix**: `spark-checkpoint` itself attempts fixes (it has full merge context)
- **Dedicated fix subagent**: `spark-fix` (fresh context, claude-opus-4-6 pinned)
- **Escalation path**: 3 inline fixes -> dedicated fix subagent -> human intervention
- **Fix scope rules**:
  - Localized failure (one file, clear cause, <50 lines): inline fix in checkpoint
  - Systemic failure (architectural incompatibility, missing interfaces): skip inline, dispatch `spark-fix` immediately
  - Fix outcomes are appended to the checkpoint summary

---

## Notes

- All 4 phases are easy or medium difficulty, routing to spark-coder-easy (Sonnet). Phase plans are written as literal specs with exact file paths and line numbers.
- Phase 3 (radio targeting) is the riskiest: root cause is unknown and requires investigation. If the bug turns out to be in the bash/fzf layer rather than Python, automated tests may not be able to cover it.
- The daemon.py contention across 4 phases is managed by never having more than 2 phases in the same batch touching it, and ensuring those 2 modify sections at least 100 lines apart.
- Manual verification is critical for every phase. Agents must actually run the daemon and test features, not just pass unit tests.
