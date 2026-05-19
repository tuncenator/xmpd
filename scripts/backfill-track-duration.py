#!/usr/bin/env python3
"""Backfill duration_seconds (and album, art_url) for tracks that landed in
TrackStore without them.

Older versions of the daemon's _cmd_play, _cmd_queue, and radio-fetch paths
only persisted title/artist; everything else was discarded. The FLAC
STREAMINFO patcher in stream_proxy reads duration_seconds back out at
stream time, so those NULL rows kept showing 0:00 in mpc even after the
patcher landed. New writes carry the full metadata, but pre-existing rows
need a one-shot fill.

Usage:
    scripts/backfill-track-duration.py [--dry-run] [--provider tidal|yt]
                                       [--limit N] [--rate-limit-ms MS]

Notes:
- Requires the same auth state the daemon uses (cookies for YT, OAuth for
  Tidal). Tracks for which the provider cannot return metadata are left
  unchanged and counted as failures.
- Rate-limited per-request (default 750 ms) so the upstream APIs are not
  hammered. The Tidal API in particular returns 429s under burst load.
- Idempotent: each run only touches rows where duration_seconds IS NULL.
- Safe to interrupt; partial progress is committed per-row.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from collections.abc import Iterable
from typing import Any

from xmpd.config import load_config
from xmpd.providers import build_registry
from xmpd.providers.base import Provider, TrackMetadata
from xmpd.track_store import TrackStore

logger = logging.getLogger("backfill_duration")


def _iter_missing(
    conn: sqlite3.Connection, provider: str | None, limit: int | None
) -> Iterable[tuple[str, str]]:
    """Yield (provider, track_id) for every row with NULL duration_seconds."""
    sql = (
        "SELECT provider, track_id FROM tracks "
        "WHERE duration_seconds IS NULL"
    )
    params: list[Any] = []
    if provider is not None:
        sql += " AND provider = ?"
        params.append(provider)
    sql += " ORDER BY provider, track_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cur = conn.execute(sql, params)
    for row in cur.fetchall():
        yield row[0], row[1]


def _backfill_one(
    store: TrackStore,
    prov: Provider,
    provider_name: str,
    track_id: str,
    dry_run: bool,
) -> tuple[bool, TrackMetadata | None]:
    """Fetch metadata for one track and update the row. Returns (ok, meta)."""
    try:
        meta = prov.get_track_metadata(track_id)
    except Exception as e:
        logger.warning("%s/%s: provider lookup failed: %s", provider_name, track_id, e)
        return False, None
    if meta is None:
        logger.info("%s/%s: provider returned no metadata", provider_name, track_id)
        return False, None
    if meta.duration_seconds is None:
        logger.info(
            "%s/%s: provider returned metadata without duration; skipping",
            provider_name, track_id,
        )
        return False, meta
    if dry_run:
        logger.info(
            "%s/%s: would set duration_seconds=%d album=%r art=%r (dry-run)",
            provider_name, track_id, meta.duration_seconds, meta.album,
            (meta.art_url or "")[:60],
        )
        return True, meta
    store.update_metadata(
        provider=provider_name,
        track_id=track_id,
        album=meta.album,
        duration_seconds=meta.duration_seconds,
        art_url=meta.art_url,
    )
    return True, meta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing")
    parser.add_argument("--provider", choices=("tidal", "yt"),
                        help="Restrict backfill to one provider")
    parser.add_argument("--limit", type=int,
                        help="Process at most N rows")
    parser.add_argument("--rate-limit-ms", type=int, default=750,
                        help="Sleep between provider calls (default 750 ms)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = load_config()
    registry = build_registry(config)
    if not registry:
        logger.error("No providers enabled in config; nothing to do.")
        return 2

    if args.provider and args.provider not in registry:
        logger.error(
            "Provider %r is not enabled in config (enabled: %s)",
            args.provider, sorted(registry.keys()),
        )
        return 2

    db_path = config["proxy_track_mapping_db"]
    store = TrackStore(db_path)
    # Reuse the same sqlite connection for the row scan; updates go through
    # TrackStore which manages its own locking.
    conn = sqlite3.connect(db_path)

    updated = 0
    failed = 0
    skipped_no_provider = 0
    seen = 0

    try:
        for provider_name, track_id in _iter_missing(conn, args.provider, args.limit):
            seen += 1
            prov = registry.get(provider_name)
            if prov is None:
                skipped_no_provider += 1
                continue

            ok, meta = _backfill_one(store, prov, provider_name, track_id, args.dry_run)
            if ok:
                updated += 1
                if meta is not None and meta.duration_seconds is not None:
                    logger.info(
                        "%s/%s: duration_seconds=%d",
                        provider_name, track_id, meta.duration_seconds,
                    )
            else:
                failed += 1

            if args.rate_limit_ms > 0:
                time.sleep(args.rate_limit_ms / 1000.0)
    except KeyboardInterrupt:
        logger.warning("Interrupted; partial progress preserved.")
    finally:
        conn.close()
        store.close()

    logger.info(
        "Done. seen=%d updated=%d failed=%d skipped_no_provider=%d (dry_run=%s)",
        seen, updated, failed, skipped_no_provider, args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
