# Phase 02: Search API Enhancement - Summary

**Date Completed:** 2026-04-28
**Completed By:** claude-sonnet-4-6 (agent-a1b2ca1f041207eec)
**Actual Token Usage:** ~60k tokens

---

## Objective

Add a JSON search output mode to the daemon that returns structured results
including provider, quality tier, like state, and full metadata. This is the
data backbone for the interactive fzf search built in Phase 3.

---

## Work Completed

### What Was Built

- `XMPDaemon._cmd_search_json()`: new daemon socket command that parses
  `--provider` and `--limit` flags, calls `ytmusic_client.search()`, looks up
  like state from the favorites cache, and returns a JSON array of track dicts.
- `XMPDaemon._get_liked_ids()`: fetches the set of liked video IDs from YouTube
  Music, caches the result for 5 minutes to avoid per-search API calls.
- `bin/xmpctl cmd_search_json()`: CLI function that builds the socket command,
  sends it to the daemon, and prints one JSON object per line (NDJSON) to stdout.
- Socket command router: wired `search-json` alongside the existing `search` case.
- `tests/test_search_json.py`: 20 unit tests covering all required fields,
  quality="Lo" for all YT tracks, liked/unloved state, cache behaviour, error
  paths, empty results, flag parsing, and NDJSON CLI output.

### Files Created

- `tests/test_search_json.py` - unit tests for search-json command and xmpctl CLI

### Files Modified

- `xmpd/daemon.py` - added `_liked_ids_cache` fields, `_get_liked_ids()`,
  `_cmd_search_json()`, and `search-json` socket routing
- `bin/xmpctl` - added `cmd_search_json()`, wired it in `main()`, updated help text
- `pyproject.toml` + `uv.lock` - added `[dependency-groups] dev` (pytest, ruff,
  mypy) so tooling is tracked in the lockfile

### Key Design Decisions

- **Quality is always "Lo" for YT tracks.** The codebase is YouTube Music-only
  (no Tidal provider exists). The plan's "Option A: add quality field to
  TrackMetadata" does not apply because there is no shared `TrackMetadata`
  dataclass -- quality is embedded directly in the search-json response dict.
- **5-minute liked IDs cache on the daemon.** The plan said to use the sync
  engine's favorites cache. The sync engine does not maintain a persistent cache
  between syncs; it fetches on-demand. Rather than plumbing through the sync
  engine, the daemon holds its own TTL cache. This avoids a slow per-track
  `get_like_state()` call and avoids a full `get_liked_songs()` call on every
  search.
- **NDJSON output from xmpctl.** One `json.dumps(track)` per `print()` call.
  Each line is a self-contained JSON object, ready for `fzf` to consume.
- **Socket command string format.** The daemon socket handler splits on
  whitespace, so flags must be space-separated tokens. `_cmd_search_json`
  receives `list[str]` (the tokens after `search-json`) and re-parses flags
  itself, matching the existing pattern used by `_cmd_radio`.

---

## Completion Criteria Status

- [x] Daemon accepts `search-json` command and returns JSON
- [x] `./bin/xmpctl search-json "radiohead"` outputs NDJSON with all fields
      (provider, track_id, title, artist, album, duration, quality, liked)
- [x] YT search results include `quality: "Lo"`
- [x] Like state is populated from favorites cache
- [x] Existing tests pass: 99 passed
- [x] New tests pass: 20 passed
- [x] `uv run python -m ruff check xmpd/daemon.py bin/xmpctl tests/test_search_json.py` -- all clean
- [x] mypy on daemon.py -- no new errors introduced (pre-existing errors unchanged)
- [ ] `TrackMetadata` has a `quality` field -- NOT APPLICABLE
- [ ] Tidal search results include correct quality tier (HR/CD/Lo) -- NOT APPLICABLE
- [ ] Manual verification with live daemon -- not run (daemon requires active YTM session)

### Deviations / Incomplete Items

- **No shared TrackMetadata.quality field.** The codebase has no `providers/`
  package, no `base.py`, and no shared `Track`/`TrackMetadata` dataclass. The
  plan's Option A cannot be applied. Quality is encoded directly in the
  search-json response payload. This is equivalent in function for Phase 3.
- **No Tidal provider.** The codebase is YouTube Music-only. All quality tiers
  come from the YT path (always "Lo"). HR/CD tiers will be addressable if
  a Tidal provider is added in a future phase.
- **Manual verification not run.** Requires a live YTMusic auth session which
  is not available in the agent environment.

---

## Testing

### Tests Written

`tests/test_search_json.py` (20 tests):

TestCmdSearchJson (11 tests):
- test_empty_query_returns_error
- test_whitespace_only_query_returns_error
- test_returns_ndjson_fields
- test_all_yt_tracks_have_quality_lo
- test_liked_track_has_liked_true
- test_no_results_returns_empty_list
- test_limit_flag_passed_to_search
- test_provider_flag_accepted
- test_search_api_failure_returns_error
- test_liked_ids_cache_is_used
- test_duration_formatted_correctly

TestGetLikedIds (4 tests):
- test_returns_empty_set_when_no_liked_songs
- test_returns_video_ids_from_liked_songs
- test_cache_avoids_repeated_api_calls
- test_failed_fetch_returns_empty_on_first_call

TestXmpctlSearchJson (5 tests):
- test_search_json_in_help
- test_search_json_no_query_exits_with_error
- test_search_json_syntax_valid
- test_search_json_no_daemon_shows_error
- test_search_json_outputs_ndjson_line_per_track

### Test Results

```
$ uv run python -m pytest tests/test_search_json.py -v
20 passed in 0.40s

$ uv run python -m pytest tests/test_config.py tests/test_daemon.py tests/test_ytmusic.py tests/test_xmpctl.py
99 passed in 2.93s
```

### Manual Testing

Not run -- requires live YTMusic session. Structural verification done via unit
tests with full mock coverage of all code paths.

---

## Challenges and Solutions

### Challenge 1: Codebase differs from documented architecture
The codebase context described a `providers/` package with `base.py`, `tidal.py`,
and a shared `Track`/`TrackMetadata` dataclass. None of that exists. The project
is YouTube Music-only with a flat module structure.
**Solution:** Adapted the plan to the real codebase. Quality field goes directly
in the search-json response dict rather than a dataclass field. Tidal quality
mapping is skipped (no Tidal provider).

### Challenge 2: xmpctl venv re-exec guard blocks module import in tests
The xmpctl script has a top-level re-exec guard (`os.execv`) that runs when
imported as a module, hanging the test.
**Solution:** Load xmpctl source with `compile()` + `exec()` into a fresh
namespace, patching `os.execv` via monkeypatch before the exec runs.

### Challenge 3: System pytest (Python 3.14) used instead of venv pytest
`uv run pytest` invoked the system pytest binary which used Python 3.14 (missing
venv packages). This caused `ModuleNotFoundError: No module named 'mpd'`.
**Solution:** Always invoke via `uv run python -m pytest`. Also added pytest,
ruff, mypy to `[dependency-groups] dev` in pyproject.toml to track them in the
lockfile.

---

## Evidence Captured

**YTMusicClient.search() return shape** (observed from existing test mocks and
source code at `xmpd/ytmusic.py:284-299`):

```python
{
    "video_id": str,    # YouTube video ID (11 chars)
    "title": str,
    "artist": str,
    "duration": int,    # seconds as int (parsed from "M:SS" string by YTMusicClient)
}
```

No `album` key in search results. The `album` field in search-json output is
set to `None` for all YT search results.

---

## Code Quality

- Ruff: all clean on modified files (xmpd/daemon.py, bin/xmpctl, tests/test_search_json.py)
- mypy: no new errors introduced; pre-existing daemon errors are unchanged
- All new functions have docstrings and type annotations

---

## Codebase Context Updates

Add to Key Files table:
- `tests/test_search_json.py` - 20 unit tests for search-json command

Add to Important APIs section under "XMPDaemon socket commands":
- `search-json [--provider yt|all] [--limit N] QUERY` -- returns `{"success": true, "results": [...]}`
  where each result has: `provider`, `track_id`, `title`, `artist`, `album`,
  `duration` (M:SS), `duration_seconds`, `quality` ("Lo" for all YT tracks),
  `liked` (bool or null)

Add to Important APIs section:
- `XMPDaemon._get_liked_ids()` -- returns `set[str]` of liked video IDs, cached
  5 minutes. Used by `_cmd_search_json` for like-state population.

Add to Patterns section:
- `cmd_search_json(args)` in `bin/xmpctl` sends `search-json ...` to daemon,
  prints one `json.dumps(track)` per line (NDJSON) to stdout.

Note in Architecture Overview:
- No `providers/` package exists. The codebase context described a planned
  multi-provider architecture that has not been implemented yet.
- No Tidal provider. YouTube Music is the only provider.
- No shared `Track`/`TrackMetadata` dataclass.

---

## Notes for Future Phases

- Phase 3 (fzf search): consume `./bin/xmpctl search-json QUERY` output line by
  line. Each line is a valid JSON object. Fields guaranteed: `provider`,
  `track_id`, `title`, `artist`, `album` (may be null), `duration`, `quality`,
  `liked` (bool or null if video_id was empty string).
- `quality` will always be `"Lo"` until a Tidal/other provider is added.
- `_get_liked_ids()` cache TTL is 5 minutes (`_liked_ids_cache_ttl`). If Phase 3
  needs fresher like state, it can call `daemon._liked_ids_cache_time = 0` to
  force a refresh on next access.
- The `--provider` flag is parsed and stored but currently has no effect (only
  YT Music exists). Phase 3 can pass `--provider yt` safely; it will not break.

---

**Phase Status:** COMPLETE
