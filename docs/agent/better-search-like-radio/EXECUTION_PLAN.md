# Execution Plan: better-search-like-radio

**Created**: 2026-04-28
**Mode**: Conductor
**Total Phases**: 5
**Total Batches**: 4

---

## Model Configuration

The conductor dispatches every subagent by `subagent_type`. Each subagent file under `~/.claude/agents/` pins its own model in frontmatter, so the conductor never passes a `model` parameter and version drift is impossible.

| Role | Subagent | Pinned model | Context | Notes |
|------|----------|--------------|---------|-------|
| Orchestrator | (slash command, not a subagent) | inherits user's session model | -- | Runs as `/spark-conductor`. Recommended outer model: `claude-opus-4-6`. |
| Hard phases | `spark-coder-hard` | `claude-opus-4-6` | 1M | Routed when phase Difficulty = hard. |
| Easy/Medium phases | `spark-coder-easy` | `claude-sonnet-4-6` | 1M | Routed when phase Difficulty = easy or medium. |
| Checkpoint | `spark-checkpoint` | `claude-opus-4-6` | 1M | Merge, test, local verify, inline fix (up to 3 attempts). Does NOT push or deploy. |
| Code review | `spark-code-reviewer` | `claude-opus-4-6` | 1M | Reviews batch diff after a successful checkpoint; must pass before any deploy step. |
| Deploy-verify | `spark-deploy-verify` | `claude-opus-4-6` | 1M | Push, deploy, verify-deploy, smoke. Runs only after code review PASSES. |
| Dedicated fix | `spark-fix` | `claude-opus-4-6` | 1M | Fresh-context fix after a checkpoint, review, or deploy-verify failure. |
| Phase planner | `spark-planner` | `claude-opus-4-7` | 1M | One-shot during `/spark-setup` step 7.5. |

---

## Cache Strategy

**Shared Prefix** (identical across all coding agents in a batch -- cached after the first agent):
- CODEBASE_CONTEXT.md (~3k tokens)
- Cross-cutting concerns from PROJECT_PLAN.md (~1k tokens)
- Previous checkpoint summary (~0k for Batch 1, ~2k for later batches)
- Universal agent instructions (~4k tokens)
- **Estimated shared prefix**: ~8k tokens

**Per-Agent Suffix** (unique to each coding agent):
- Phase plan from PHASE_XX.md (~3-4k tokens)
- Phase-specific instructions (~1k tokens)
- **Estimated per-agent suffix**: ~4-5k tokens

**Note**: All agents in a parallel batch are spawned in a single message to maximize prompt cache hits. The shared prefix must be byte-identical across all agent prompts.

---

## File Contention Analysis

| File / Directory | Phases That Touch It | Risk | Mitigation |
|-----------------|---------------------|------|------------|
| `xmpd/stream_proxy.py` | Phase 1 only | NONE | No contention |
| `xmpd/providers/base.py` | Phase 2 only | NONE | No contention |
| `xmpd/providers/tidal.py` | Phase 2 only | NONE | No contention |
| `xmpd/providers/ytmusic.py` | Phase 2 only | NONE | No contention |
| `xmpd/daemon.py` | Phase 2, Phase 5 | LOW | Different batches (Batch 1 vs Batch 4), no conflict |
| `bin/xmpctl` | Phase 2, Phase 4, Phase 5 | LOW | All in different batches |
| `bin/xmpd-search` (new) | Phase 3, Phase 4, Phase 5 | LOW | Sequential batches, each extends the previous |
| `tests/test_stream_proxy.py` | Phase 1 only | NONE | No contention |

**Batch 1 is contention-free**: Phase 1 touches `stream_proxy.py` + its tests. Phase 2 touches `providers/`, `daemon.py`, `xmpctl`. Zero overlap.

---

## Batch Schedule

| Batch | Phases | Mode | Checkpoint Deploy | Checkpoint Verify |
|-------|--------|------|-------------------|-------------------|
| 1 | Phase 1, Phase 2 | parallel | No | Proxy counter returns to 0 after playback; search-json returns valid JSON with quality + liked |
| 2 | Phase 3 | sequential | No | fzf search opens, shows colored results with quality badges and liked indicators |
| 3 | Phase 4 | sequential | No | All actions work: play, queue, radio, multi-select queue, multi-select play |
| 4 | Phase 5 | sequential | No | Like/unlike in search toggles [+1] instantly; provider favorites actually updated |

---

## Batch Details

### Batch 1: Infrastructure + Search API

**Mode**: parallel
**Rationale**: Phase 1 (proxy fix) and Phase 2 (search API) are completely independent. Zero file overlap. Can safely run in parallel to cut wall-clock time.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 1 | Fix Proxy Connection Leak | hard | spark-coder-hard | ~60k | Deep async debugging, stress tests |
| 2 | Search API Enhancement | medium | spark-coder-easy | ~50k | Add TrackMetadata.quality, JSON search output |

**Checkpoint**:
- **Deploy**: No -- no deployment configured
- **Verify**: (1) Health endpoint shows `active_connections` returns to 0 after playlist playback. (2) `xmpctl search-json "radiohead"` returns NDJSON with `provider`, `quality`, `liked` fields populated.
- **Critical**: Yes -- both are foundation for remaining phases

### Batch 2: Interactive Search UI

**Mode**: sequential
**Rationale**: Phase 3 depends on Phase 2's JSON search output. Only one phase in this batch.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 3 | Interactive fzf Search | hard | spark-coder-hard | ~70k | fzf wrapper, ANSI formatting, clerk keybind |

**Checkpoint**:
- **Deploy**: No
- **Verify**: `C-s` in clerk opens fzf search. Typing a query shows live colored results with [TD]/[YT] provider tags, quality badges, liked indicators.
- **Critical**: Yes -- all remaining phases extend this UI

### Batch 3: Actions

**Mode**: sequential
**Rationale**: Phase 4 depends on Phase 3's fzf interface. Only one phase.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 4 | Search Actions | medium | spark-coder-easy | ~50k | fzf keybindings for play/queue/radio/multi-select |

**Checkpoint**:
- **Deploy**: No
- **Verify**: All actions functional: enter=play, ctrl-q=queue, ctrl-r=radio, tab=select, ctrl-a=queue-all, ctrl-p=play-all. Each verified with actual playback.
- **Critical**: No -- Phase 5 is the only dependent

### Batch 4: Like Integration

**Mode**: sequential
**Rationale**: Phase 5 depends on Phase 4's action framework. Final batch.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 5 | Real-time Like Updates | medium | spark-coder-easy | ~40k | ctrl-l toggle, instant [+1] display update |

**Checkpoint**:
- **Deploy**: No
- **Verify**: Like/unlike in search instantly shows/hides [+1]. Provider favorites actually updated (check Tidal favorites).
- **Critical**: No -- final phase

---

## Dependency Graph

```
=== Batch 1 (parallel) ===
Phase 1 (hard)  --+
Phase 2 (medium) -+--> merge
  |
--- Checkpoint 1 ---
  |
=== Batch 2 ===
Phase 3 (hard)
  |
--- Checkpoint 2 ---
  |
=== Batch 3 ===
Phase 4 (medium)
  |
--- Checkpoint 3 ---
  |
=== Batch 4 ===
Phase 5 (medium)
  |
--- Checkpoint 4 (final) ---
```

---

## Conductor Pacing

- **Mode**: full-auto
- **Batches Per Session**: N/A

4 batches is small enough for full-auto. Conductor runs all batches without pausing.

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

- Phase 1 (proxy fix) is the most uncertain phase. The root cause is unknown. If investigation reveals the issue is overload rather than a leak, the fix might be configuration (increase limit) rather than code. The checkpoint should be prepared for either outcome.
- Phase 3 (fzf search) is the most complex new code. fzf's `change:reload` with `--disabled` and `--with-nth` can be finicky. The checkpoint should verify the visual output carefully.
- Batch 1's parallelism saves significant wall-clock time since both phases are substantial (60k + 50k tokens).
- All phases after Batch 1 are sequential single-phase batches. The overhead per checkpoint is justified by the dependency chain and the importance of verifying each UI layer before building the next.
