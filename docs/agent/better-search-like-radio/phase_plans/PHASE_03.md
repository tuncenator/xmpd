# Phase 3: Interactive fzf Search

**Feature**: better-search-like-radio
**Estimated Context Budget**: ~70k tokens

**Difficulty**: hard

**Execution Mode**: sequential
**Batch**: 2

---

## Objective

Build an fzf-based interactive search with live-as-you-type results, provider-colored output (Tidal teal, YT pink), quality tier badges, and liked [+1] indicators. Replace the current `C-s` keybinding in clerk.

---

## Deliverables

1. New search frontend script (e.g., `bin/xmpd-search`) using fzf with `change:reload()` for live search
2. ANSI-colored output formatter that colors results by provider
3. Quality badge display (HR/CD/Lo) per result
4. Liked indicator [+1] per result
5. Updated clerk keybinding to use the new search
6. Tests for the output formatter

---

## Detailed Requirements

### 1. fzf Architecture

The search uses fzf's dynamic reload mechanism:

```bash
fzf --ansi \
    --disabled \
    --bind "change:reload:xmpctl search-json {q} | xmpd-search-format" \
    --bind "start:reload:echo ''" \
    --preview-window=hidden \
    --header="..." \
    --prompt="Search: "
```

Key fzf flags:
- `--ansi`: Enable ANSI color interpretation
- `--disabled`: Disable fzf's built-in fuzzy matching (we're doing server-side search)
- `--bind "change:reload:..."`: On every keystroke, re-run the search command
- Query is passed as `{q}` to the reload command

### 2. Search Flow

```
User types query
    -> fzf sends {q} to reload command
    -> xmpctl search-json {q} returns NDJSON
    -> formatter script (xmpd-search-format or inline) converts to colored display lines
    -> fzf displays formatted lines
```

The formatter converts each JSON line to a display line like:

```
[TD] HR [+1] Radiohead - Creep (3:59)
[YT] Lo      Radiohead - Creep (3:59)
```

### 3. Output Format Specification

Each fzf display line must encode the provider and track_id for action handling (Phase 4). Use a hidden field approach:

```
{provider}\t{track_id}\t{visible_line}
```

Where `{visible_line}` is the ANSI-colored display text. fzf's `--with-nth=3..` shows only the visible part. `--delimiter='\t'` splits on tabs.

So the full fzf invocation becomes:

```bash
fzf --ansi \
    --disabled \
    --delimiter=$'\t' \
    --with-nth=3.. \
    --bind "change:reload:..." \
    ...
```

And actions (Phase 4) extract fields 1 and 2 from the selected line.

### 4. Provider Colors (ANSI True Color)

Consistent with `bin/xmpd-status` colors:

| Provider | Color | ANSI escape |
|----------|-------|-------------|
| Tidal | #73daca (teal) | `\033[38;2;115;218;202m` |
| YouTube | #f7768e (pink) | `\033[38;2;247;118;142m` |
| Reset | | `\033[0m` |

The provider tag `[TD]` or `[YT]` and the artist/title should be colored.

### 5. Quality Badge

Show quality tier after the provider tag:

| Quality | Display |
|---------|---------|
| HR | `HR` (bold or highlighted) |
| CD | `CD` |
| Lo | `Lo` (dimmed) |
| null | (no badge) |

Use ANSI dim (`\033[2m`) for Lo, bold (`\033[1m`) for HR.

### 6. Liked Indicator

If `liked: true`, show `[+1]` after the quality badge. If `liked: false` or `null`, show nothing.

### 7. Formatter Implementation

Create a formatter that reads NDJSON from stdin and outputs colored display lines. This can be:

**Option A**: A Python script `bin/xmpd-search-format` that reads JSON lines and outputs formatted lines.

**Option B**: Inline in the search command itself (`xmpctl search-json` outputs pre-formatted lines with a `--format` flag).

**Choose Option B** -- avoids spawning an extra process per keystroke. Add a `--format fzf` flag to `xmpctl search-json` that outputs pre-formatted ANSI lines instead of raw JSON:

```
xmpctl search-json --format fzf "radiohead"
# Output (tab-separated, 3rd field is ANSI colored):
# tidal\t58990486\t\033[38;2;115;218;202m[TD] CD [+1] Radiohead - Creep (3:59)\033[0m
# yt\t9RfVp-GhKfs\t\033[38;2;247;118;142m[YT] Lo Radiohead - Creep (3:59)\033[0m
```

### 8. Search Script

Create `bin/xmpd-search` (executable Python or bash script) that:

1. Launches fzf with the correct configuration
2. Handles the `change:reload` binding to call `xmpctl search-json --format fzf {q}`
3. Handles empty query gracefully (show nothing or a prompt message)
4. Returns selected line(s) on exit

This script will be extended with action keybindings in Phase 4. For now, just `enter` to select and print the result (provider + track_id) to stdout.

### 9. Clerk Keybinding Update

Update `~/.config/clerk/clerk.tmux` (line 55):

Current:
```tmux
bind-key -n C-s run-shell 'tmux new-window -n xmpd-search "xmpctl search"'
```

New:
```tmux
bind-key -n C-s run-shell 'tmux new-window -n xmpd-search "xmpd-search"'
```

### 10. Debouncing

fzf's `change:reload` fires on every keystroke. With two provider searches (Tidal + YT), this generates a lot of API calls. Consider:

- fzf supports `--bind "change:reload:sleep 0.2; ..."` for basic debounce
- Or implement debounce in the search command (skip if query < 2 chars)
- Tidal and YT both have rate limits. YTMusicProvider already has 100ms rate limiting.
- At minimum: skip empty queries and single-character queries

### 11. Error Handling

- If daemon is not running, show a message in fzf header ("xmpd not running")
- If one provider fails, still show results from the other
- If query is too short, show nothing (not an error)
- Network timeouts: search should timeout after ~3 seconds and show partial results

---

## Dependencies

**Requires**: Phase 2 (Search API Enhancement) -- needs `xmpctl search-json --format fzf` output

**Enables**: Phase 4 (Search Actions) -- Phase 4 adds keybindings to this fzf interface

---

## Completion Criteria

- [ ] `bin/xmpd-search` exists and is executable
- [ ] Running `xmpd-search` opens fzf in the terminal
- [ ] Typing a query shows live results from both Tidal and YT
- [ ] Results show provider tag [TD]/[YT] in correct colors (teal/pink)
- [ ] Results show quality badge (HR/CD/Lo)
- [ ] Liked tracks show [+1]
- [ ] Pressing enter prints the selected track's provider and track_id
- [ ] `C-s` in clerk opens the new search (clerk.tmux updated)
- [ ] Empty and single-char queries are handled gracefully
- [ ] One provider failing doesn't break the search
- [ ] Existing tests pass: `uv run pytest tests/ -q`
- [ ] New formatter tests pass
- [ ] Manual verification: open search via `C-s`, type a query, see colored results with quality badges and liked indicators

---

## Testing Requirements

- Test the fzf output formatter: given a Track with known fields, verify the ANSI output string is correct
- Test provider color mapping
- Test quality badge formatting (HR bold, Lo dim, CD normal)
- Test liked indicator presence/absence
- Test tab-separated hidden field encoding
- Test empty query handling
- Manual: actually open the search, type queries, verify visual appearance

---

## Notes

- fzf must be installed on the system. Check with `which fzf`. If not installed, document the requirement.
- The `--format fzf` flag should produce exactly one line per result, with no trailing newline issues.
- ANSI true color requires a terminal that supports it. The user's setup (tmux + i3) supports it (the i3blocks widget already uses true color).
- The search script will likely be a bash script wrapping fzf, not Python. fzf is more naturally orchestrated from bash.
- Consider making the search script detect whether `xmpd` is running before launching fzf, and showing a clear error if not.
- The Phase 4 agent will add action keybindings (play, queue, radio, multi-select) to this script.
