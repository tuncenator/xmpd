# xmpd - Project Plan

**Feature/Initiative**: search-and-tidal-quality
**Type**: Bugfix (branch: bugfix/search-and-tidal-quality)
**Created**: 2026-04-29
**Estimated Total Phases**: 4

---

## Project Location

**IMPORTANT: All paths in this document are relative to the project root.**

- **Project Root**: `/home/tunc/Sync/Programs/xmpd`
- **Verify with**: `pwd` -> should output `/home/tunc/Sync/Programs/xmpd`

---

## Project Overview

### Purpose

Fix all bugs under the "Search action bugs" and "Tidal stream quality bugs" sections in `todo.md`. Search results currently can't be played (missing TrackStore registration), radio targets the wrong track, ctrl-q closes the terminal, dead code clutters the codebase, and Tidal streams default to lowest quality.

### Scope

**In Scope**:
- TrackStore registration for play/queue/multi-select from search
- ctrl-r radio targeting the fzf-selected track instead of currently playing
- Rebinding ctrl-q to a non-conflicting key
- Removing dead `xmpctl search` command and `_cmd_search` daemon handler
- ffmpeg `-map` for DASH stream selection (highest quality)
- Per-track quality labels in search results

**Out of Scope**:
- Two-mode fzf search redesign (separate todo item)
- Tidal play reporting
- Like-toggle playlist patching
- Any new features beyond fixing listed bugs

### Success Criteria

- [ ] Searching and pressing enter plays the track (proxy returns audio, not 404)
- [ ] Queuing from search adds a playable track to MPD
- [ ] ctrl-r from search creates radio based on the selected track
- [ ] Queue keybinding works without closing the terminal
- [ ] Multi-select actions (ctrl-a, ctrl-p) queue/play all selected tracks
- [ ] `xmpctl search` command and `_cmd_search` daemon handler removed
- [ ] Tidal tracks play at highest available quality (not lowest)
- [ ] Search results show actual per-track quality labels

---

## Architecture Overview

### Key Components

1. **Daemon** (`xmpd/daemon.py`): Command handlers for play, queue, radio, search-json
2. **Stream Proxy** (`xmpd/stream_proxy.py`): HTTP proxy that resolves and streams audio
3. **TrackStore** (`xmpd/track_store.py`): SQLite metadata store bridging daemon commands and proxy lookups
4. **Providers** (`xmpd/providers/`): Tidal and YouTube Music API clients
5. **CLI** (`bin/xmpctl`): Client that sends commands to daemon
6. **Search UI** (`bin/xmpd-search`): fzf wrapper calling xmpctl

### Data Flow

```
xmpd-search (fzf) -> xmpctl play/queue -> daemon _cmd_play/_cmd_queue
  -> [MISSING: track_store.add_track()] -> proxy URL added to MPD
  -> MPD requests proxy URL -> stream_proxy -> track_store.get_track() -> 404!
```

### Technology Stack

- **Language**: Python 3.11+
- **Async**: aiohttp (stream proxy)
- **Audio**: MPD + ffmpeg (DASH transcoding)
- **Search UI**: fzf (bash wrapper)
- **Testing**: pytest + pytest-asyncio
- **Config**: YAML (`~/.config/xmpd/config.yaml`)

---

## Phase Overview

> **Detailed phase plans are in `phase_plans/PHASE_XX.md`.**

| Phase | Name | Objective (one line) | Dependencies |
|-------|------|---------------------|--------------|
| 1 | TrackStore Registration | Fix play/queue/multi-select to register tracks before adding to MPD | None |
| 2 | Dead Code Removal + Key Rebind | Remove old search command, rebind ctrl-q to non-conflicting key | None |
| 3 | Radio Targeting Fix | Fix ctrl-r to create radio from fzf-selected track, not currently playing | None |
| 4 | Tidal Quality Fixes | Fix DASH stream selection and per-track quality labels | None |

---

## Phase Dependencies Graph

```
Phase 1 (TrackStore)
Phase 2 (Dead Code + Keybind)     All four phases are
Phase 3 (Radio Targeting)         functionally independent.
Phase 4 (Tidal Quality)           Batching is driven by
                                  file contention, not logic.
```

---

## Cross-Cutting Concerns

### Code Style

- Follow existing codebase conventions
- Use type hints for all function signatures
- No unnecessary comments

### Error Handling

- Use existing exception types from `xmpd/exceptions.py`
- Log errors before raising
- Proxy returns appropriate HTTP status codes (404, 500)

### Logging (MANDATORY)

Logging is ALREADY set up in this project. No new infrastructure needed.

- **Framework**: Python standard `logging` module
- **Output**: Both file (`~/.config/xmpd/xmpd.log`) and stdout
- **Format**: `[timestamp] [level] [module] message`
- **Levels**: Configurable via `config.yaml` `log_level` (default INFO)
- **Logger creation**: `logger = logging.getLogger(__name__)` in each module

All phases must use existing logging. Add `logger.info()` / `logger.debug()` calls for new code paths.

### Testing Strategy

- Existing test suite: pytest with 42 test files
- Add/update tests for modified functions
- **CRITICAL**: Every phase must include manual verification by actually running the feature end-to-end (start daemon, search, play/queue, check logs). Type checks and test suites alone are insufficient.

---

## Integration Points

### Search -> Play/Queue -> Proxy

The core bug chain: `xmpd-search` -> `xmpctl` -> daemon `_cmd_play`/`_cmd_queue` -> proxy. Fix requires TrackStore registration in the daemon before proxy URL construction.

### DASH Manifest -> ffmpeg

Tidal's manifest API returns multi-quality DASH manifests. ffmpeg must select the highest quality stream, not default to first (lowest).

---

## Glossary

**TrackStore**: SQLite database mapping `(provider, track_id)` to stream metadata
**Proxy URL**: `http://localhost:8080/proxy/{provider}/{track_id}` -- MPD plays these
**DASH**: Dynamic Adaptive Streaming over HTTP -- Tidal's streaming format
**clerk**: tmux-based TUI frontend for MPD

---

## References

- `todo.md` in project root: full bug descriptions and investigation notes
- Existing correct pattern: `_cmd_radio` (daemon.py:957-967) for TrackStore registration
