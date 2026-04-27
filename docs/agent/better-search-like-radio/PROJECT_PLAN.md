# xmpd - Project Plan

**Feature/Initiative**: better-search-like-radio
**Type**: Refactoring / Feature Enhancement
**Created**: 2026-04-28
**Estimated Total Phases**: 5

---

## Project Location

**IMPORTANT: All paths in this document are relative to the project root.**

- **Project Root**: `/home/tunc/Sync/Programs/xmpd`
- **Verify with**: `pwd` -> should output `/home/tunc/Sync/Programs/xmpd`

When you see a path like `xmpd/stream_proxy.py`, it means `/home/tunc/Sync/Programs/xmpd/xmpd/stream_proxy.py`

---

## Project Overview

### Purpose

Revamp xmpd's search functionality from a basic text prompt (`xmpctl search`) into an interactive fzf-like experience with live-as-you-type results, multi-provider support (Tidal + YouTube Music), rich visual indicators (provider colors, quality badges, liked status), and comprehensive result actions (play, queue, radio, multi-select). Also fix a recurring proxy connection limit leak that blocks all streaming after slots fill.

### Scope

**In Scope**:
- Fix stream proxy connection counter leak (still occurring despite commit a13e063)
- New JSON search API in daemon for structured search results with metadata
- Interactive fzf-based search with `change:reload()` for live results
- Provider-colored output (Tidal teal, YT pink) consistent with i3blocks widget
- Per-track quality badges in search results (Tidal: real `audio_quality`, YT: Lo)
- Liked song [+1] indicators in search results
- Search result actions: play, queue, radio from cursor, multi-select + queue/clear-and-play
- Real-time like/unlike updates visible in search view

**Out of Scope**:
- New provider integrations
- Quality indicators in playlists (not reliably knowable at sync time)
- Changes to the i3blocks widget itself
- Playlist editing/reordering
- Configuration schema changes

### Success Criteria

- [ ] Proxy connection limit never reached under normal use (playlist load + skip-through)
- [ ] Search returns live results as user types, from both providers
- [ ] Each search result shows provider color, quality tier, liked status
- [ ] All actions work: play, queue, radio, multi-select + queue, multi-select + clear-and-play
- [ ] Liking/unliking a song in search instantly updates its display
- [ ] Ctrl+S keybind in clerk opens the new interactive search

---

## Architecture Overview

### Key Components

1. **Stream Proxy** (`xmpd/stream_proxy.py`): HTTP proxy that lazily resolves stream URLs. Connection counter leak needs fixing.
2. **Search API** (new, in daemon): JSON-output search command returning structured results with quality, like state, provider info.
3. **fzf Search Frontend** (new, likely `bin/xmpd-search`): fzf wrapper with `change:reload()` for live search, ANSI-colored output, keybind actions.
4. **Provider Search**: `TidalProvider.search()` and `YTMusicProvider.search()` already exist; need to expose quality metadata.

### Data Flow

```
User types in fzf -> change:reload(xmpctl search-json {q})
                          |
                    daemon socket
                          |
              TidalProvider.search() + YTMusicProvider.search()
                          |
              JSON results (with quality, like state, provider)
                          |
                    fzf display (ANSI colors)
                          |
              User action (enter/ctrl-q/ctrl-r/tab)
                          |
                    xmpctl play/queue/radio command
```

### Technology Stack

- **Language**: Python 3.11
- **Key Libraries**: aiohttp, tidalapi, ytmusicapi, python-mpd2
- **TUI**: fzf (external, for interactive search)
- **Testing**: pytest + pytest-asyncio
- **Linting**: ruff, mypy (strict)

---

## Phase Overview

> **Detailed phase plans are in `phase_plans/PHASE_XX.md`.**
> Only read the plan file for your assigned phase to save context.

| Phase | Name | Objective (one line) | Dependencies |
|-------|------|---------------------|--------------|
| 1 | Fix Proxy Connection Leak | Diagnose and fix the recurring connection counter leak in stream_proxy.py | None |
| 2 | Search API Enhancement | Add JSON search output to daemon with quality, like state, provider metadata | Phase 1 |
| 3 | Interactive fzf Search | Build fzf-based interactive search with live results, colors, quality badges | Phase 2 |
| 4 | Search Actions | Wire fzf keybindings for play, queue, radio, multi-select operations | Phase 3 |
| 5 | Real-time Like Updates | Like/unlike in search with instant visual feedback | Phase 4 |

---

## Phase Dependencies Graph

```
Phase 1 (Proxy Fix)
    |
Phase 2 (Search API)
    |
Phase 3 (fzf Search)
    |
Phase 4 (Actions)
    |
Phase 5 (Like Updates)
```

All phases are sequential. Each builds directly on the previous.

---

## Cross-Cutting Concerns

### Code Style

- Follow PEP 8, enforced by ruff (E, F, W, I, N, UP rules)
- Type hints on all function signatures (mypy strict: `disallow_untyped_defs`)
- Maximum line length: 100 characters
- Minimal comments, only when the "why" is non-obvious

### Error Handling

- Custom exceptions in `xmpd/exceptions.py`
- Log errors with context before raising
- Graceful degradation: if one provider's search fails, still show results from the other

### Logging (MANDATORY)

Logging is already established in the project. All new code must use the existing pattern.

- **Framework**: Python `logging` module
- **Output**: File at `~/.config/xmpd/xmpd.log` + configurable via `log_level`/`log_file` in config.yaml
- **Format**: `[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s`
- **Levels**: Configurable, default INFO
- **Convention**: `logger = logging.getLogger(__name__)` at module top

Phase 1 does not need to set up logging (already done). All phases must add appropriate logging to new code and check logs after running.

### Configuration

- All config in `~/.config/xmpd/config.yaml`
- Loaded via `xmpd/config.py` with schema validation
- Defaults defined in `config.py:_DEFAULTS`
- No config schema changes expected for this feature

### Testing Strategy

- Unit tests for all new modules (pytest)
- Async tests via pytest-asyncio (`asyncio_mode = "auto"`)
- Stress tests for proxy connection leak (reproduce before fixing)
- **Manual verification required**: every phase that touches user-facing functionality must actually run the feature and confirm it works. Do not rely solely on test suites.

---

## Integration Points

### Search API <-> fzf Frontend
- JSON output format from daemon search, consumed by fzf wrapper
- Must be stable: field names, ordering, encoding

### fzf Frontend <-> xmpctl Commands
- Action keybindings call existing xmpctl commands (play, queue, radio)
- Multi-select actions need batch command support

### Provider Search <-> Shared Track Format
- Tidal search returns `audio_quality` field on raw tidalapi.Track objects
- YT Music search has no quality info; hardcode Lo
- Like state from provider's `get_like_state()` or favorites cache

---

## Data Schemas

### Search Result JSON (new)

```json
{
  "provider": "tidal",
  "track_id": "58990486",
  "title": "Creep",
  "artist": "Radiohead",
  "album": "Creep",
  "duration": "3:59",
  "quality": "CD",
  "liked": true
}
```

### Tidal audio_quality -> Quality Tier Mapping

| Tidal audio_quality | Display (compact) | Display (full) |
|--------------------|--------------------|----------------|
| HI_RES_LOSSLESS | HR | HiRes |
| LOSSLESS | CD | HiFi |
| HIGH | Lo | Lossy |
| LOW | Lo | Lossy |

### Provider Colors (ANSI, consistent with i3blocks)

| Provider | Playing (hex) | ANSI approximation |
|----------|---------------|-------------------|
| Tidal | #73daca (teal) | 38;2;115;218;202 |
| YouTube | #f7768e (pink) | 38;2;247;118;142 |

---

## Glossary

**Provider**: Music source backend (Tidal, YouTube Music)
**Track**: A single song with metadata (provider, track_id, title, artist, etc.)
**Stream Proxy**: HTTP server that resolves provider-specific stream URLs on demand
**DASH**: Dynamic Adaptive Streaming over HTTP, used by Tidal for HiRes content
**fzf**: Command-line fuzzy finder, used as the interactive search UI
**Quality Tier**: Lo (lossy), CD (16-bit lossless), HR (24-bit/hi-res lossless)

---

## References

- tidalapi Track attributes: `audio_quality`, `is_hi_res_lossless`, `is_lossless`, `media_metadata_tags`
- ytmusicapi search: returns `videoId`, `title`, `artists`, `album`, `duration_seconds` (no quality info)
- fzf: `--bind 'change:reload(...)'` for live search, ANSI color support via `--ansi`
- i3blocks widget colors and quality classification: `bin/xmpd-status:classify_audio_quality()`

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
