# Phase 4: Search Actions

**Feature**: better-search-like-radio
**Estimated Context Budget**: ~50k tokens

**Difficulty**: medium

**Execution Mode**: sequential
**Batch**: 3

---

## Objective

Wire fzf keybindings in the interactive search for all track actions: play single, queue single, start radio from cursor, multi-select with queue or clear-and-play.

---

## Deliverables

1. fzf keybindings in `bin/xmpd-search` for all actions
2. Action handler logic (extracting provider/track_id from selected lines, sending commands)
3. Multi-select support with batch operations
4. Visual feedback on action execution (fzf header update or notification)
5. Tests for action dispatch logic

---

## Detailed Requirements

### 1. Keybinding Map

| Key | Action | Scope |
|-----|--------|-------|
| `enter` | Play selected track | Single |
| `ctrl-q` | Queue selected track | Single |
| `ctrl-r` | Start radio from selected track | Single |
| `tab` | Toggle multi-select on current line | Multi-select |
| `ctrl-a` | Queue all selected tracks | Multi-selection |
| `ctrl-p` | Clear playlist and play all selected | Multi-selection |

These keys must not conflict with fzf's built-in bindings. `tab` is already fzf's default multi-select toggle. `enter` is fzf's default accept.

### 2. Action Execution

Each action needs to:
1. Extract `provider` and `track_id` from the selected line (fields 1 and 2, tab-separated)
2. Send the appropriate command via `xmpctl`
3. Provide feedback (print confirmation or update fzf header)

For single-track actions, use fzf's `--bind "key:execute(...)"`:

```bash
--bind "enter:execute-silent(xmpctl play {1} {2})+abort"
--bind "ctrl-q:execute-silent(xmpctl queue {1} {2})"
--bind "ctrl-r:execute-silent(xmpctl radio --provider {1} --track-id {2} --apply)+abort"
```

Note: `{1}` and `{2}` in fzf refer to the first and second fields of the selected line (provider and track_id, with `--delimiter=$'\t'`).

For multi-select actions, fzf can output all selected lines on exit. Use `--bind "ctrl-a:accept"` and `--bind "ctrl-p:accept"`, then handle the action after fzf exits based on which key was pressed.

fzf's `--expect` flag captures which key triggered the exit:

```bash
fzf ... --expect=ctrl-a,ctrl-p
```

On exit, fzf prints:
```
ctrl-a           <- the key that was pressed
line1            <- first selected line
line2            <- second selected line
...
```

The wrapper script reads the first line to determine the action, then processes the remaining lines.

### 3. xmpctl Command Support

Check what commands `xmpctl` already supports and what needs to be added:

- `xmpctl play {provider} {track_id}` -- should already exist (from search flow)
- `xmpctl queue {provider} {track_id}` -- check if this exists; if not, add it
- `xmpctl radio --provider {provider} --track-id {track_id} --apply` -- check the current radio invocation syntax in `bin/xmpctl`

For multi-select queue: loop over selected lines and call `xmpctl queue` for each.

For multi-select clear-and-play: clear MPD queue (`mpc clear`), then add all selected tracks, then `mpc play`.

### 4. Batch Queue Command

For efficiency, consider adding a batch command to the daemon:

```
queue-batch <provider1> <track_id1> <provider2> <track_id2> ...
```

This avoids N socket round-trips for N selected tracks. If this is too invasive, just loop `xmpctl queue` calls -- it's fine for typical multi-select sizes (5-20 tracks).

### 5. Visual Feedback

After executing an action:
- **Play**: fzf should close (action exits the search)
- **Queue**: fzf stays open, show brief confirmation. Use `execute(echo "Queued: {3..}" && sleep 0.5)` or update the fzf header.
- **Radio**: fzf should close (radio replaces the playlist)
- **Multi-select queue**: fzf closes after queueing all selected
- **Multi-select play**: fzf closes after replacing playlist

### 6. Key Help Header

Show available keybindings in the fzf header:

```
Search: enter=play | ctrl-q=queue | ctrl-r=radio | tab=select | ctrl-a=queue-all | ctrl-p=play-all
```

Use `--header` in fzf.

### 7. Radio Action Detail

The radio command needs the provider and track_id of the track under the cursor. When invoked:
1. Extract provider and track_id from selected line
2. Call `xmpctl radio --provider {provider} --track-id {track_id} --apply`
3. This should: get radio tracks from provider, create radio playlist, clear MPD, load and play

Verify the current `xmpctl radio` command accepts `--provider` and `--track-id` flags. The existing radio flow (commit bf61c63, 50ab99e) may already support this. Read `bin/xmpctl:cmd_radio()`.

---

## Dependencies

**Requires**: Phase 3 (Interactive fzf Search) -- needs the fzf search interface with tab-separated hidden fields

**Enables**: Phase 5 (Real-time Like Updates) -- Phase 5 adds like/unlike as another action

---

## Completion Criteria

- [ ] `enter` plays selected track and closes search
- [ ] `ctrl-q` queues selected track (search stays open)
- [ ] `ctrl-r` starts radio from selected track and closes search
- [ ] `tab` toggles multi-select on tracks
- [ ] `ctrl-a` queues all selected tracks and closes search
- [ ] `ctrl-p` clears playlist, adds all selected, plays, and closes search
- [ ] Key help shown in fzf header
- [ ] All xmpctl commands used by actions work correctly
- [ ] Existing tests pass: `uv run pytest tests/ -q`
- [ ] Manual verification: open search, search for songs, test every action
  - Play a single Tidal track via enter
  - Queue a YT track via ctrl-q
  - Start radio from a Tidal track via ctrl-r
  - Multi-select 3 tracks, queue-all via ctrl-a
  - Multi-select 3 tracks, play-all via ctrl-p (verify playlist was cleared first)

---

## Testing Requirements

- Test action dispatch logic: given a selected line, verify correct xmpctl command is constructed
- Test multi-select parsing: given fzf output with multiple lines, verify all tracks are processed
- Test edge cases: no selection (enter with nothing selected), single item in multi-select mode
- Manual testing of every action with actual playback is required

---

## Notes

- fzf's `{1}`, `{2}`, `{3..}` field references depend on `--delimiter`. Make sure the tab delimiter is set correctly.
- `execute-silent` vs `execute`: use `execute-silent` when you don't want fzf to show the command output. Use `execute` if you want to show feedback.
- Some actions close fzf (`+abort` after execute), others keep it open. Be deliberate about which ones close.
- The user's existing `C-r` keybinding in clerk calls `xmpctl radio --apply`. The search radio action should work similarly but with the selected track as seed instead of the current playing track.
