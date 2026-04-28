# Search action bugs (play, queue, radio from xmpd-search)

These affect the post-search actions, not the search interface itself. The two-mode redesign below won't fix them because the problem is in what happens after a track is selected.

## play/queue from search adds unplayable URL to MPD

When a track is selected via enter (play) or ctrl-q (queue) in xmpd-search, the daemon's `_cmd_play`/`_cmd_queue` (daemon.py:1145,1182) build a proxy URL like `http://localhost:PORT/proxy/PROVIDER/TRACK_ID` and add it to MPD. But they never register the track in the TrackStore first. The proxy server (stream_proxy.py:562-565) looks up the track in the store, finds nothing, returns 404. MPD shows just the track ID, playback fails.

Compare with `_cmd_radio` (daemon.py:957-967) which correctly calls `self.track_store.add_track(...)` before playlist creation.

Fix: `_cmd_play` and `_cmd_queue` must call `self.track_store.add_track(provider, track_id, stream_url=None, title=..., artist=...)` before adding the proxy URL to MPD. The proxy will then resolve the stream on-demand.

Affects both YouTube and Tidal tracks from search.

## ctrl-r (radio) uses currently playing song, not fzf-highlighted song

The fzf binding passes the right args:
```
ctrl-r:execute-silent(xmpctl radio --provider {1} --track-id {2} --apply)+abort
```

xmpctl's `cmd_radio` (xmpctl:664-668) builds the daemon command as:
```python
cmd = "radio"
if provider: cmd += f" --provider {provider}"
if track_id: cmd += f" {track_id}"
```

This sends `radio --provider yt lQ-5q0t-EJg` to the daemon. The daemon parses it, gets provider and track_id, and should use the provided track. The code path looks correct for the case where track_id is not None (daemon.py:910 skips the current-track inference).

Possible causes to investigate:
- `execute-silent` swallowing an error, making it look like the wrong track plays (the radio actually fails, and whatever was playing before continues, giving the impression radio was created from the current song)
- The track_id from fzf might have trailing whitespace or ANSI escapes embedded in the field
- The `--apply` flag in xmpctl triggers mpc clear/load/play after daemon responds, but if the daemon call fails silently, the old playlist remains

Needs manual testing with debug logging to pin down.

## ctrl-q (queue) is unusable: ctrl-q is terminal "close window"

ctrl-q is universally bound to close the terminal window (or send SIGQUIT via stty). Using it as a keybind in fzf means pressing it closes the terminal/clerk window instead of queuing the track.

Rebind to a different key. Candidates: ctrl-e, ctrl-w (if not bound to word-delete), alt-q, or another free binding.

## Remove dead `xmpctl search` command

`xmpctl search` (xmpctl:331-486) is the old text-based interactive search with numbered results and `input()` prompts. It was replaced by `bin/xmpd-search` (fzf) + `xmpctl search-json` (backend). The old command should be removed along with its daemon handler `_cmd_search` (daemon.py:844-895). `search-json` stays.

## Other xmpd-search keybindings untested

ctrl-l (like-toggle), tab (multi-select), ctrl-a (queue-all), ctrl-p (play-all) have the same structural issues as play/queue (no TrackStore registration). They all go through the same `_cmd_queue` path for the multi-select actions, and `_cmd_like_toggle` for likes. The like-toggle might work since it goes to the provider API directly, but the queue-based ones will fail for the same TrackStore reason.

---

# Two-mode fzf search with debounce for xmpd-search

The interactive search (bin/xmpd-search) needs a two-mode design: Search mode and Browse mode. The current implementation has a single mode where typing fires API searches and action keybinds are always active.

Debounce: Replace the current 0.15s sleep debounce with 350ms. The user types at 70-90 WPM (130-170ms inter-key, 200-300ms word-boundary pauses). 350ms lets a full phrase like "led zeppelin" complete before the search fires.

Search mode (initial state):

- --disabled is on (no local fzf filtering)
- change:reload fires API searches via xmpctl search-json --format fzf {q} with 350ms debounce
- Only active keybind is Enter, which transitions to Browse mode
- Prompt shows Search:
- Action keybinds (play, queue, radio, like, multi-select) are unbound in this mode

Browse mode (after Enter):

- --disabled turns off, enabling fzf's native fuzzy filtering on the current result set
- No API calls happen; the user filters locally over the locked-in results
- All action keybinds become active: enter=play, ctrl-q=queue, ctrl-r=radio, ctrl-l=like, tab=select, ctrl-a=queue-all, ctrl-p=play-all
- Prompt changes to Browse:
- Esc returns to Search mode: re-enables --disabled, clears the filter input, restores the search prompt with the previous query text intact so the user can continue typing to refine the API search
- Second Esc in Search mode quits fzf entirely

Example workflow:

1. User types "led zeppelin" fast. 350ms debounce means only one API search fires for the full phrase.
2. Results appear. User presses Enter to lock in.
3. Prompt changes to Browse: . User types "in my time" to fuzzy-filter locally. Doesn't see "In My Time of Dying."
4. User presses Esc to return to Search mode. Prompt shows Search: with "led zeppelin" still in the input.
5. User appends " in my" (now "led zeppelin in my"). One API search fires after 350ms.
6. Results update. User presses Enter to lock in again.
7. User types "TD in my" to fuzzy-filter. "[TD] Led Zeppelin - In My Time of Dying" rises to the top.
8. User presses ctrl-r to start radio from the Tidal version.

fzf features needed: rebind/unbind for toggling action keybinds between modes, disable-search/enable-search for toggling local filtering, change-prompt for visual mode indicator. These are available in fzf 0.30+.

Files to modify: bin/xmpd-search (the bash fzf wrapper). No daemon or xmpctl changes needed.

# Tidal stream quality bugs

## Hardcoded "CD" quality label in search results

`_quality_for_provider` (daemon.py:1054-1059) returns a static string per provider: "CD" for Tidal, "Lo" for YouTube. It never queries per-track quality from Tidal. Every Tidal search result displays "CD" regardless of actual availability (could be HiRes, could be lower). Display-only bug; doesn't affect the actual stream.

## ffmpeg DASH stream selection always picks lowest quality

The ffmpeg command in `_stream_dash_via_ffmpeg` (stream_proxy.py:91-102) has no `-map` flag. ffmpeg defaults to Stream #0 (first audio stream in the DASH manifest). Tidal's DASH manifests order streams lowest-quality-first (standard adaptive streaming layout), so users always get the lower quality variant even when FLAC_HIRES is available.

The manifest API (tidal.py:281-282) correctly requests `["FLAC", "FLAC_HIRES"]`, so the manifest contains both variants. The problem is purely in stream selection at playback time.

Fix: add `-map 0:a:-1` (last audio stream, highest quality in Tidal's manifests) or parse the manifest to select based on a `quality_ceiling` config value.

---

# Tidal Play Reporting

Replace the current report_play implementation (which calls track.get_stream() and does nothing useful) with proper event reporting to Tidal's play-log system.

When a track crosses the 30-second play threshold, POST a playback_session event to https://tidal.com/api/event-batch. The request is application/x-www-form-urlencoded using SQS SendMessageBatchRequestEntry format, authenticated with Authorization: Bearer <access_token>.

The playback_session event (group: play_log) requires: playbackSessionId and streamingSessionId (same UUID, from the stream response), actualProductId/requestedProductId (track ID), startAssetPosition/endAssetPosition (seconds), startTimestamp/endTimestamp (unix millis), an actions array of PLAYBACK_START/PLAYBACK_STOP entries with positions and timestamps, quality metadata (actualQuality, actualAudioMode, actualAssetPresentation) from the stream info, sourceType/sourceId, productType: TRACK, and isPostPaywall: true. The client block carries platform info and the client token 49YxDN9a2aFV6RTG. The user block carries the access token, client ID, user ID, and tracking UUID.

The HistoryReporter already tracks play/pause timing. Extend the Tidal provider to capture streamingSessionId and quality info during stream resolution, accumulate play/pause actions, and build the SQS-formatted payload at report time. Only the playback_session event is required; the surrounding streaming_metrics events are optional.

# Like-Toggle Playlist Patching

After a successful like-toggle, update the [+1] indicator in two places: the on-disk playlist files and the live MPD queue.

On-disk playlists: Scan the MPD playlist directory for M3U/XSPF files containing the track's proxy URL. For a like: append [+1] to that track's EXTINF title line (M3U) or title element (XSPF). For an unlike: strip it. Leave the sync schedule untouched; the next periodic sync will reconcile naturally and the local patch is idempotent. Skip [+1] tagging entirely when the track belongs to the provider's default liked-songs playlist (yt/Liked Songs for YouTube Music, tidal/Favorites for Tidal, configurable via favorites_playlist_name_per_provider). Every track in those playlists is liked by definition, so the indicator adds no information.

Live MPD queue: For every instance of the track in the current queue, update the title in-place using MPD's cleartagid {id} Title followed by addtagid {id} Title "{new title}". This works on the currently-playing track without disrupting playback, and ncmpcpp reflects the change immediately. No delete/re-insert needed.
