"""Queries command handlers for the daemon socket protocol.

The daemon owns connections and state; handlers implement command behavior.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import TYPE_CHECKING, Any

from xmpd.providers.base import Provider

if TYPE_CHECKING:
    from xmpd.daemon import XMPDaemon

logger = logging.getLogger(__name__)


def search_json(daemon: XMPDaemon, args: list[str]) -> dict[str, Any]:
    """Handle 'search-json' command - return structured JSON search results.

    Syntax: search-json [--provider yt|all] [--limit N] QUERY

    Args:
        args: Remaining command tokens after 'search-json'.

    Returns:
        Response dict with 'success' and 'results' (list of track dicts).
        Each track dict has: provider, track_id, title, artist, album,
        duration, duration_seconds, quality, liked.
    """
    # Parse args: consume --provider and --limit flags, rest is query
    provider_filter = "all"
    limit = 25
    remaining: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--provider" and i + 1 < len(args):
            provider_filter = args[i + 1]
            i += 2
        elif args[i] == "--limit" and i + 1 < len(args):
            try:
                limit = int(args[i + 1])
            except ValueError:
                pass
            i += 2
        else:
            remaining.append(args[i])
            i += 1

    query = " ".join(remaining).strip()
    logger.info(
        "search-json command: query=%r, provider=%s, limit=%d",
        query,
        provider_filter,
        limit,
    )

    if not query:
        return {"success": False, "error": "Empty search query"}

    # Determine which providers to search
    if provider_filter and provider_filter != "all":
        if provider_filter not in daemon.provider_registry:
            return {"success": False, "error": f"Unknown provider: {provider_filter}"}
        targets = {provider_filter: daemon.provider_registry[provider_filter]}
    else:
        targets = daemon.provider_registry

    auth_targets = {}
    for pname, prov in targets.items():
        try:
            is_auth, _ = prov.is_authenticated()
            if is_auth:
                auth_targets[pname] = prov
        except Exception as e:
            logger.warning("search-json: auth check failed for %s: %s", pname, e)

    def _search_provider(
        pname: str,
        prov: Provider,
    ) -> tuple[str, list[tuple[str, str, dict[str, Any]]]]:
        search_results = prov.search(query, limit=limit)
        fallback_quality = daemon._quality_for_provider(pname)
        hits: list[tuple[str, str, dict[str, Any]]] = []
        for track in search_results:
            duration_secs = track.metadata.duration_seconds or 0
            quality = track.metadata.quality or fallback_quality
            hits.append(
                (
                    track.provider,
                    track.track_id,
                    {
                        "provider": track.provider,
                        "track_id": track.track_id,
                        "title": track.metadata.title,
                        "artist": track.metadata.artist or "Unknown Artist",
                        "album": track.metadata.album or None,
                        "duration": daemon._format_duration(duration_secs),
                        "duration_seconds": duration_secs,
                        "quality": quality,
                    },
                )
            )
        return pname, hits

    raw_hits: list[tuple[str, str, dict[str, Any]]] = []
    with ThreadPoolExecutor(max_workers=len(auth_targets) + 1) as pool:
        liked_ids_future = pool.submit(daemon._get_liked_ids)
        search_futures = {
            pool.submit(_search_provider, pname, prov): pname
            for pname, prov in auth_targets.items()
        }
        for future in as_completed(search_futures):
            pname = search_futures[future]
            try:
                _, hits = future.result()
                raw_hits.extend(hits)
            except Exception as e:
                logger.warning("search-json: search failed for %s: %s", pname, e)
        liked_ids = liked_ids_future.result()

    results: list[dict[str, Any]] = []
    for provider, track_id, entry in raw_hits:
        entry["liked"] = f"{provider}:{track_id}" in liked_ids if track_id else None
        results.append(entry)
    logger.info("search-json: returning %d results for %r", len(results), query)
    return {"success": True, "results": results}


def history_json(daemon: XMPDaemon, args: list[str]) -> dict[str, Any]:
    """Handle 'history-json' command - return local history rows.

    Syntax: history-json [--mode time|count] [--since ISO|all] [--limit N]

    Args:
        args: Remaining command tokens after 'history-json'.

    Returns:
        Response dict with 'success' and 'rows' (list of row dicts).
        Each row dict carries the columns in the local plays table
        plus, in count mode, 'play_count' and 'last_played_at'.
    """
    if daemon.history_store is None:
        return {"success": False, "error": "history not enabled"}

    mode = "time"
    since_str = "all"
    limit = 5000
    i = 0
    while i < len(args):
        if args[i] == "--mode" and i + 1 < len(args):
            mode = args[i + 1]
            i += 2
        elif args[i] == "--since" and i + 1 < len(args):
            since_str = args[i + 1]
            i += 2
        elif args[i] == "--limit" and i + 1 < len(args):
            try:
                limit = int(args[i + 1])
            except ValueError:
                pass
            i += 2
        else:
            i += 1

    if mode not in ("time", "count"):
        return {"success": False, "error": "mode must be time or count"}
    assert mode in ("time", "count")  # narrowing for mypy

    since: datetime | None = None
    if since_str != "all":
        try:
            since = datetime.fromisoformat(since_str)
        except ValueError:
            return {"success": False, "error": f"invalid since: {since_str}"}

    try:
        rows = daemon.history_store.get_plays(
            mode=mode,  # type: ignore[arg-type]
            since=since,
            limit=limit,
        )
    except sqlite3.Error as e:
        logger.exception("history-json: SQLite error")
        return {"success": False, "error": f"history-json: {e}"}

    logger.info(
        "history-json: mode=%s since=%s limit=%d -> %d rows",
        mode,
        since_str,
        limit,
        len(rows),
    )
    return {"success": True, "rows": rows}


def history_backfill(daemon: XMPDaemon, args: list[str]) -> dict[str, Any]:
    """Handle 'history-backfill' IPC command.

    Parses ``--log PATH`` and ``--dry-run`` from args, resolves the MPD log
    path (explicit -> config -> autodetect), calls run_backfill, and if rows
    were inserted triggers one bidir push.

    Args:
        args: Remaining command tokens after 'history-backfill'.

    Returns:
        Response dict with 'success', 'inserted', 'skipped', 'orphans',
        'dry_run', and 'log_path'; or 'success'=False and 'error' on failure.
    """
    from xmpd.history_backfill import run_backfill as _run_backfill

    if not daemon.history_store:
        return {"success": False, "error": "history.enabled is false"}

    log_path: str | None = None
    dry_run = False
    i = 0
    while i < len(args):
        if args[i] == "--log" and i + 1 < len(args):
            log_path = args[i + 1]
            i += 2
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            i += 1

    # Resolution chain: explicit -> config -> autodetect
    if not log_path:
        log_path = (daemon.config.get("history") or {}).get("mpd_log_path")
    if not log_path:
        log_path = daemon._autodetect_mpd_log_path()
    if not log_path:
        return {"success": False, "error": "could not locate MPD log file"}

    log_path = os.path.expanduser(log_path)
    if not os.path.isfile(log_path):
        return {"success": False, "error": f"log file not found: {log_path}"}

    try:
        result = _run_backfill(
            daemon.history_store,
            daemon.track_store,
            log_path,
            dry_run=dry_run,
            mpd_socket_path=daemon.config.get("mpd_socket_path"),
        )
    except Exception as exc:
        logger.error("history-backfill failed: %s", exc, exc_info=True)
        return {"success": False, "error": str(exc)}

    # Trigger one bidir push if anything was inserted and not a dry-run
    if not dry_run and result["inserted"] > 0 and daemon.history_syncer is not None:
        try:
            if daemon._history_executor is not None:
                daemon._history_executor.submit(daemon.history_syncer.bidir_push)
        except Exception as exc:
            logger.warning("history-backfill: failed to submit bidir push: %s", exc)

    logger.info(
        "history-backfill: inserted=%d skipped=%d orphans=%d"
        " skipped_failed_decode=%d skipped_placeholder=%d dry_run=%s log=%s",
        result["inserted"],
        result["skipped"],
        result["orphans"],
        result["skipped_failed_decode"],
        result["skipped_placeholder"],
        dry_run,
        log_path,
    )
    return {
        "success": True,
        "inserted": result["inserted"],
        "skipped": result["skipped"],
        "orphans": result["orphans"],
        "skipped_failed_decode": result["skipped_failed_decode"],
        "skipped_placeholder": result["skipped_placeholder"],
        "dry_run": dry_run,
        "log_path": log_path,
    }
