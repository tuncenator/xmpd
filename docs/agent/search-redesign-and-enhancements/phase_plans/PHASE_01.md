# Phase 1: Two-Mode fzf Search

**Feature**: search-redesign-and-enhancements
**Estimated Context Budget**: ~50k tokens

**Difficulty**: easy
**Visual**: no

**Execution Mode**: parallel
**Batch**: 1

---

## Objective

Redesign `bin/xmpd-search` with two distinct modes (Search mode and Browse mode), 350ms debounce, and mode-aware keybind toggling. The current single-mode implementation fires API searches on every keystroke with 0.15s debounce and keeps all action keybinds always active.

---

## Deliverables

1. Modified `bin/xmpd-search` -- two-mode fzf search with Search/Browse modes
2. Modified `tests/test_search_fzf_format.py` or new `tests/test_xmpd_search_modes.py` -- tests for the mode logic (where testable)

---

## Detailed Requirements

### Current state of `bin/xmpd-search`

The script is at `bin/xmpd-search`. Read it first. Key observations:
- Single fzf invocation with `--disabled` (API-driven search only, no local filtering)
- `change:reload` fires `sleep 0.15; xmpctl search-json --format fzf {q}` on every keystroke
- All keybinds always active: enter=play, ctrl-e=queue, ctrl-r=radio, ctrl-l=like-toggle, tab=select, ctrl-a=queue-all, ctrl-p=play-all
- `--expect='ctrl-a,ctrl-p'` for multi-select actions

### Implementation: Two-mode fzf

Use fzf's built-in features (requires fzf 0.30+):

**Search mode (initial state)**:
- `--disabled` is on (no local filtering)
- `change:reload` fires API search with 350ms debounce: `sleep 0.35; xmpctl search-json --format fzf {q}`
- Only Enter is bound (transitions to Browse mode)
- Prompt: `"Search: "`
- Action keybinds (ctrl-e, ctrl-r, ctrl-l, tab, ctrl-a, ctrl-p) are unbound
- Second press of Esc (or Esc when query is empty) quits fzf

**Browse mode (after Enter)**:
- `--disabled` turns off via `enable-search` action (enables local fuzzy filtering)
- No API calls fire (change:reload is unbound or no-op)
- All action keybinds become active: enter=play, ctrl-e=queue, ctrl-r=radio, ctrl-l=like-toggle, tab=select
- Prompt: `"Browse: "`
- Esc returns to Search mode: `disable-search` + `change-prompt(Search: )` + rebind Enter to transition + unbind action keybinds + restore query text

**fzf actions for mode transitions**:

Enter in Search mode (transition to Browse):
```
enter:unbind(change)+enable-search+change-prompt(Browse: )+rebind(enter,ctrl-e,ctrl-r,ctrl-l,tab)+transform-query(echo {q})
```

Esc in Browse mode (return to Search):
```
esc:disable-search+change-prompt(Search: )+unbind(enter,ctrl-e,ctrl-r,ctrl-l,tab)+rebind(change)+transform-query(echo {q})
```

**Canonical reference**: fzf's own ripgrep/fzf mode-switching example in `ADVANCED.md` (https://github.com/junegunn/fzf/blob/master/ADVANCED.md) uses this exact pattern. Study this example:

```bash
# From fzf ADVANCED.md -- ripgrep mode (Search) / fzf mode (Browse) switching
fzf --ansi --disabled --query "$INITIAL_QUERY" \
    --bind "start:reload($RG_PREFIX {q})+unbind(ctrl-r)" \
    --bind "change:reload:sleep 0.1; $RG_PREFIX {q} || true" \
    --bind "ctrl-f:unbind(change,ctrl-f)+change-prompt(2. fzf> )+enable-search+rebind(ctrl-r)+transform-query(echo {q} > /tmp/rg-fzf-r; cat /tmp/rg-fzf-f)" \
    --bind "ctrl-r:unbind(ctrl-r)+change-prompt(1. ripgrep> )+disable-search+reload($RG_PREFIX {q} || true)+rebind(change,ctrl-f)+transform-query(echo {q} > /tmp/rg-fzf-f; cat /tmp/rg-fzf-r)" \
    --prompt '1. ripgrep> '
```

Key pattern: `unbind(change)` when entering Browse mode prevents `change:reload` from firing during local filtering. `rebind(change)` restores API search when returning to Search mode. `transform-query` with temp files preserves query text across mode switches.

**Version requirements** (from fzf changelog):
- `rebind` / `unbind`: fzf 0.30.0
- `transform-query`: fzf 0.36.0
- `enable-search` / `disable-search` / `change-prompt`: same range

The key fzf actions used:
- `enable-search` / `disable-search`: toggle local filtering
- `change-prompt(...)`: change the prompt string
- `rebind(key1,key2,...)` / `unbind(key1,key2,...)`: toggle keybinds
- `transform-query(cmd)`: set the query string from command output

**Important edge cases**:

1. **Esc handling**: In Search mode, Esc should quit fzf (abort). In Browse mode, Esc returns to Search mode. Use fzf's `--bind 'esc:...'` with mode-dependent behavior. One approach: in Browse mode, bind esc to the transition-back action; in Search mode, leave esc at default (abort).

2. **Multi-select (ctrl-a, ctrl-p)**: These use `--expect` and post-fzf processing. They should only work in Browse mode. In Search mode, unbind them or use `rebind`/`unbind`.

3. **ctrl-l (like-toggle with reload)**: In the current implementation, ctrl-l does `execute-silent(xmpctl like-toggle ...)+reload(...)`. In Browse mode, the reload would re-fire the API search and lose the Browse state. Instead, in Browse mode ctrl-l should do `execute-silent(xmpctl like-toggle ...)` without reload, and the user can Esc back to Search mode to get fresh results.

4. **Debounce**: Replace `sleep 0.15` with `sleep 0.35` in the reload command.

5. **Header**: Update the header to reflect mode-specific keybinds. In Search mode, show minimal legend (just "enter=browse"). In Browse mode, show the full legend.

### Step-by-step implementation order

1. Read the current `bin/xmpd-search` to understand the exact fzf options and keybindings.
2. Check fzf version: `fzf --version`. Confirm >= 0.30 for rebind/unbind support.
3. Rewrite the fzf invocation with the two-mode structure:
   a. Start in Search mode (--disabled, only Enter bound for transition)
   b. Enter transitions to Browse mode (enable-search, rebind action keys)
   c. Esc in Browse transitions back to Search mode (disable-search, unbind action keys)
   d. 350ms debounce on the reload command
4. Handle multi-select (ctrl-a, ctrl-p) -- only active in Browse mode.
5. Update the header/legend for each mode.
6. Test manually:
   a. Type a query, verify debounce works (only one API call after 350ms)
   b. Press Enter, verify Browse mode: local filtering works, action keybinds active
   c. Press Esc, verify Search mode returns with query preserved
   d. Press Esc again, verify fzf quits
   e. In Browse mode, test play (enter), queue (ctrl-e), radio (ctrl-r), like-toggle (ctrl-l)

### What NOT to change

- Do NOT modify `bin/xmpctl` or `xmpd/daemon.py` -- the backend search-json and action commands are unchanged.
- Do NOT change the fzf output format (tab-separated provider/track_id/display).
- Keep the existing multi-select post-processing logic (ctrl-a, ctrl-p with --expect).

---

## Dependencies

**Requires**: None (first phase, no prior phases needed)

**Enables**: Nothing (all phases are independent)

---

## Completion Criteria

- [ ] Search mode: typing fires debounced API calls (350ms), local filtering disabled, action keybinds inactive
- [ ] Browse mode: Enter transitions from Search, local fuzzy filtering active, all action keybinds work
- [ ] Mode switching: Esc in Browse returns to Search with query preserved; Esc in Search quits
- [ ] Debounce: fast typing ("led zeppelin") fires one API call after the final keystroke + 350ms
- [ ] All action keybinds work in Browse mode: enter=play, ctrl-e=queue, ctrl-r=radio, ctrl-l=like, tab=select, ctrl-a=queue-all, ctrl-p=play-all
- [ ] Header updates per mode
- [ ] Existing test suite passes: `uv run pytest tests/ -v`

---

## Testing Requirements

- Run `uv run pytest tests/ -v` to confirm no regressions
- Manual testing of the full Search -> Browse -> Search -> quit flow
- Manual testing of all action keybinds in Browse mode
- Verify debounce by watching daemon logs (`tail -f ~/.config/xmpd/xmpd.log`) during typing

---

## Technical Reference

### fzf mode-switching features (from official docs, context7)

**Source**: https://github.com/junegunn/fzf/blob/master/ADVANCED.md, "Switching between Ripgrep mode and fzf mode"

The canonical two-mode pattern:
1. Start with `--disabled` (external search mode, no local filtering)
2. `change:reload` fires the external search command
3. Mode switch to fzf: `unbind(change)+enable-search+change-prompt(...)+rebind(...)+transform-query(...)`
4. Mode switch back: `disable-search+rebind(change)+change-prompt(...)+unbind(...)+transform-query(...)`

**Query preservation** uses temp files:
```bash
transform-query(echo {q} > /tmp/search-query; cat /tmp/browse-query)
```
This saves the current query to one file and restores the other mode's query from another file.

**Key version requirements**:
- fzf >= 0.30.0 for `rebind`/`unbind`
- fzf >= 0.36.0 for `transform-query`

---

## Notes

- The `--expect` flag for ctrl-a/ctrl-p may interact with `rebind`/`unbind`. Test this interaction.
- The `change:reload` binding fires on every query change. In Browse mode (after `enable-search`), local filtering changes the query, which could fire `change:reload`. You MUST `unbind(change)` when entering Browse mode to prevent this. This is the core of the pattern from fzf's ADVANCED.md.
- `transform-query` preserves the query text across mode switches via temp files. Essential for the Esc-back flow.
- Verify fzf version on the system: `fzf --version`. Manjaro typically ships recent fzf.
