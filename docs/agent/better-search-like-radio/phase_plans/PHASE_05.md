# Phase 5: Real-time Like Updates

**Feature**: better-search-like-radio
**Estimated Context Budget**: ~40k tokens

**Difficulty**: medium

**Execution Mode**: sequential
**Batch**: 4

---

## Objective

Add like/unlike functionality to the interactive search with instant visual feedback: liking a track shows [+1] immediately, unliking removes it. Also handle tracks not in the default liked songs list.

---

## Deliverables

1. Like/unlike keybinding in fzf search
2. Instant row update mechanism (re-render the liked/unliked track's display line)
3. Like state sync between search view and provider
4. Handle non-favorites-list tracks getting liked/unliked
5. Tests for like toggle and display update logic

---

## Detailed Requirements

### 1. Like Keybinding

Add `ctrl-l` as the like/unlike toggle in fzf (consistent with clerk's `C-l` binding):

```bash
--bind "ctrl-l:execute-silent(...)+reload(...)"
```

When pressed:
1. Extract provider and track_id from selected line
2. Check current like state
3. Call `xmpctl like {provider} {track_id}` to toggle
4. Reload the search results to reflect the new state

### 2. Like Toggle Command

Check if `xmpctl like` supports provider and track_id args:

Current `C-l` in clerk calls `xmpctl like` which likes the currently playing track. For search, we need to like an arbitrary track by provider and track_id.

If `xmpctl like` doesn't accept provider/track_id args, add this mode:
```
xmpctl like {provider} {track_id}   -> likes the specified track
xmpctl unlike {provider} {track_id} -> unlikes the specified track
xmpctl like-toggle {provider} {track_id} -> toggles like state
```

The daemon handler should:
1. Determine current like state via `provider.get_like_state(track_id)`
2. If LIKED -> call `provider.unlike(track_id)`, update favorites cache
3. If NEUTRAL/DISLIKED -> call `provider.like(track_id)`, update favorites cache
4. Return new state in response

### 3. Instant Visual Update

After liking/unliking, the search results need to refresh so the [+1] indicator appears/disappears. Two approaches:

**Option A**: Full reload. After the like action, re-run the search query and re-display all results. Simple but potentially slow (re-queries both providers).

**Option B**: Partial update. Cache the current results locally and toggle the `liked` field on the affected track, then re-render. More complex but instant.

**Choose Option A for simplicity.** The like action triggers `reload` in fzf, which re-runs the search query. The search query re-fetches from daemon, which now has the updated favorites cache. fzf flickers briefly but the result is accurate.

```bash
--bind "ctrl-l:execute-silent(xmpctl like-toggle {1} {2})+reload(xmpctl search-json --format fzf {q})"
```

The `{q}` in the reload preserves the current query.

### 4. Favorites Cache Update

When a track is liked/unliked via the search interface, the daemon's in-memory favorites cache must be updated immediately (not wait for next sync cycle). Check where favorites are cached:

- `SyncEngine` builds `liked_track_ids` during sync
- This set needs to be updated when `like`/`unlike` is called outside of sync

Ensure the daemon updates its favorites cache in real-time when `like`/`unlike` commands are processed. If the cache is in `SyncEngine`, the daemon needs to call back into it to update the set.

### 5. Non-Favorites Tracks

The user wants: "a way to instantly update the row of a song that I liked with [+1] if it is not in the default liked songs list."

This means:
- A track found via search might not be in any synced playlist
- When liked, it should still show [+1] in search results
- The liked indicator in search comes from the search-json output's `liked` field, which comes from the favorites cache
- So: liking a track via search must update the favorites cache AND the provider's actual favorites (Tidal favorites, YT liked songs)

Both `provider.like()` and `provider.unlike()` already call the provider API to add/remove from favorites. The missing piece is updating the local favorites cache so the next search-json query returns `liked: true`.

### 6. Unlike Visibility

When de-liking a track in search, the [+1] should disappear immediately. Same mechanism as liking: the unlike updates the cache, the reload refreshes the display.

### 7. Edge Cases

- Like a track from search that is already in the favorites playlist: [+1] should already be showing, unlike should remove it
- Like a track from search that is not in any playlist: [+1] appears in search only (no playlist effect until next sync)
- Rate limiting: Tidal and YT have rate limits on like/unlike. Don't allow rapid-fire toggling. The provider implementations already handle this (Tidal has rate limit retry, YT has 100ms cooldown).
- Error handling: if the like API call fails, don't update the cache or the display. Show an error notification.

---

## Dependencies

**Requires**: Phase 4 (Search Actions) -- needs the working fzf search with keybindings

**Enables**: None (final phase)

---

## Completion Criteria

- [ ] `ctrl-l` in search toggles like/unlike for the selected track
- [ ] [+1] appears immediately after liking a track
- [ ] [+1] disappears immediately after unliking a track
- [ ] Works for tracks not in any synced playlist
- [ ] Provider favorites are actually updated (check Tidal favorites after liking via search)
- [ ] Daemon favorites cache is updated in real-time
- [ ] Rapid toggling doesn't cause errors or inconsistencies
- [ ] Existing tests pass: `uv run pytest tests/ -q`
- [ ] New tests for like toggle and cache update
- [ ] `uv run mypy xmpd/` passes
- [ ] `uv run ruff check xmpd/ bin/` passes
- [ ] Manual verification:
  - Search for a track, like it via ctrl-l, see [+1] appear
  - Unlike it via ctrl-l, see [+1] disappear
  - Like a track from search, then check Tidal (or YT) favorites to confirm it was added
  - Unlike a previously liked track from search, confirm removal from favorites

---

## Testing Requirements

- Test like toggle command: given a track, verify like state changes
- Test favorites cache update: after like, verify track appears in favorites set
- Test search-json output reflects updated like state after toggle
- Test unlike: after unlike, verify track removed from favorites set
- Manual testing of the full flow is required

---

## Notes

- The like/unlike API calls are synchronous (blocking) in the providers. They run in executor threads via the daemon. Response time depends on Tidal/YT API latency (~200-500ms).
- The user's existing `C-l` in clerk likes the CURRENTLY PLAYING track. The search like (`ctrl-l`) likes the SELECTED (cursor) track. These are different code paths but should use the same underlying like/unlike logic.
- Consider adding a brief visual flash or notification when a track is liked (e.g., update fzf header briefly to show "Liked: Artist - Title"). This is optional polish.
- The favorites cache might be per-provider. Make sure both Tidal and YT favorites caches are updated correctly.
