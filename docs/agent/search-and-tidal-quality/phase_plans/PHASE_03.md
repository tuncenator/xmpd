# Phase 3: Radio Targeting Fix

**Feature**: search-and-tidal-quality
**Estimated Context Budget**: ~60k tokens

**Difficulty**: medium

**Execution Mode**: sequential
**Batch**: 3

---

## Objective

Fix ctrl-r in `bin/xmpd-search` so that pressing it creates a radio station seeded from the fzf-highlighted track, not from whatever is currently playing in MPD. The root cause needs investigation -- the code path looks correct on paper but the wrong track gets used in practice.

---

## Deliverables

1. Root cause identified and documented
2. Fix applied to the appropriate file(s)
3. Manual verification that ctrl-r from search creates radio from the selected track

---

## Detailed Requirements

### Investigation Strategy

The bug report identifies three possible causes. Investigate them in order of likelihood:

**Hypothesis 1: Silent error swallowing**

The fzf binding uses `execute-silent`:
```
ctrl-r:execute-silent(xmpctl radio --provider {1} --track-id {2} --apply)+abort
```

`execute-silent` suppresses ALL output, including errors. If `xmpctl radio` fails (bad args, daemon error), the error is invisible. fzf aborts (closes), and whatever was playing before continues, giving the illusion that radio was created from the current song.

To test:
1. Add a temporary debug line to `cmd_radio` in xmpctl that writes args to a temp file:
   ```python
   with open("/tmp/xmpd-radio-debug.txt", "w") as f:
       f.write(f"provider={provider} track_id={track_id} apply={apply}\n")
   ```
2. Run `bin/xmpd-search`, search, highlight a track, press ctrl-r
3. Check `/tmp/xmpd-radio-debug.txt` -- does it show the correct provider and track_id?
4. Check daemon log (`~/.config/xmpd/xmpd.log`) -- did the radio command arrive? With what args?

**Hypothesis 2: Trailing whitespace or ANSI escapes in fzf field extraction**

fzf's `{1}` and `{2}` extract fields by whitespace splitting. If the search result lines contain ANSI color codes, tab characters, or trailing spaces, the extracted values may be corrupted.

To test:
1. Check the fzf output format in `bin/xmpd-search` -- how are fields delimited?
2. Check `cmd_search_json --format fzf` output for embedded ANSI or unusual whitespace
3. In the debug file from Hypothesis 1, check if provider or track_id have unexpected characters

**Hypothesis 3: --apply flag failure**

The `--apply` flag in xmpctl triggers `mpc clear && mpc load {playlist} && mpc play`. If the daemon call succeeds but the MPD apply fails, the old playlist/playback remains.

To test:
1. After the debug from Hypothesis 1, check if the daemon actually created a radio playlist
2. Check if `mpc lsplaylists` shows a new radio playlist
3. Try manually: `mpc clear && mpc load {radio-playlist} && mpc play`

### Fix Implementation

Based on investigation findings, apply the targeted fix. Common patterns:

- If field extraction is broken: fix the fzf `--delimiter` or field references
- If silent error: change `execute-silent` to `execute` temporarily to surface errors, fix the underlying error, then restore `execute-silent`
- If args corrupted: add `.strip()` or sanitization in xmpctl `cmd_radio`
- If `--apply` race condition: add error checking to the apply logic

### After fixing, verify the full chain:

1. `bin/xmpd-search` extracts correct provider + track_id from fzf selection
2. `xmpctl radio --provider X --track-id Y --apply` sends correct command to daemon
3. Daemon `_cmd_radio` receives the provided track_id (not None)
4. Daemon creates radio from the specified track (check log: "Creating radio from {track_id}")
5. MPD loads and plays the new radio playlist

---

## Dependencies

**Requires**: None (functionally independent)

**Enables**: None (final phase)

---

## Completion Criteria

- [ ] Root cause identified and documented in phase summary
- [ ] Fix applied and committed
- [ ] Tests pass: `pytest tests/ -v`
- [ ] **Manual verification**: Run `bin/xmpd-search`, search for a specific track, press ctrl-r, confirm:
  - Daemon log shows radio created from the SELECTED track (check track_id in log)
  - MPD plays the radio playlist (not the previously playing song)
  - The first track in the radio playlist is the seed track or closely related
- [ ] **Manual verification**: Repeat with a different track to confirm it's not a coincidence

---

## Testing Requirements

- Investigation may not require new automated tests (bug might be in bash/fzf layer)
- If the fix is in Python (xmpctl or daemon), add a test for the fixed behavior
- Manual testing is the primary verification for this phase

---

## Notes

- Start with Hypothesis 1 (silent error swallowing) -- it's the most likely cause and easiest to diagnose
- The debug file approach (`/tmp/xmpd-radio-debug.txt`) avoids needing an interactive terminal
- Clean up any debug code before final commit
- This phase requires a running daemon and fzf for manual testing. If the environment doesn't support this, document what you found and what remains untested
