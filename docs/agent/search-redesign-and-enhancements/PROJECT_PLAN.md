# xmpd - Project Plan

**Feature/Initiative**: search-redesign-and-enhancements
**Type**: New Feature
**Created**: 2026-04-29
**Estimated Total Phases**: 3

---

## Project Location

**IMPORTANT: All paths in this document are relative to the project root.**

- **Project Root**: `/home/tunc/Sync/Programs/xmpd`
- **Verify with**: `pwd` -> should output `/home/tunc/Sync/Programs/xmpd`

When you see a path like `xmpd/daemon.py`, it means `/home/tunc/Sync/Programs/xmpd/xmpd/daemon.py`

---

## Project Overview

### Purpose

Three independent enhancements to xmpd's interactive experience: a two-mode fzf search interface with proper debounce, real play reporting to Tidal's event-batch API (replacing the no-op `track.get_stream()` workaround), and instant like-indicator updates in playlists and the live MPD queue after a like toggle.

### Scope

**In Scope**:
- Two-mode fzf search (Search mode / Browse mode) with 350ms debounce in `bin/xmpd-search`
- Tidal play reporting via `playback_session` events to `https://tidal.com/api/event-batch`
- Like-toggle playlist patching: on-disk M3U/XSPF files and live MPD queue tag updates

**Out of Scope**:
- YouTube play reporting changes (already working via ytmusicapi)
- Tidal HiRes streaming upgrades
- Daemon socket protocol changes
- New CLI commands

### Success Criteria

- [ ] Two-mode search: typing in Search mode fires debounced API calls; Enter transitions to Browse mode with local fzf filtering; Esc returns to Search mode preserving query; action keybinds only active in Browse mode
- [ ] Tidal play reporting: after 30s of Tidal track playback, a `playback_session` event is POSTed to `https://tidal.com/api/event-batch` with correct SQS format
- [ ] Like-toggle patching: after toggling like on a track, the `[+1]` indicator updates immediately in on-disk playlists containing that track and in the live MPD queue (visible in ncmpcpp without refresh)
- [ ] All existing tests pass; new tests cover the added functionality
- [ ] The running xmpd systemd service works end-to-end with all three features

---

## Architecture Overview

### Key Components

1. **bin/xmpd-search** (bash): fzf wrapper, restructured into two modes with keybind toggling
2. **xmpd/providers/tidal.py**: Extended `report_play()` with session capture and event-batch POST
3. **xmpd/playlist_patcher.py** (new): Patches M3U/XSPF files and MPD queue tags after like toggles

### Data Flow

```
Search:  User types -> fzf (Search mode, 350ms debounce) -> xmpctl search-json -> daemon
         User presses Enter -> fzf (Browse mode, local filter) -> action keybinds active

Tidal Report:  Stream resolved (capture session ID + quality) -> play >30s -> build SQS payload -> POST event-batch

Like Patch:  like-toggle command -> provider API -> success -> patch playlist files + patch MPD queue tags
```

### Technology Stack

- **Language**: Python 3.11+, Bash
- **Key Libraries**: tidalapi, python-mpd2, aiohttp, requests
- **Testing**: pytest, pytest-asyncio
- **Build**: uv

---

## Phase Overview

> **Detailed phase plans are in `phase_plans/PHASE_XX.md`.**
> Only read the plan file for your assigned phase to save context.

| Phase | Name | Objective (one line) | Dependencies |
|-------|------|---------------------|--------------|
| 1 | Two-Mode fzf Search | Redesign xmpd-search with Search/Browse modes and 350ms debounce | None |
| 2 | Tidal Play Reporting | Replace no-op report_play with real event-batch POST to Tidal | None |
| 3 | Like-Toggle Playlist Patching | Update [+1] indicators in playlists and MPD queue after like toggle | None |

---

## Phase Dependencies Graph

```
Phase 1 (easy)   -- bin/xmpd-search (bash)
Phase 2 (hard)   -- xmpd/providers/tidal.py
Phase 3 (medium) -- xmpd/playlist_patcher.py (new), xmpd/daemon.py

All three are independent -- no inter-phase dependencies.
```

---

## Cross-Cutting Concerns

### Code Style

- Follow PEP 8 for Python, shellcheck-clean for bash
- Use type hints for all function signatures
- Maximum line length: 100 characters

### Error Handling

- Provider methods are best-effort: return False on failure, never raise
- Log errors before returning failure
- Use custom exceptions from `xmpd/exceptions.py`

### Logging (MANDATORY)

**Logging is already established.** All modules use Python `logging` via `logger = logging.getLogger(__name__)`. Output goes to `~/.config/xmpd/xmpd.log`. All new code must use the same pattern. Phase agents must check logs after running or deploying code.

- **Framework**: Python `logging`
- **Output**: `~/.config/xmpd/xmpd.log`
- **Format**: Standard Python logging format
- **Levels**: DEBUG for development, INFO for production

### Configuration

- All config in `~/.config/xmpd/config.yaml`
- Loaded via `xmpd/config.py`
- New config keys: none required for Phase 1; Phase 2 may need client token config; Phase 3 uses existing `like_indicator` and `favorites_playlist_name_per_provider` config

### Testing Strategy

- Unit tests for all new modules and functions
- Tests in `tests/` following existing naming: `test_{module}.py`
- Run with `uv run pytest tests/ -v`
- Live verification against running xmpd service for end-to-end validation

---

## Integration Points

### Phase 2 (Tidal Report) <-> HistoryReporter

HistoryReporter calls `provider.report_play(track_id, duration_seconds)`. The Tidal provider's implementation changes internally but the interface stays the same.

### Phase 3 (Like Patch) <-> Daemon

`_cmd_like_toggle` in daemon.py calls the new patching functions after a successful like/unlike.

---

## Glossary

**Search mode**: fzf state where typing fires debounced API searches, local filtering disabled
**Browse mode**: fzf state where typing filters locally over locked-in results, action keybinds active
**SQS format**: Amazon SQS SendMessageBatchRequestEntry encoding used by Tidal's event-batch API
**playback_session**: Tidal play-log event type for reporting track plays
**Like indicator**: `[+1]` tag appended to track titles in playlists for liked tracks

---

## References

- Tidal event-batch API: `https://tidal.com/api/event-batch` (undocumented, reverse-engineered)
- fzf documentation: `man fzf` and `https://github.com/junegunn/fzf`
- python-mpd2: `https://github.com/Mic92/python-mpd2`

---

**Instructions for Agents**:
1. **First**: Run `pwd` and verify you're in `/home/tunc/Sync/Programs/xmpd`
2. Read your phase plan from `phase_plans/PHASE_XX.md` (NOT the entire PROJECT_PLAN.md)
3. Check the dependencies to understand what should already exist
4. Follow the detailed requirements exactly
5. Meet all completion criteria before marking phase complete
6. Create your summary in `summaries/PHASE_XX_SUMMARY.md`
7. Update `STATUS.md` when complete

**Remember**: All file paths in this plan are relative to `/home/tunc/Sync/Programs/xmpd`

**Context Budget Note**: Each phase targets ~120k total tokens (reading + implementation + thinking + output). Phase plans are individual files to minimize reading overhead. If a phase runs out of context, note it in your summary and suggest splitting.
