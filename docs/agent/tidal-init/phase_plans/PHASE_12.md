# Phase 12: AirPlay bridge -- Tidal album art

**Feature**: tidal-init
**Estimated Context Budget**: ~40k tokens

**Difficulty**: medium

**Execution Mode**: parallel
**Batch**: 8

---

## Objective

Teach the AirPlay bridge (`extras/airplay-bridge/mpd_owntone_metadata.py`) to recognise the new
multi-source proxy URL shape (`/proxy/(yt|tidal)/<id>`) and to fetch album art for Tidal-served
tracks by reading the `art_url` column from xmpd's track-store SQLite DB. The YT path and the
existing iTunes/MusicBrainz fallback chain stay byte-for-byte unchanged.

The bridge runs as a separate user-systemd process; it never writes to the track DB.

---

## Deliverables

1. **Updated regex** in `extras/airplay-bridge/mpd_owntone_metadata.py`: `YT_PROXY_RE` becomes
   a multi-source regex that captures provider and track_id in two groups.
2. **New helper `_read_tidal_art_url(track_id) -> str | None`** in the same file: read-only
   SQLite lookup against `~/.config/xmpd/track_mapping.db`, tolerant to all error modes.
3. **Refactored `_resolve_yt_proxy`** -- renamed and rewritten to `_resolve_xmpd_proxy(song)`,
   branching on provider: YT keeps the existing YouTube-thumbnail behaviour, Tidal prefers
   `art_url` and falls through to the existing iTunes/MusicBrainz chain on miss/failure.
4. **Updated `derive_album`**: returns `"xmpd-tidal"` for `tidal` proxied tracks, keeps
   `"xmpd-yt"` for `yt`, unchanged behaviour for non-proxy URLs.
5. **New unit-test file** `tests/test_airplay_bridge_track_store_reader.py` with four tests
   covering the SQLite reader. Tests load the bridge module via `importlib.util` (the file's
   parent directory is hyphenated and the file is not part of the `xmpd` package).
6. **No User-Agent change required** -- it is already `xmpd-airplay-bridge/1.0` (verified during
   plan write); confirm during implementation by re-grepping.

---

## Detailed Requirements

### File under edit

The phase modifies exactly one source file plus adds one test file:

- Edit: `extras/airplay-bridge/mpd_owntone_metadata.py`
- Add: `tests/test_airplay_bridge_track_store_reader.py`

No other files in the repo are touched. Do NOT add `extras/airplay-bridge/__init__.py` or
`extras/__init__.py` -- the bridge is an installable script, not a Python package, and
`pyproject.toml` explicitly limits `packages.find` to `xmpd*`. Tests must use `importlib.util`
to load the module by path (see Test 0 below).

### 1. Imports

Add `import sqlite3` at the top of `mpd_owntone_metadata.py` near the other stdlib imports
(alphabetically between `socket` and `sys`). Pathlib's `Path` is already imported.

### 2. Regex update

Replace the existing constant definition

```python
YT_PROXY_RE = re.compile(r"/proxy/([A-Za-z0-9_-]{11})(?:[?#/]|$)")
```

with two compiled regexes:

```python
# xmpd serves tracks via http://localhost:<port>/proxy/<provider>/<track_id>.
# Group 1: provider canonical name ("yt" or "tidal"). Group 2: provider-native track id.
# YT ids are 11 chars; Tidal ids are decimal strings; we accept anything until ?, #, /, or whitespace.
XMPD_PROXY_RE = re.compile(r"/proxy/(yt|tidal)/([^/?\s#]+)")

# Backwards-compat alias retained for any internal callers that still reference the old name;
# remove once all call sites are migrated. Currently used by derive_album and _resolve_xmpd_proxy.
YT_PROXY_RE = XMPD_PROXY_RE  # noqa: N816 -- legacy name kept temporarily
```

NOTE: After the rest of this phase's edits, all in-file callers reference `XMPD_PROXY_RE`
explicitly; the alias is therefore redundant. Drop the alias before committing -- the only
reason to keep it would be external scripts importing the symbol, and there are none.

### 3. Update `derive_album`

Current:

```python
def derive_album(song: dict) -> str:
    file_uri = song.get("file", "")
    if YT_PROXY_RE.search(file_uri):
        return "xmpd-yt"
    if "://" in file_uri:
        return song.get("Album") or "stream"
    return song.get("Album") or "local"
```

Target:

```python
def derive_album(song: dict) -> str:
    """Tag album by source.

    "xmpd-yt"     -- xmpd proxy URL with provider=yt
    "xmpd-tidal"  -- xmpd proxy URL with provider=tidal
    <song Album>  -- local file (falling back to "local")
    "stream"      -- other HTTP source without an Album tag
    """
    file_uri = song.get("file", "")
    m = XMPD_PROXY_RE.search(file_uri)
    if m:
        provider = m.group(1)
        return f"xmpd-{provider}"
    if "://" in file_uri:
        return song.get("Album") or "stream"
    return song.get("Album") or "local"
```

Also update the module docstring at the top of the file (lines 6-10) to mention `xmpd-tidal`
alongside `xmpd-yt`. Keep the rest of the docstring intact.

### 4. New helper `_read_tidal_art_url`

Insert immediately above `_resolve_yt_proxy` (around the existing line 384):

```python
TRACK_STORE_DB_PATH = Path("~/.config/xmpd/track_mapping.db").expanduser()


def _read_tidal_art_url(track_id: str) -> str | None:
    """Look up a Tidal track's art_url in xmpd's track_store. Read-only.

    Returns the URL string if a row exists for (provider='tidal', track_id=<track_id>) and
    that row has a non-NULL art_url. Returns None on every other path -- DB missing, row
    missing, art_url NULL, sqlite3.Error (locked DB, schema mismatch, corruption). Never
    raises.
    """
    if not TRACK_STORE_DB_PATH.is_file():
        return None
    try:
        conn = sqlite3.connect(
            f"file:{TRACK_STORE_DB_PATH}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        try:
            cur = conn.execute(
                "SELECT art_url FROM tracks WHERE provider = ? AND track_id = ?",
                ("tidal", track_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return row[0]  # may be None when art_url is NULL
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.debug("track_store art_url lookup failed for %s: %s", track_id, e)
        return None
```

Notes:

- `mode=ro` is the explicit URI flag; combined with `uri=True` it gives a read-only handle that
  refuses any write. Do NOT use `:memory:`, `mode=rw`, or omit the flag.
- `timeout=1.0` keeps the bridge responsive when the daemon holds a write lock during a sync
  cycle. On lock contention SQLite raises `sqlite3.OperationalError` (subclass of
  `sqlite3.Error`); we swallow it and return None so the caller falls through to iTunes.
- `TRACK_STORE_DB_PATH` is a module-level constant for cleanliness and so the test can
  monkey-patch it.

### 5. Refactor `_resolve_yt_proxy` -> `_resolve_xmpd_proxy`

Current behaviour (lines 384-411): if the song's MPD file URI matches `YT_PROXY_RE`, fetch
`https://img.youtube.com/vi/<video_id>/hqdefault.jpg` and cache to disk.

New behaviour:

```python
def _resolve_xmpd_proxy(song: dict) -> bytes | None:
    """Fetch album art for an xmpd-proxied track.

    YT path: download YouTube thumbnail, cache on disk by video_id (existing behaviour).
    Tidal path: read art_url from xmpd's track_store, download it, cache on disk by track_id.
        On track_store miss / download failure, return None so the chain falls through to the
        existing iTunes/MusicBrainz/CAA resolver.
    """
    file_uri = song.get("file", "")
    m = XMPD_PROXY_RE.search(file_uri)
    if not m:
        return None
    provider, track_id = m.group(1), m.group(2)

    cache_path = ART_CACHE_DIR / f"{provider}-{track_id}.jpg"
    if cache_path.exists():
        try:
            return cache_path.read_bytes()
        except OSError as e:
            log.warning("Could not read cached art %s: %s", cache_path, e)

    if provider == "yt":
        url = f"https://img.youtube.com/vi/{track_id}/hqdefault.jpg"
    elif provider == "tidal":
        url = _read_tidal_art_url(track_id)
        if not url:
            log.debug("No art_url in track_store for tidal/%s", track_id)
            return None
    else:
        # Defensive: regex restricts provider to yt|tidal, but stay robust.
        return None

    try:
        ART_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": ART_HTTP_USER_AGENT})
        with urllib.request.urlopen(req, timeout=ART_FETCH_TIMEOUT_SEC) as resp:
            data = resp.read()
        cache_path.write_bytes(data)
        log.info("Fetched %s art for %s (%d bytes)", provider, track_id, len(data))
        return data
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to fetch %s art for %s: %s", provider, track_id, e)
        return None
```

Key differences from the original:

- Cache filename is `<provider>-<track_id>.jpg`, NOT `<track_id>.jpg`. This avoids collisions
  if a yt and a tidal track ever share the same numeric/alphanumeric id by coincidence, and it
  makes the cache directory self-describing.
  - **Compatibility**: pre-existing entries `<11char>.jpg` (YT-only) become orphaned. They're
    harmless -- the next play of the YT track misses the new key, downloads once, and writes
    the new keyed file. A one-line note goes in the phase summary: "Existing YT thumbnail
    cache entries become orphan files; safe to delete `~/.cache/mpd-owntone-metadata/*.jpg`
    for any 11-char filenames not prefixed with `yt-` or `tidal-`."
- For Tidal, the URL comes from `_read_tidal_art_url(track_id)`. On store-miss, return None
  WITHOUT writing a cache miss marker -- the next play after `xmpctl sync` populates the row
  should succeed. (The iTunes/MusicBrainz `_resolve_online` path has its own miss cache; that
  is a separate concern.)
- Tidal URLs are general HTTPS and require the `User-Agent` header for any politeness; the
  existing `urllib.request.Request(...)` upgrade applies the constant we already use.

Then update `_RESOLVERS` (around line 414):

```python
_RESOLVERS = (
    _resolve_xmpd_proxy,   # was _resolve_yt_proxy
    _resolve_mpd_readpicture,
    _resolve_mpd_albumart,
    _resolve_online,
)
```

Delete the now-unused `_resolve_yt_proxy` symbol entirely.

### 6. Sanity grep after editing

Before declaring done, run:

```bash
grep -nE 'ytmpd|YT_PROXY_RE|_resolve_yt_proxy' extras/airplay-bridge/mpd_owntone_metadata.py
```

Expected output: zero matches. If any match remains, finish the rename or remove the alias.

Also verify:

```bash
grep -nE 'User-Agent|ART_HTTP_USER_AGENT' extras/airplay-bridge/mpd_owntone_metadata.py
```

Expected: the constant string is `"xmpd-airplay-bridge/1.0 (+https://github.com/tuncenator/xmpd)"`
and is referenced in 4 `urllib.request.Request` call sites + 1 declaration. No `ytmpd-airplay`
or other legacy variant.

### 7. Tests

Add `tests/test_airplay_bridge_track_store_reader.py`. The bridge module's parent directory has
a hyphen, so a normal `import` does not work. Use `importlib.util.spec_from_file_location` to
load it once per test session and expose `_read_tidal_art_url` and the
`TRACK_STORE_DB_PATH` constant.

Skeleton:

```python
"""Tests for the AirPlay bridge's track-store reader.

The bridge module lives at extras/airplay-bridge/mpd_owntone_metadata.py. Its parent dir
contains a hyphen, so it is not importable through the package system. We load it via
importlib.util once per test session and monkey-patch the DB path constant per test.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_PATH = REPO_ROOT / "extras" / "airplay-bridge" / "mpd_owntone_metadata.py"


@pytest.fixture(scope="module")
def bridge():
    spec = importlib.util.spec_from_file_location("airplay_bridge_under_test", BRIDGE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["airplay_bridge_under_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def track_db(tmp_path: Path) -> Path:
    """Create a fresh post-Phase-5-shape tracks DB and return its path."""
    db = tmp_path / "track_mapping.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE tracks (
            track_id         TEXT NOT NULL,
            provider         TEXT NOT NULL DEFAULT 'yt',
            stream_url       TEXT,
            artist           TEXT,
            title            TEXT NOT NULL,
            album            TEXT,
            duration_seconds INTEGER,
            art_url          TEXT,
            updated_at       REAL NOT NULL
        );
        CREATE UNIQUE INDEX tracks_pk_idx ON tracks(provider, track_id);
        """
    )
    conn.commit()
    conn.close()
    return db


def test_read_tidal_art_url_returns_value(bridge, track_db, monkeypatch):
    """Seeded tidal row with art_url returns the URL."""
    conn = sqlite3.connect(track_db)
    conn.execute(
        "INSERT INTO tracks (track_id, provider, title, art_url, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("12345678", "tidal", "Hello", "https://resources.tidal.com/images/abc.jpg", 1.0),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(bridge, "TRACK_STORE_DB_PATH", track_db)
    assert bridge._read_tidal_art_url("12345678") == "https://resources.tidal.com/images/abc.jpg"


def test_read_tidal_art_url_returns_none_for_missing_row(bridge, track_db, monkeypatch):
    """Empty tracks table -> None, not an exception."""
    monkeypatch.setattr(bridge, "TRACK_STORE_DB_PATH", track_db)
    assert bridge._read_tidal_art_url("nonexistent-id") is None


def test_read_tidal_art_url_returns_none_for_yt_provider(bridge, track_db, monkeypatch):
    """Row with provider=yt is ignored even if track_id matches."""
    conn = sqlite3.connect(track_db)
    conn.execute(
        "INSERT INTO tracks (track_id, provider, title, art_url, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("dQw4w9WgXcQ", "yt", "YT track", "https://example.com/yt-art.jpg", 1.0),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(bridge, "TRACK_STORE_DB_PATH", track_db)
    assert bridge._read_tidal_art_url("dQw4w9WgXcQ") is None


def test_read_tidal_art_url_returns_none_for_missing_db(bridge, tmp_path, monkeypatch):
    """Pointing at a nonexistent DB path returns None without raising."""
    monkeypatch.setattr(bridge, "TRACK_STORE_DB_PATH", tmp_path / "nope.db")
    assert bridge._read_tidal_art_url("anything") is None
```

Edge-case fidelity:

- The DB schema in the fixture intentionally mirrors the **target** post-Phase-5 shape (compound
  unique index on `(provider, track_id)` plus the new nullable columns). Phase 12 is
  conductor-batch-8, after Phase 5 has landed; the real DB has this shape. If Phase 5 chose a
  slightly different column order, the fixture is order-tolerant because the queries name
  columns explicitly. Coder must read `xmpd/track_store.py` post-Phase-5 to confirm column
  names match (`provider`, `track_id`, `art_url`).
- Don't spawn a real `XMPDaemon` from these tests -- they import only the bridge module.
- The fixture seeds via a writable connection; the bridge opens read-only via URI -- both
  must coexist on the same SQLite file (they do; SQLite supports concurrent ro+rw).

### 8. Manual verification (after restart)

Restart the bridge so it picks up the new code:

```bash
systemctl --user restart mpd-owntone-metadata
journalctl --user -u mpd-owntone-metadata -f
```

(If the unit name differs, find it with `systemctl --user list-unit-files | grep -i metadata`.)

Then play three representative tracks while watching the journal:

1. **YT track over AirPlay**: load any `YT: ...` playlist into MPD; play; observe on the
   AirPlay receiver (e.g. Denon Kitchen) that the album art appears -- this exercises the
   YT branch of `_resolve_xmpd_proxy`. Journal should show
   `Fetched yt art for <video_id> (... bytes)`.
2. **Tidal track over AirPlay**: load any `TD: ...` playlist; play. Art should appear, sourced
   from `track_store.art_url`. Journal should show `Fetched tidal art for <track_id> (... bytes)`.
3. **Local-music track**: queue a non-proxied file with embedded or folder art. Bridge falls
   through `_resolve_xmpd_proxy` (no match), hits `_resolve_mpd_readpicture` /
   `_resolve_mpd_albumart`, art appears. No regression.

Negative spot check:

4. **Tidal track whose row has NULL `art_url`** (rare; happens if Phase 10's metadata extractor
   didn't capture an art URL for this track): bridge logs
   `No art_url in track_store for tidal/<track_id>`, falls through to iTunes/MusicBrainz, art
   still appears (or a cached-miss marker is written). No exception in the journal.

### Edge cases to handle explicitly

- **DB locked** (SQLite `OperationalError: database is locked`): caught by the
  `sqlite3.Error` clause; bridge returns None and logs at DEBUG. No retry loop -- a single
  upcoming track-change emission is allowed to fall through to the iTunes path.
- **Schema mismatch** (Phase 5 not yet run, or older bridge installed against newer DB):
  `sqlite3.OperationalError: no such column: art_url` -- caught the same way, returns None.
- **`art_url` is NULL in the row**: `row[0]` is None; we return None and the caller treats it
  as a miss and falls through.
- **Empty `track_id`** in the URL (degenerate): the regex `[^/?\s#]+` requires at least one
  character, so `_resolve_xmpd_proxy` never sees an empty `track_id`; this edge cannot occur
  via the regex match path.
- **Provider value not in {yt, tidal}**: regex restricts to `(yt|tidal)`; the explicit
  `else` branch in `_resolve_xmpd_proxy` is defensive only.
- **Cache-write failure** after a successful network fetch: existing code already swallows
  `OSError` from `cache_path.write_bytes`, just logs WARNING; preserve that behaviour. (The
  outer `except Exception` covers this; the function still returns the bytes it has, so the
  art is still emitted to OwnTone.)

  Wait -- re-read: in the existing code, if `cache_path.write_bytes(data)` raises after the
  download succeeds, the `except Exception` catches it and returns None, throwing away the
  art. That is the current behaviour and we must preserve it (not change semantics in this
  phase). Coder, do not "improve" this.

---

## Dependencies

**Requires**:

- Phase 5 (track_store schema migration): `tracks` table must have `provider` and `art_url`
  columns. Compound `(provider, track_id)` index must exist. The `_read_tidal_art_url` query
  fails closed if Phase 5 hasn't run -- but for live verification, both Phase 5 and Phase 10
  (which populates Tidal `art_url` values) must have run first.

**Enables**:

- Phase 13 (install / docs): Phase 13 references Phase 12 in docs/MIGRATION.md but does not
  block on it for code -- the integration is loose.

---

## Completion Criteria

- [ ] `extras/airplay-bridge/mpd_owntone_metadata.py` updated:
    - [ ] `XMPD_PROXY_RE` defined with two capture groups; `YT_PROXY_RE` alias removed.
    - [ ] `_read_tidal_art_url(track_id) -> str | None` implemented and read-only-URI based.
    - [ ] `_resolve_xmpd_proxy(song)` replaces `_resolve_yt_proxy`, branches on provider,
          falls through to None on Tidal track-store miss.
    - [ ] `_RESOLVERS` tuple references `_resolve_xmpd_proxy`.
    - [ ] `derive_album` returns `xmpd-tidal` for tidal proxy URLs.
    - [ ] `import sqlite3` added.
    - [ ] Module docstring mentions `xmpd-tidal`.
- [ ] `tests/test_airplay_bridge_track_store_reader.py` created with 4 tests, all passing.
- [ ] `pytest -q tests/test_airplay_bridge_track_store_reader.py` passes locally.
- [ ] `pytest -q` (full suite) passes -- no regression in other test files.
- [ ] `ruff check extras/airplay-bridge/mpd_owntone_metadata.py tests/test_airplay_bridge_track_store_reader.py` clean.
- [ ] `mypy xmpd/` still passes (the bridge module is outside the `xmpd/` package; not in the
      mypy scope -- confirm `mypy.ini` / `pyproject.toml` `[tool.mypy]` does not include
      `extras/`).
- [ ] Sanity greps return zero matches:
    - [ ] `grep -nE 'ytmpd|_resolve_yt_proxy|YT_PROXY_RE' extras/airplay-bridge/mpd_owntone_metadata.py`
- [ ] Manual: bridge restarted; YT track plays with art on the AVR.
- [ ] Manual: Tidal track plays with art on the AVR (sourced from `art_url`); journal shows
      `Fetched tidal art for ...`.
- [ ] Manual: local file plays with art (existing chain unchanged).
- [ ] Phase summary file `docs/agent/tidal-init/summaries/PHASE_12_SUMMARY.md` written; STATUS.md updated.

---

## Testing Requirements

**Automated (4 unit tests in `tests/test_airplay_bridge_track_store_reader.py`)**:

1. `test_read_tidal_art_url_returns_value`: seeded row -> URL returned.
2. `test_read_tidal_art_url_returns_none_for_missing_row`: no row -> None.
3. `test_read_tidal_art_url_returns_none_for_yt_provider`: only the `tidal` provider matches.
4. `test_read_tidal_art_url_returns_none_for_missing_db`: nonexistent DB path -> None.

Run command: `pytest -q tests/test_airplay_bridge_track_store_reader.py`
Expected: `4 passed in <1s`

Full-suite run: `pytest -q`
Expected: all existing tests still pass; total count is up by 4.

**Manual (per existing bridge convention)**:

The bridge's existing test convention is no automated tests for the resolver chain or the
metadata-loop -- these involve a live MPD socket, OwnTone pipe, and AirPlay receiver. The 4
new unit tests cover the only newly-introduced piece of pure logic (the SQLite reader).
Everything else verifies manually per the four-track checklist in section 8 above.

**Special check**: after restarting the bridge service and playing a Tidal track, run
`ls -la ~/.cache/mpd-owntone-metadata/ | grep tidal-` to confirm a `tidal-<track_id>.jpg`
cache file was written.

---

## External Interfaces Consumed

- **xmpd track_store row shape (post-Phase-5)** -- the `tracks` SQLite table that Phase 5
  defines. Phase 12 reads `(provider, track_id, art_url)` only.
  - **Consumed by**: `_read_tidal_art_url` in `extras/airplay-bridge/mpd_owntone_metadata.py`
    and the seed fixture in `tests/test_airplay_bridge_track_store_reader.py`.
  - **How to capture**: with the daemon already syncing in this branch (Phase 11 enables
    Tidal end-to-end), run:
    ```bash
    sqlite3 -readonly ~/.config/xmpd/track_mapping.db ".schema tracks"
    sqlite3 -readonly ~/.config/xmpd/track_mapping.db \
      "SELECT provider, track_id, art_url FROM tracks WHERE provider='tidal' LIMIT 5"
    sqlite3 -readonly ~/.config/xmpd/track_mapping.db "PRAGMA user_version"
    ```
    Paste the schema, the 5 sample rows (or note "no tidal rows yet" if Phase 11 hasn't run
    a sync since landing), and the user_version into the phase summary's "Evidence Captured"
    section. Confirm column names match what `_read_tidal_art_url` queries (`provider`,
    `track_id`, `art_url`).
  - **If not observable**: if no Tidal rows exist yet (e.g. Phase 11's sync hasn't been
    triggered in the test environment), capture the schema only and leave a note in the
    summary. The unit tests do NOT depend on real Tidal rows -- they synthesise a fixture
    DB with the same shape. The dependency on real rows is only for the manual AVR
    verification.

---

## Notes

- **Read-only mandate**: Never open the DB with `mode=rw` or default mode from this process.
  The xmpd daemon is the sole writer to `track_mapping.db`. SQLite's URI-form `mode=ro`
  refuses any DML/DDL even if the code accidentally tries one.
- **Don't restart at the wrong time**: `systemctl --user restart mpd-owntone-metadata`
  briefly drops the metadata pipe; OwnTone reattaches automatically. If a track is mid-play
  the listener may see an empty-meta blip; warn the user to pause before restarting if they
  care. Not a hard failure mode.
- **Cache-key change is one-way**: switching from `<id>.jpg` to `<provider>-<id>.jpg`
  abandons the existing YT cache files. They are not deleted by this phase. Mention this in
  the phase summary; cleanup is opportunistic ad-hoc work the user can do later.
- **Why no helper for the SQLite read**: the read is one function, used in one place, with
  no recurring pattern across phases. Per planner heuristics this is one-shot mechanics --
  no helper script proposed.
- **Why we don't need to touch the iTunes/MusicBrainz chain**: the brief is explicit -- only
  the xmpd-served branch changes. `_resolve_online`, `_album_cache_*`, `_fetch_itunes`,
  `_fetch_musicbrainz` remain identical. They sit later in `_RESOLVERS` and are reached
  whenever `_resolve_xmpd_proxy` returns None on a non-proxy or Tidal-store-miss URL.
- **Watch for `xmpd-tidal` typos**: hyphen, lowercase, no spaces. The classifier value is
  user-visible (it shows up as the "Album" field on AirPlay receivers when the song has no
  album tag), so consistency with `xmpd-yt` matters.
- **About the brief's `_classify_album` reference**: the function in the source is actually
  named `derive_album`. The brief used a different name; treat the requirement as binding to
  the real function, not the brief-named one.
- **About the brief's "currently returns ytmpd" claim**: the source already returns `xmpd-yt`
  (legacy from a prior cleanup, evidence: `CHANGELOG.md` line 13). Phase 12 only adds the
  Tidal branch; the YT branch and label are unchanged. There is no `"ytmpd"` literal in the
  bridge source -- the post-edit grep should already show zero matches.

---
