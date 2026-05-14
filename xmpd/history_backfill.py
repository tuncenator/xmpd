"""One-shot MPD log backfill for xmpd local history store.

Reads the MPD log file, extracts every ``player: played`` line, and imports
the corresponding play events into the local HistoryStore.  Already-present
rows (from previous backfill or live play reporting) are silently skipped so
the operation is idempotent.

Plays that immediately follow a ``Failed to decode`` exception for the same
URL (within a 2-second grace window) are treated as phantom plays and
excluded from import.

Public API::

    result = run_backfill(history_store, track_store, log_path, dry_run=False)
    # result == {"inserted": N, "skipped": M, "orphans": K, "skipped_failed_decode": F}
"""

from __future__ import annotations

import logging
import re
import socket
from datetime import datetime

from xmpd.exceptions import XMPDError
from xmpd.history_store import HistoryStore
from xmpd.track_store import TrackStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level regex constants
# ---------------------------------------------------------------------------

# Matches a log line emitted by MPD when a proxy stream is played.
# Named groups: ts, provider, track_id.
# Handles both ISO 8601 (single token like 2026-05-07T17:51:23) and legacy
# MMM DD HH:MM:SS (three tokens, e.g. "May  8 09:12:33") formats.
# The ts group greedily captures up to three whitespace-separated tokens
# before the " player:" keyword.
LOG_LINE_RE = re.compile(
    r'^(?P<ts>\S+(?:\s+\S+(?:\s+\S+)?)?)\s+player:\s+played\s+"http://[^/]+/proxy/(?P<provider>\w+)/(?P<track_id>[^"]+)"\s*$'
)

# Matches any player:played line; the URL group is later classified as
# proxy (already captured above), local file (no scheme), or other stream
# (has "://" but not proxy -- silently ignored).
PLAYED_LINE_RE = re.compile(
    r'^(?P<ts>\S+(?:\s+\S+(?:\s+\S+)?)?)\s+player:\s+played\s+"(?P<url>[^"]+)"\s*$'
)

# Matches an MPD "Failed to decode" exception line for a proxy URL.
# Extracts timestamp, provider, and track_id so we can correlate with
# subsequent ``player: played`` lines for the same track.
FAILED_DECODE_RE = re.compile(
    r'^(?P<ts>\S+(?:\s+\S+(?:\s+\S+)?)?)\s+exception:\s+Failed to decode\s+"http://[^/]+/proxy/(?P<provider>\w+)/(?P<track_id>[^"]+)"'
)

# Maximum seconds between a failed-decode exception and a subsequent
# ``player: played`` event for the same URL to still be considered a
# phantom play (and thus skipped).
FAILED_DECODE_GRACE_SECONDS = 2

# Title/artist values that mark a TrackStore row as an unresolved stub. These
# are the strings the resolver writes when it can't fetch real metadata
# (e.g. the historical ``testvideoid`` / tidal ``99999999`` placeholders left
# in track_mapping.db from earlier dev runs). A play whose TrackStore record
# carries these placeholders never represents real listening data and is
# excluded from history backfill.
_PLACEHOLDER_TITLES = frozenset({"Unknown", "Unknown Title"})
_PLACEHOLDER_ARTISTS = frozenset({"Unknown", "Unknown Artist"})

# Hard-coded (provider, track_id) pairs that must never enter history. These
# are well-known dev-time placeholders that linger in old MPD logs and would
# otherwise be re-imported every time the user runs ``history-backfill``.
# Adding to this set is intentionally a code change so the decision is
# explicit and reviewable.
_BLOCKLISTED_TRACKS: frozenset[tuple[str, str]] = frozenset(
    {
        ("yt", "testvideoid"),
        ("tidal", "99999999"),
    }
)


def _is_placeholder_stub(track: dict | None) -> bool:
    """Return True when *track* is the explicit unresolved-track stub."""
    if not track:
        return False
    title = track.get("title")
    artist = track.get("artist")
    return title in _PLACEHOLDER_TITLES and artist in _PLACEHOLDER_ARTISTS


def _is_blocklisted_track(provider: str, track_id: str) -> bool:
    """Return True for (provider, track_id) pairs banned from history."""
    return (provider, track_id) in _BLOCKLISTED_TRACKS

# Detects ISO 8601 timestamps like 2026-05-07T17:51:23
ISO_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")

# Detects legacy MPD timestamps like "May  8 09:12:33"
LEGACY_TIMESTAMP_RE = re.compile(r"^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}$")

# Matches log_file directive in mpd.conf: log_file "/path/to/file"
MPDCONF_LOG_FILE_RE = re.compile(r'^\s*log_file\s+"([^"]+)"', re.MULTILINE)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_played_at(timestamp_str: str, log_mtime: float) -> str:
    """Convert an MPD log timestamp string to ISO 8601 with local tz offset.

    Args:
        timestamp_str: The ``ts`` capture group from LOG_LINE_RE.
        log_mtime: Log file mtime as a POSIX float (from os.path.getmtime).

    Returns:
        ISO 8601 string with local UTC offset, e.g. ``2026-05-07T17:51:23+03:00``.

    Raises:
        ValueError: If the timestamp does not match either known format.
    """
    local_tzinfo = datetime.now().astimezone().tzinfo

    if ISO_TIMESTAMP_RE.match(timestamp_str):
        naive = datetime.fromisoformat(timestamp_str)
        local_dt = naive.replace(tzinfo=local_tzinfo)
        return local_dt.isoformat()

    if LEGACY_TIMESTAMP_RE.match(timestamp_str):
        mtime_year = datetime.fromtimestamp(log_mtime).year
        candidate = datetime.strptime(f"{mtime_year} {timestamp_str}", "%Y %b %d %H:%M:%S")
        candidate_local = candidate.replace(tzinfo=local_tzinfo)
        # If the candidate is more than 30 days after log mtime, the line was
        # written in the previous calendar year (e.g. Dec in a Jan-mtime log).
        mtime_dt = datetime.fromtimestamp(log_mtime, tz=local_tzinfo)
        delta_seconds = (candidate_local - mtime_dt).total_seconds()
        if delta_seconds > 30 * 24 * 3600:
            candidate = datetime.strptime(f"{mtime_year - 1} {timestamp_str}", "%Y %b %d %H:%M:%S")
            candidate_local = candidate.replace(tzinfo=local_tzinfo)
        return candidate_local.isoformat()

    raise ValueError(f"unrecognized timestamp: {timestamp_str!r}")


def _enrich_local_tracks(
    paths: set[str], mpd_socket_path: str | None
) -> dict[str, dict[str, object]]:
    """Look up tags for a set of MPD-relative paths via a transient connection.

    Returns ``{path: {"title", "artist", "album", "duration_seconds"}}``.
    Paths that MPD doesn't know are absent from the mapping; the caller
    falls back to NULL metadata for those rows.

    Args:
        paths: Set of MPD-relative file paths.
        mpd_socket_path: Unix socket path or ``host:port`` for MPD. If None
            or empty, the lookup is skipped and an empty dict is returned.
    """
    if not paths or not mpd_socket_path:
        return {}

    from mpd import MPDClient as MPDClientBase

    client = MPDClientBase()
    client.timeout = 30
    try:
        if ":" in mpd_socket_path:
            host, port_str = mpd_socket_path.split(":", 1)
            client.connect(host, int(port_str))
        else:
            client.connect(mpd_socket_path)
    except Exception as exc:
        logger.warning(
            "backfill: MPD connect failed, skipping local enrichment: %s", exc
        )
        return {}

    out: dict[str, dict[str, object]] = {}
    try:
        for path in paths:
            try:
                results = client.find("file", path)
            except Exception as exc:
                logger.debug("backfill: MPD find failed for %r: %s", path, exc)
                continue
            if not results:
                continue
            r = results[0]
            time_raw = r.get("time")
            try:
                duration: int | None = int(time_raw) if time_raw else None
            except (TypeError, ValueError):
                duration = None
            out[path] = {
                "title": r.get("title") or None,
                "artist": r.get("artist") or None,
                "album": r.get("album") or None,
                "duration_seconds": duration,
                "art_url": None,
                "quality": None,
            }
    finally:
        try:
            client.close()
            client.disconnect()
        except Exception:
            pass

    return out


def _resolve_log_path(explicit: str | None, configured: str | None) -> str:
    """Resolve the MPD log path from explicit arg, config, or raise.

    Args:
        explicit: Path passed via ``--log`` flag (may be None or empty string).
        configured: Path from ``history.mpd_log_path`` config key (may be None).

    Returns:
        Non-empty path string (not expanded; caller must expanduser).

    Raises:
        XMPDError: If neither source provides a non-empty path.
    """
    if explicit:
        return explicit
    if configured:
        return configured
    raise XMPDError("could not locate MPD log file")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_backfill(
    history_store: HistoryStore,
    track_store: TrackStore | None,
    log_path: str,
    *,
    dry_run: bool,
    mpd_socket_path: str | None = None,
) -> dict[str, int]:
    """Import all ``player: played`` events from an MPD log into the history store.

    Idempotent: rows already present in the DB (by played_at + provider +
    track_id key) are counted as ``skipped``, not re-inserted.

    Within-log duplicates (same second, same track) collapse to one row.
    Malformed lines are debug-logged and excluded from all counters.
    Orphan rows (no track_store match) are inserted with NULL metadata fields
    and counted under ``orphans``; they are still included in ``inserted``.

    Local file plays (``player: played "<bare path>"``) are imported with
    ``provider="local"`` and ``track_id`` set to the MPD-relative path.
    When ``mpd_socket_path`` is provided, each unique local path is enriched
    via ``find file <path>`` so the row carries title/artist/album/duration.

    Args:
        history_store: Open HistoryStore for this host.
        track_store: TrackStore for metadata enrichment, or None.
        log_path: Absolute path to the MPD log file.
        dry_run: If True, parse and count but do not write to the DB.
        mpd_socket_path: Optional MPD socket (or host:port) used to look up
            tags for local file plays. When None, local rows are inserted
            with NULL metadata.

    Returns:
        Dict with keys ``inserted``, ``skipped``, ``orphans``,
        ``skipped_failed_decode``.
    """
    import os

    # Build dedup set from existing rows for this host.
    self_host = socket.gethostname().upper()
    existing = history_store.get_plays(mode="time", since=None, limit=10_000_000)
    seen: set[tuple[str, str, str]] = {
        (r["played_at"], r["provider"], r["track_id"])
        for r in existing
        if r.get("host") == self_host
    }

    log_mtime = os.path.getmtime(log_path)

    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        raise  # let daemon layer catch and format the error

    # --- First pass: collect failed-decode timestamps per (provider, track_id) ---
    failed_decode_times: dict[tuple[str, str], list[datetime]] = {}
    for line in lines:
        line_stripped = line.rstrip("\n")
        fd = FAILED_DECODE_RE.match(line_stripped)
        if not fd:
            continue
        ts_str = fd.group("ts")
        fd_provider = fd.group("provider")
        fd_track_id = fd.group("track_id")
        try:
            iso = _parse_played_at(ts_str, log_mtime)
            dt = datetime.fromisoformat(iso)
        except ValueError:
            continue
        failed_decode_times.setdefault((fd_provider, fd_track_id), []).append(dt)

    # Parse matching played lines into (played_at, provider, track_id) tuples.
    matches: list[tuple[str, str, str]] = []
    for line in lines:
        line = line.rstrip("\n")
        proxy_m = LOG_LINE_RE.match(line)
        if proxy_m is not None:
            ts_str = proxy_m.group("ts")
            provider = proxy_m.group("provider")
            track_id = proxy_m.group("track_id")
        else:
            generic_m = PLAYED_LINE_RE.match(line)
            if generic_m is None:
                logger.debug("backfill: skipping non-match line: %r", line)
                continue
            url = generic_m.group("url")
            if "://" in url:
                # http://... that didn't hit the proxy regex => non-xmpd stream
                logger.debug("backfill: skipping non-proxy stream line: %r", line)
                continue
            ts_str = generic_m.group("ts")
            provider = "local"
            track_id = url
        try:
            played_at = _parse_played_at(ts_str, log_mtime)
        except ValueError:
            logger.warning("backfill: unrecognized timestamp %r -- skipping line", ts_str)
            continue
        matches.append((played_at, provider, track_id))

    # Enrich local file paths via MPD before the insert loop.
    local_paths = {tid for _, prov, tid in matches if prov == "local"}
    local_meta = _enrich_local_tracks(local_paths, mpd_socket_path)

    inserted = 0
    skipped = 0
    orphans = 0
    skipped_failed_decode = 0
    skipped_placeholder = 0

    for played_at, provider, track_id in matches:
        # Check if this play is a phantom from a failed decode.
        fd_times = failed_decode_times.get((provider, track_id))
        if fd_times:
            play_dt = datetime.fromisoformat(played_at)
            is_phantom = any(
                0 <= (play_dt - fd_dt).total_seconds() <= FAILED_DECODE_GRACE_SECONDS
                for fd_dt in fd_times
            )
            if is_phantom:
                skipped_failed_decode += 1
                logger.debug(
                    "backfill: skipping failed-decode phantom %s/%s at %s",
                    provider,
                    track_id,
                    played_at,
                )
                continue

        # Hard-coded blocklist (e.g., legacy testvideoid / tidal 99999999
        # that escaped the failed-decode window). Counted under
        # ``skipped_placeholder`` alongside metadata-stub skips.
        if _is_blocklisted_track(provider, track_id):
            skipped_placeholder += 1
            logger.debug(
                "backfill: skipping blocklisted %s/%s at %s",
                provider,
                track_id,
                played_at,
            )
            continue

        # Metadata lookup (done for all rows to compute orphan count accurately).
        # Local plays bypass track_store and read tags from the MPD enrichment
        # map; they never count as orphans because the live MPD library is the
        # source of truth for local files.
        if provider == "local":
            track = local_meta.get(track_id)
            is_orphan = False
        else:
            track = track_store.get_track(provider, track_id) if track_store is not None else None
            is_orphan = track is None
            if is_orphan:
                orphans += 1

        # Skip placeholder-stub rows (e.g., legacy testvideoid / tidal 99999999
        # that were never resolved to real tracks).
        if _is_placeholder_stub(track):
            skipped_placeholder += 1
            logger.debug(
                "backfill: skipping placeholder-stub %s/%s at %s",
                provider,
                track_id,
                played_at,
            )
            continue

        key = (played_at, provider, track_id)
        if key in seen:
            skipped += 1
            continue

        if track is None:
            title: str | None = None
            artist: str | None = None
            album: str | None = None
            duration_seconds: int | None = None
            art_url: str | None = None
            quality: str | None = None
        else:
            title = track.get("title")
            artist = track.get("artist")
            album = track.get("album")
            duration_seconds = track.get("duration_seconds")
            art_url = track.get("art_url")
            quality = track.get("quality")

        if not dry_run:
            history_store.add_play(
                provider=provider,
                track_id=track_id,
                played_at=played_at,
                title=title,
                artist=artist,
                album=album,
                duration_seconds=duration_seconds,
                art_url=art_url,
                quality=quality,
                play_seconds=None,
            )
            logger.debug("backfill: inserted %s/%s played_at=%s", provider, track_id, played_at)

        inserted += 1
        seen.add(key)

    logger.info(
        "backfill: %s inserted=%d skipped=%d orphans=%d skipped_failed_decode=%d "
        "skipped_placeholder=%d log=%s",
        "(dry-run)" if dry_run else "done",
        inserted,
        skipped,
        orphans,
        skipped_failed_decode,
        skipped_placeholder,
        log_path,
    )
    return {
        "inserted": inserted,
        "skipped": skipped,
        "orphans": orphans,
        "skipped_failed_decode": skipped_failed_decode,
        "skipped_placeholder": skipped_placeholder,
    }
