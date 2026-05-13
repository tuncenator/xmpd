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
) -> dict[str, int]:
    """Import all ``player: played`` events from an MPD log into the history store.

    Idempotent: rows already present in the DB (by played_at + provider +
    track_id key) are counted as ``skipped``, not re-inserted.

    Within-log duplicates (same second, same track) collapse to one row.
    Malformed lines are debug-logged and excluded from all counters.
    Orphan rows (no track_store match) are inserted with NULL metadata fields
    and counted under ``orphans``; they are still included in ``inserted``.

    Args:
        history_store: Open HistoryStore for this host.
        track_store: TrackStore for metadata enrichment, or None.
        log_path: Absolute path to the MPD log file.
        dry_run: If True, parse and count but do not write to the DB.

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
        m = LOG_LINE_RE.match(line)
        if not m:
            logger.debug("backfill: skipping non-match line: %r", line)
            continue
        ts_str = m.group("ts")
        provider = m.group("provider")
        track_id = m.group("track_id")
        try:
            played_at = _parse_played_at(ts_str, log_mtime)
        except ValueError:
            logger.warning("backfill: unrecognized timestamp %r -- skipping line", ts_str)
            continue
        matches.append((played_at, provider, track_id))

    inserted = 0
    skipped = 0
    orphans = 0
    skipped_failed_decode = 0

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

        # Metadata lookup (done for all rows to compute orphan count accurately)
        track = track_store.get_track(provider, track_id) if track_store is not None else None
        is_orphan = track is None

        if is_orphan:
            orphans += 1

        key = (played_at, provider, track_id)
        if key in seen:
            skipped += 1
            continue

        if is_orphan:
            title: str | None = None
            artist: str | None = None
            album: str | None = None
            duration_seconds: int | None = None
            art_url: str | None = None
            quality: str | None = None
        else:
            assert track is not None
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
        "backfill: %s inserted=%d skipped=%d orphans=%d skipped_failed_decode=%d log=%s",
        "(dry-run)" if dry_run else "done",
        inserted,
        skipped,
        orphans,
        skipped_failed_decode,
        log_path,
    )
    return {
        "inserted": inserted,
        "skipped": skipped,
        "orphans": orphans,
        "skipped_failed_decode": skipped_failed_decode,
    }
