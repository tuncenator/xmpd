# Execution Plan: search-redesign-and-enhancements

**Created**: 2026-04-29
**Mode**: Conductor
**Total Phases**: 3
**Total Batches**: 1

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
| Dedicated fix | `spark-fix` | `claude-opus-4-6` | 1M | Fresh-context fix after a checkpoint, review, or deploy-verify failure. Never deploys; routes back to code review on FIX COMPLETE. |
| Phase planner | `spark-planner` | `claude-opus-4-7` | 1M | One-shot during `/spark-setup` step 7.5. |

---

## Cache Strategy

**Shared Prefix** (identical across all coding agents in the batch -- cached after the first agent):
- CODEBASE_CONTEXT.md (~3k tokens)
- Cross-cutting concerns from PROJECT_PLAN.md (~1k tokens)
- Universal agent instructions (~5k tokens)
- **Estimated shared prefix**: ~9k tokens

**Per-Agent Suffix** (unique to each coding agent):
- Phase plan from PHASE_XX.md (~2-3k tokens each)
- **Estimated per-agent suffix**: ~3k tokens

**Note**: All agents in a parallel batch are spawned in a single message to maximize prompt cache hits. The shared prefix must be byte-identical across all agent prompts.

---

## File Contention Analysis

> Phases that touch the same files must NOT be in the same parallel batch.

| File / Directory | Phases That Touch It | Risk | Mitigation |
|-----------------|---------------------|------|------------|
| `bin/xmpd-search` | Phase 1 only | NONE | -- |
| `xmpd/providers/tidal.py` | Phase 2 only | NONE | -- |
| `xmpd/playlist_patcher.py` | Phase 3 only (new file) | NONE | -- |
| `xmpd/daemon.py` | Phase 3 only | NONE | -- |
| `tests/` | Phases 1, 2, 3 (different test files) | LOW | Each phase creates its own test file |

**No file contention.** All three phases touch entirely different files. Safe for a single parallel batch.

---

## Batch Schedule

| Batch | Phases | Mode | Checkpoint Deploy | Checkpoint Verify |
|-------|--------|------|-------------------|-------------------|
| 1 | Phase 1, Phase 2, Phase 3 | parallel | No (deploy disabled) | Tests pass, service restart + status check |

---

## Batch Details

### Batch 1: All Features

**Mode**: parallel
**Rationale**: All three phases are fully independent -- different files, different features, no shared state. Zero file contention. Maximum parallelism.

| Phase | Name | Difficulty | Subagent | Est. Tokens | Notes |
|-------|------|------------|----------|-------------|-------|
| 1 | Two-Mode fzf Search | easy | spark-coder-easy | ~50k | Pure bash, `bin/xmpd-search` only |
| 2 | Tidal Play Reporting | hard | spark-coder-hard | ~80k | Reverse-engineered API, SQS encoding |
| 3 | Like-Toggle Playlist Patching | medium | spark-coder-easy | ~60k | New module + daemon integration |

**Checkpoint**:
- **Deploy**: No -- deployment is disabled for this feature
- **Verify**:
  - All tests pass: `uv run pytest tests/ -v`
  - Service restarts cleanly: `systemctl --user restart xmpd && systemctl --user is-active xmpd`
  - Status check: `bin/xmpctl status`
  - Phase 1: manual search test (type query, Enter to Browse, Esc back)
  - Phase 2: play a Tidal track >30s, check logs for event-batch POST
  - Phase 3: like-toggle a track, check playlist files and MPD queue
- **Critical**: Yes -- this is the only batch

---

## Dependency Graph

```
=== Batch 1 ===
Phase 1 (easy)   --+
Phase 2 (hard)   --+--> merge
Phase 3 (medium) --+
  |
--- Checkpoint 1 ---
  |
=== DONE ===
```

---

## Conductor Pacing

- **Mode**: full-auto
- **Batches Per Session**: N/A

Single batch, single checkpoint. full-auto is the natural choice.

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

- Phase 2 (Tidal Play Reporting) is the highest-risk phase due to the undocumented API. The research reference in the phase plan is thorough, but edge cases around auth token state and SQS encoding may surface.
- Phase 1 (fzf Search) depends on fzf version >= 0.30 for `rebind`/`unbind`. The user's Manjaro system should have a recent fzf.
- Phase 3 (Like Patch) is structurally straightforward but needs careful regex work for M3U/XSPF title manipulation.
- With all three phases in one parallel batch, the checkpoint merge should be clean since there's zero file contention.
