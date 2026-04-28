# Phase 2: Dead Code Removal + Key Rebind

**Feature**: search-and-tidal-quality
**Estimated Context Budget**: ~40k tokens

**Difficulty**: easy

**Execution Mode**: parallel
**Batch**: 2

---

## Objective

Remove the dead `xmpctl search` command and its daemon handler `_cmd_search`, and rebind ctrl-q in `bin/xmpd-search` to a non-conflicting key so the queue action works without closing the terminal.

---

## Deliverables

1. `_cmd_search` handler removed from `daemon.py` (lines ~844-895)
2. The daemon's command dispatch table no longer routes `search` to the removed handler
3. `cmd_search` command removed from `bin/xmpctl` (lines ~331-486)
4. `xmpctl`'s command dispatch no longer includes `search` as a valid command
5. ctrl-q rebound to `ctrl-e` (or another free key) in `bin/xmpd-search`
6. Tests updated to remove references to old search command

---

## Detailed Requirements

### 1. Remove `_cmd_search` from daemon.py

- Delete the `_cmd_search` method (lines ~844-895)
- Find and remove the dispatch entry that routes the `search` command to `_cmd_search`. Look for a dictionary or if/elif chain in the command dispatch logic (search for `"search"` in the dispatch section). **Be careful**: only remove the `search` entry, NOT `search-json`
- Search the file for any other references to `_cmd_search` (e.g. in help text, comments) and remove those too

### 2. Remove `cmd_search` from xmpctl

- Delete the `cmd_search` function (lines ~331-486)
- Remove the `search` entry from xmpctl's command dispatch (look for where commands are mapped to functions)
- Remove any imports or helper functions used exclusively by `cmd_search`
- Keep `cmd_search_json` and its dispatch entry intact -- that's the active search backend

### 3. Rebind ctrl-q in bin/xmpd-search

- Find the ctrl-q binding (currently: `ctrl-q:execute-silent(xmpctl queue --provider {1} --track-id {2})+abort` or similar)
- Change `ctrl-q` to `ctrl-e`
  - `ctrl-e` is not bound to any standard terminal function
  - Verify `ctrl-e` isn't already used in the fzf config (search the file for `ctrl-e`)
  - If `ctrl-e` conflicts with something, use `alt-q` instead
- Update any comments in the script that reference ctrl-q

### 4. Update tests

- Search `tests/` for any tests that reference `_cmd_search`, `cmd_search`, or send a `search` command (as opposed to `search-json`)
- Remove or update those tests
- Run the full test suite to ensure nothing breaks

---

## Dependencies

**Requires**: None

**Enables**: Phase 3 (radio targeting) benefits from the dead code being gone, since `_cmd_search` is adjacent to `_cmd_radio` in daemon.py

---

## Completion Criteria

- [ ] `_cmd_search` method no longer exists in daemon.py
- [ ] `cmd_search` function no longer exists in xmpctl
- [ ] Sending `search` command to daemon returns an error (unknown command), not results
- [ ] `search-json` command still works: `bin/xmpctl search-json "test" --format fzf` returns results
- [ ] ctrl-e (or chosen key) queues a track in `bin/xmpd-search` without closing the terminal
- [ ] Tests pass: `pytest tests/ -v`
- [ ] **Manual verification**: Run `bin/xmpd-search`, search for something, press ctrl-e, confirm the track is queued in MPD (`mpc playlist`) and fzf stays open
- [ ] **Manual verification**: Run `bin/xmpctl search "test"` and confirm it reports an unknown command

---

## Testing Requirements

- Remove tests for dead `search` command
- Verify `search-json` tests still pass
- Manual test of new queue keybinding in fzf

---

## Notes

- The dead code removal is safe. `search-json` is the only search path used by `bin/xmpd-search`
- When removing from daemon command dispatch, search for both string matching and any help/usage text that lists `search` as a command
- The ctrl-q -> ctrl-e rebind is the simplest fix. ctrl-e moves cursor to end-of-line in readline, but fzf doesn't use readline when bindings are explicitly set
