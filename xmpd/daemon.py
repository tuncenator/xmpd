"""Sync daemon for xmpd - multi-provider music sync to MPD.

This module implements the XMPDaemon class which coordinates provider-agnostic
playlist syncing to MPD, with support for periodic auto-sync, history reporting,
rating dispatch, and an HTTP audio proxy.
"""

import asyncio
import json
import logging
import os
import re
import signal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mpd import MPDClient as MPDClientBase

from xmpd.auth.ytmusic_cookie import FirefoxCookieExtractor
from xmpd.commands import playback, queries, ratings
from xmpd.config import get_config_dir, load_config
from xmpd.config import get_playlist_prefixes as _build_playlist_prefix
from xmpd.exceptions import CookieExtractionError, MPDConnectionError
from xmpd.history_reporter import HistoryReporter
from xmpd.history_store import HistoryStore
from xmpd.history_syncer import HistorySyncer
from xmpd.mpd_client import MPDClient
from xmpd.notify import send_notification
from xmpd.providers import build_registry
from xmpd.providers.base import Provider
from xmpd.rating import RatingManager
from xmpd.stream_proxy import StreamRedirectProxy, resolve_stream_cache_hours
from xmpd.stream_resolver import StreamResolver
from xmpd.sync_engine import SyncEngine
from xmpd.track_store import TrackStore

logger = logging.getLogger(__name__)

# Candidate paths for MPD configuration files (used by _autodetect_mpd_log_path).
_MPDCONF_CANDIDATES = ["~/.mpdconf", "~/.mpd/mpd.conf", "/etc/mpd.conf"]


def _build_yt_config(config: dict[str, Any]) -> dict[str, Any]:
    """Synthesize a ``yt`` provider config section from legacy top-level keys.

    Legacy configurations may omit the ``yt:`` section. Normalize that
    shape before building the provider registry.
    """
    if "yt" in config and isinstance(config["yt"], dict):
        # Already has the new shape; ensure ``enabled`` defaults to True
        section = dict(config["yt"])
        section.setdefault("enabled", True)
        return section
    # Legacy config: synthesize from top-level keys
    return {"enabled": True}


class XMPDaemon:
    """Multi-provider sync daemon for xmpd.

    The daemon:
    - Builds a provider registry from config and probes authentication
    - Injects the registry into SyncEngine, HistoryReporter, StreamRedirectProxy
    - Runs periodic sync loop in background thread
    - Listens for manual sync triggers via Unix socket
    - Persists sync state between runs
    - Handles signals for graceful shutdown and config reload
    """

    def __init__(self) -> None:
        """Initialize the daemon with all sync components."""
        logger.info("Initializing xmpd sync daemon...")

        # Load configuration
        self.config = load_config()
        logger.info("Configuration loaded")

        # Runtime control (set early so lambdas referencing _running resolve)
        self._running = False

        # Initialize core components
        try:
            self.mpd_client = MPDClient(
                socket_path=self.config["mpd_socket_path"],
                playlist_directory=self.config.get("mpd_playlist_directory"),
            )

            # Persistent cache file for stream URLs
            cache_file = get_config_dir() / "stream_cache.json"
            self.stream_resolver = StreamResolver(
                cache_hours=self.config["stream_cache_hours"],
                should_stop_callback=lambda: not self._running,
                cache_file=str(cache_file),
            )

            # Initialize proxy components if enabled
            self.track_store: TrackStore | None = None
            self.proxy_server: StreamRedirectProxy | None = None
            self.proxy_config: dict[str, Any] | None = None

            if self.config.get("proxy_enabled", True):
                logger.info("Initializing stream proxy server...")
                self.track_store = TrackStore(self.config["proxy_track_mapping_db"])

            # ----- Provider registry -----
            # Ensure config has a yt section for build_registry
            registry_config = dict(self.config)
            registry_config["yt"] = _build_yt_config(self.config)

            raw_registry = build_registry(registry_config, stream_resolver=self.stream_resolver)

            self.provider_registry: dict[str, Provider] = {}
            for name, provider in raw_registry.items():
                try:
                    is_auth, err = provider.is_authenticated()
                except Exception as exc:
                    logger.warning("%s authentication probe raised: %s", name, exc)
                    is_auth, err = False, str(exc)

                if is_auth:
                    logger.info("Provider %s: ready", name)
                else:
                    logger.warning(
                        "%s not configured (%s); run 'xmpctl auth %s'",
                        name,
                        err or "no credentials",
                        name,
                    )
                # Keep all providers in registry for provider-status reporting;
                # downstream consumers (sync, proxy, history) guard with
                # is_authenticated() before network calls.
                self.provider_registry[name] = provider

            # ----- Proxy server -----
            if self.config.get("proxy_enabled", True) and self.track_store is not None:
                self.proxy_server = StreamRedirectProxy(
                    track_store=self.track_store,
                    provider_registry=self.provider_registry,
                    stream_resolver=self.stream_resolver,
                    host=self.config["proxy_host"],
                    port=self.config["proxy_port"],
                    stream_cache_hours=resolve_stream_cache_hours(self.config),
                )
                self.proxy_config = {
                    "enabled": True,
                    "host": self.config["proxy_host"],
                    "port": self.config["proxy_port"],
                }
                logger.info(
                    "Proxy server initialized at %s:%s",
                    self.config["proxy_host"],
                    self.config["proxy_port"],
                )

            # ----- Playlist prefix -----
            playlist_prefix = _build_playlist_prefix(self.config)

            # ----- SyncEngine -----
            self.sync_engine = SyncEngine(
                provider_registry=self.provider_registry,
                mpd_client=self.mpd_client,
                track_store=self.track_store or TrackStore(":memory:"),
                playlist_prefix=playlist_prefix,
                proxy_config=self.proxy_config,
                should_stop_callback=lambda: not self._running,
                playlist_format=self.config.get("playlist_format", "m3u"),
                mpd_music_directory=self.config.get("mpd_music_directory"),
                sync_favorites=self.config.get("sync_liked_songs", True),
                like_indicator=self.config.get(
                    "like_indicator", {"enabled": False, "tag": "+1", "alignment": "right"}
                ),
            )

            # ----- Rating manager -----
            self._rating_manager = RatingManager()

        except Exception as e:
            logger.error("Failed to initialize components: %s", e)
            raise

        # State management
        self.state_file = get_config_dir() / "sync_state.json"
        self.state = self._load_state()

        # Runtime control (threads)
        self._sync_thread: threading.Thread | None = None
        self._socket_thread: threading.Thread | None = None
        self._proxy_thread: threading.Thread | None = None
        self._sync_in_progress = False
        self._sync_lock = threading.Lock()

        # Socket for manual triggers
        self.sync_socket_path = get_config_dir() / "sync_socket"

        # Async event loop for proxy server (if enabled)
        self._proxy_loop: asyncio.AbstractEventLoop | None = None
        self._proxy_shutdown_event: asyncio.Event | None = None

        # ----- Auto-auth (YouTube: pull cookies straight from Firefox) -----
        yt_section = self.config.get("yt", {})
        self.auto_auth_config: dict[str, Any] = (
            yt_section.get("auto_auth", {}) if isinstance(yt_section, dict) else {}
        )
        self._auto_auth_enabled = bool(self.auto_auth_config.get("enabled", False))
        self._auto_auth_thread: threading.Thread | None = None
        self._auto_auth_shutdown = threading.Event()
        self._last_reactive_refresh: float = 0.0
        self._reactive_refresh_cooldown: float = 300.0  # 5 minutes
        if self._auto_auth_enabled:
            logger.info(
                "Auto-auth enabled (browser: %s, refresh every %sh)",
                self.auto_auth_config.get("browser", "firefox-dev"),
                self.auto_auth_config.get("refresh_interval_hours", 12),
            )
        else:
            logger.info("Auto-auth disabled")

        # Liked IDs cache for search-json like-state population
        self._liked_ids_cache: set[str] = set()
        self._liked_ids_cache_time: float = 0.0
        self._liked_ids_cache_ttl: float = 300.0  # 5 minutes

        # History store / syncer / executor (new in xmpd-history feature)
        self.history_store: HistoryStore | None = None
        self.history_syncer: HistorySyncer | None = None
        self._history_executor: ThreadPoolExecutor | None = None
        history_cfg = self.config.get("history", {})
        if history_cfg.get("enabled", False) and self.track_store is not None:
            self.history_store = HistoryStore(history_cfg["db_path"])
            watchtower_cfg = history_cfg["watchtower"]
            self.history_syncer = HistorySyncer(
                history_store=self.history_store,
                ssh_target=watchtower_cfg["ssh_target"],
                tailscale_hostname=watchtower_cfg["tailscale_hostname"],
                bidir_batch=watchtower_cfg["bidir_batch"],
                pull_batch=watchtower_cfg["pull_batch"],
            )
            self._history_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="hist-sync",
            )
            logger.info(
                "History store enabled: db=%s, ssh_target=%s",
                history_cfg["db_path"],
                watchtower_cfg["ssh_target"],
            )
        else:
            logger.info("History store disabled")

        # History reporting (existing block -- now passes the new collaborators)
        self._history_reporter: HistoryReporter | None = None
        self._history_thread: threading.Thread | None = None
        self._history_shutdown = threading.Event()
        history_reporting_cfg = self.config.get("history_reporting", {})
        if history_reporting_cfg.get("enabled", False) and self.track_store is not None:
            self._history_reporter = HistoryReporter(
                mpd_socket_path=self.config["mpd_socket_path"],
                provider_registry=self.provider_registry,
                track_store=self.track_store,
                proxy_config=self.proxy_config or {},
                min_play_seconds=history_reporting_cfg.get("min_play_seconds", 30),
                history_store=self.history_store,
                history_syncer=self.history_syncer,
                executor=self._history_executor,
            )
            logger.info(
                "History reporting enabled (min_play_seconds=%d)",
                history_reporting_cfg.get("min_play_seconds", 30),
            )
        else:
            logger.info("History reporting disabled")

        logger.info("Daemon components initialized")

    def run(self) -> None:
        """Main daemon loop - starts all background tasks and blocks until shutdown."""
        logger.info("Starting xmpd sync daemon...")

        # Connect to MPD
        try:
            self.mpd_client.connect()
            logger.info("Connected to MPD")
        except MPDConnectionError as e:
            logger.error(f"Failed to connect to MPD: {e}")
            raise

        self._running = True
        self.state["daemon_start_time"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self._save_state()

        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGHUP, self._signal_handler)

        # Start background threads (daemon=True allows process to exit even if threads are stuck)
        self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._socket_thread = threading.Thread(target=self._listen_for_triggers, daemon=True)

        self._sync_thread.start()
        self._socket_thread.start()

        # Start proxy server if enabled
        if self.proxy_server:
            logger.info("Starting stream proxy server...")
            self._proxy_thread = threading.Thread(target=self._run_proxy_server, daemon=True)
            self._proxy_thread.start()

        # Start history reporting thread if enabled (after proxy so URLs resolve)
        if self._history_reporter is not None:
            self._history_thread = threading.Thread(
                target=self._history_loop,
                name="history-reporter",
                daemon=True,
            )
            self._history_thread.start()
            logger.info("History reporting thread started")

        # Trigger startup nudge so any rows queued while offline get drained early.
        if self.history_syncer is not None:
            try:
                self.history_syncer.startup_nudge()
            except Exception as e:
                logger.warning("history startup_nudge failed: %s", e)

        # Refresh YouTube auth straight from Firefox before the first sync, then
        # keep it fresh on a schedule.
        if self._auto_auth_enabled:
            logger.info("Auto-auth: refreshing YouTube session from Firefox at startup...")
            if self._attempt_auto_refresh():
                logger.info("Auto-auth: startup refresh succeeded")
            else:
                logger.warning("Auto-auth: startup refresh failed")
                self._notify_refresh_failed()
            self._auto_auth_thread = threading.Thread(
                target=self._auto_auth_loop, name="auto-auth", daemon=True
            )
            self._auto_auth_thread.start()

        logger.info("xmpd daemon started successfully")

        # Perform initial sync immediately
        if self.config.get("enable_auto_sync", True):
            logger.info("Triggering initial sync...")
            self._perform_sync()

        # Keep main thread alive
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt")
            self._running = False

        # Cleanup after main loop exits
        logger.info("Main loop exited, cleaning up...")
        sync_alive = self._sync_thread.is_alive() if self._sync_thread else None
        socket_alive = self._socket_thread.is_alive() if self._socket_thread else None
        proxy_alive = self._proxy_thread.is_alive() if self._proxy_thread else None
        logger.debug(
            f"Threads alive: sync={sync_alive}, socket={socket_alive}, proxy={proxy_alive}"
        )
        self.stop()

    def stop(self) -> None:
        """Stop the daemon gracefully."""
        if not self._running:
            logger.debug("Stop called but daemon is already stopped")
            return

        logger.info("Stopping xmpd daemon...")
        self._running = False

        # Shut down the history sync executor before joining the reporter thread.
        if self._history_executor is not None:
            try:
                self._history_executor.shutdown(wait=False, cancel_futures=True)
                logger.info("history sync executor shutdown")
            except Exception as e:
                logger.warning("history executor shutdown failed: %s", e)

        # Signal auto-auth refresh thread to stop
        self._auto_auth_shutdown.set()
        if self._auto_auth_thread is not None and self._auto_auth_thread.is_alive():
            self._auto_auth_thread.join(timeout=5)
            if self._auto_auth_thread.is_alive():
                logger.warning("Auto-auth thread did not stop in time")

        # Signal history reporter to stop
        if self._history_thread is not None:
            logger.info("Stopping history reporter...")
            self._history_shutdown.set()
            self._history_thread.join(timeout=5)
            if self._history_thread.is_alive():
                logger.warning("History reporter thread did not stop in time")

        # Note: Sync will detect _running=False and cancel itself gracefully
        if self._sync_in_progress:
            logger.info("Sync in progress will be cancelled...")

        # Cleanup socket
        if self.sync_socket_path.exists():
            try:
                self.sync_socket_path.unlink()
            except Exception as e:
                logger.warning(f"Error removing socket file: {e}")

        # Disconnect from MPD
        try:
            self.mpd_client.disconnect()
        except Exception as e:
            logger.warning(f"Error disconnecting from MPD: {e}")

        # Stop proxy server if enabled
        if self.proxy_server and self._proxy_loop:
            logger.info("Stopping stream proxy server...")
            try:
                # Signal the proxy server to shut down
                if self._proxy_shutdown_event:

                    def set_shutdown_event() -> None:
                        if self._proxy_shutdown_event:
                            self._proxy_shutdown_event.set()

                    self._proxy_loop.call_soon_threadsafe(set_shutdown_event)

                # Wait for proxy thread to finish (10s timeout for HTTP cleanup)
                if self._proxy_thread and self._proxy_thread.is_alive():
                    logger.debug("Waiting for proxy thread to stop...")
                    self._proxy_thread.join(timeout=10)
                    if self._proxy_thread.is_alive():
                        logger.warning("Proxy thread did not stop within 10s timeout")
                    else:
                        logger.info("Proxy thread stopped successfully")
            except Exception as e:
                logger.warning(f"Error stopping proxy server: {e}")

        # Close TrackStore database connection
        if self.track_store:
            try:
                self.track_store.close()
                logger.info("TrackStore closed")
            except Exception as e:
                logger.warning(f"Error closing TrackStore: {e}")

        # Wait for threads to finish
        if self._sync_thread and self._sync_thread.is_alive():
            logger.debug("Waiting for sync thread to stop...")
            self._sync_thread.join(timeout=5)
            if self._sync_thread.is_alive():
                logger.warning("Sync thread did not stop within timeout")

        if self._socket_thread and self._socket_thread.is_alive():
            logger.debug("Waiting for socket thread to stop...")
            self._socket_thread.join(timeout=2)
            if self._socket_thread.is_alive():
                logger.warning("Socket thread did not stop within timeout")

        # Final check - log any threads still alive
        threads_alive = []
        if self._sync_thread and self._sync_thread.is_alive():
            threads_alive.append("sync")
        if self._socket_thread and self._socket_thread.is_alive():
            threads_alive.append("socket")
        if self._proxy_thread and self._proxy_thread.is_alive():
            threads_alive.append("proxy")
        if self._history_thread and self._history_thread.is_alive():
            threads_alive.append("history")

        if threads_alive:
            logger.warning(f"Daemon stopping with threads still alive: {', '.join(threads_alive)}")
            logger.warning("Process will exit (threads are daemon threads)")
        else:
            logger.info("All threads stopped cleanly")

        logger.info("xmpd daemon stopped")

    def _sync_loop(self) -> None:
        """Background thread for periodic sync."""
        logger.info("Starting periodic sync loop")

        if not self.config.get("enable_auto_sync", True):
            logger.info("Auto-sync disabled, periodic sync loop inactive")
            return

        interval_minutes = self.config["sync_interval_minutes"]
        interval_seconds = interval_minutes * 60

        try:
            while self._running:
                # Sleep in small intervals to allow quick shutdown
                for _ in range(int(interval_seconds)):
                    if not self._running:
                        break
                    time.sleep(1)

                if self._running:
                    self._perform_sync()

        except Exception as e:
            logger.error(f"Error in sync loop: {e}", exc_info=True)

        logger.info("Periodic sync loop stopped")

    def _run_proxy_server(self) -> None:
        """Background thread for running the async proxy server."""
        logger.info("Starting proxy server thread")

        try:
            # Create new event loop for this thread
            self._proxy_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._proxy_loop)

            # Run proxy server in this loop
            async def run_server() -> None:
                """Async wrapper to run the proxy server."""
                # Create shutdown event in the async context
                self._proxy_shutdown_event = asyncio.Event()

                proxy_server = self.proxy_server
                if proxy_server is None:
                    return
                async with proxy_server:
                    logger.info(
                        f"Proxy server running at http://{self.config['proxy_host']}:{self.config['proxy_port']}"
                    )
                    # Keep server running until shutdown event is set
                    await self._proxy_shutdown_event.wait()

            self._proxy_loop.run_until_complete(run_server())

        except Exception as e:
            logger.error(f"Error in proxy server thread: {e}", exc_info=True)

        finally:
            if self._proxy_loop:
                # Cancel all pending tasks
                try:
                    pending = asyncio.all_tasks(self._proxy_loop)
                    for task in pending:
                        task.cancel()
                    # Wait for all tasks to be cancelled
                    if pending:
                        self._proxy_loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                except Exception as e:
                    logger.warning(f"Error cancelling tasks: {e}")

                # Close the loop
                self._proxy_loop.close()
            logger.info("Proxy server thread stopped")

    def _history_loop(self) -> None:
        """Run history reporter in background thread."""
        try:
            assert self._history_reporter is not None
            logger.info(
                "History reporting started (min_play_seconds=%d)",
                self.config["history_reporting"]["min_play_seconds"],
            )
            self._history_reporter.run(self._history_shutdown)
        except Exception as e:
            logger.error("History reporter crashed: %s", e, exc_info=True)
        finally:
            logger.info("History reporting stopped")

    # -----------------------------------------------------------------------
    # Auto-auth: refresh YouTube's browser.json from the Firefox cookie store
    # -----------------------------------------------------------------------

    def _attempt_auto_refresh(self) -> bool:
        """Extract fresh YouTube cookies from Firefox and re-init the yt client.

        Writes browser.json atomically, reloads the provider, then verifies the
        session is actually live (cookie presence alone does not prove YouTube
        still honors the session). Returns True only on a verified live session.
        """
        yt = self.provider_registry.get("yt")
        if yt is None or not hasattr(yt, "refresh_auth"):
            logger.warning("Auto-auth: no yt provider available to refresh")
            return False

        browser_json = get_config_dir() / "browser.json"
        try:
            extractor = FirefoxCookieExtractor(
                browser=self.auto_auth_config.get("browser", "firefox-dev"),
                profile=self.auto_auth_config.get("profile"),
                container=self.auto_auth_config.get("container"),
            )
            # Write to a temp file first, then rename for atomicity.
            tmp_path = browser_json.with_suffix(".json.tmp")
            extractor.build_browser_json(tmp_path)
            tmp_path.rename(browser_json)
        except CookieExtractionError as e:
            logger.error("Auto-auth: cookie extraction failed: %s", e)
            return self._record_refresh_failure()
        except Exception as e:
            logger.error("Auto-auth: cookie extraction crashed: %s", e, exc_info=True)
            return self._record_refresh_failure()

        if not yt.refresh_auth(browser_json):
            logger.error("Auto-auth: client re-init failed after cookie refresh")
            return self._record_refresh_failure()

        # Cookies were written, but confirm YouTube accepts the session. A
        # signed-out Firefox profile still yields present, unexpired cookies
        # that validate locally yet are rejected server-side.
        if hasattr(yt, "has_live_session") and not yt.has_live_session():
            logger.error(
                "Auto-auth: cookies refreshed but YouTube session is not signed in "
                "(sign into music.youtube.com in Firefox)"
            )
            return self._record_refresh_failure()

        self.state["last_auto_refresh"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self.state["auto_refresh_failures"] = 0
        self._save_state()
        logger.info(
            "Auto-auth: YouTube session refreshed from Firefox (%s)",
            self.auto_auth_config.get("browser", "firefox-dev"),
        )
        return True

    def _record_refresh_failure(self) -> bool:
        """Increment the persisted failure counter and return False."""
        self.state["auto_refresh_failures"] = self.state.get("auto_refresh_failures", 0) + 1
        self._save_state()
        return False

    def _notify_refresh_failed(self) -> None:
        """Desktop-notify the user that auto cookie refresh failed."""
        send_notification(
            "xmpd: YouTube auth refresh failed",
            "Could not refresh cookies from Firefox. Open music.youtube.com in "
            "Firefox Developer Edition and make sure you are signed in.",
            urgency="normal",
        )

    def _auto_auth_loop(self) -> None:
        """Background thread: periodically refresh cookies from Firefox."""
        logger.info("Starting auto-auth refresh loop")
        interval_hours = self.auto_auth_config.get("refresh_interval_hours", 12)
        interval_seconds = max(1.0, float(interval_hours) * 3600)

        try:
            while self._running:
                # Wait for the interval, waking early on shutdown.
                if self._auto_auth_shutdown.wait(timeout=interval_seconds):
                    break
                if not self._running:
                    break

                logger.info("Proactive auto-auth refresh triggered")
                if self._attempt_auto_refresh():
                    logger.info("Proactive auto-auth refresh succeeded")
                else:
                    logger.warning("Proactive auto-auth refresh failed")
                    self._notify_refresh_failed()
        except Exception as e:
            logger.error("Error in auto-auth loop: %s", e, exc_info=True)

        logger.info("Auto-auth refresh loop stopped")

    def _maybe_reactive_refresh(self) -> None:
        """Refresh from Firefox if the YouTube session has gone dead mid-run.

        Called before each sync. Rate-limited by a cooldown so a persistently
        broken session does not hammer extraction every cycle.
        """
        if not self._auto_auth_enabled:
            return
        yt = self.provider_registry.get("yt")
        if yt is None or not hasattr(yt, "has_live_session"):
            return
        now = time.time()
        if now - self._last_reactive_refresh < self._reactive_refresh_cooldown:
            return
        try:
            live = yt.has_live_session()
        except Exception:
            live = False
        if live:
            return

        self._last_reactive_refresh = now
        logger.info("Auto-auth: YouTube session is not live, refreshing from Firefox")
        if self._attempt_auto_refresh():
            logger.info("Auto-auth: reactive refresh succeeded")
        else:
            logger.warning("Auto-auth: reactive refresh failed")
            self._notify_refresh_failed()

    def _perform_sync(self) -> None:
        """Execute sync and update state."""
        # Skip if sync already in progress
        if self._sync_in_progress:
            logger.warning("Sync already in progress, skipping")
            return

        # Ensure the YouTube session is live before syncing (auto-auth only).
        self._maybe_reactive_refresh()

        with self._sync_lock:
            self._sync_in_progress = True
            logger.info("Starting sync...")
            start_time = time.time()

            try:
                # Perform sync
                result = self.sync_engine.sync_all_playlists()

                # Update state
                self.state["last_sync"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                self.state["last_sync_result"] = {
                    "success": result.success,
                    "playlists_synced": result.playlists_synced,
                    "playlists_failed": result.playlists_failed,
                    "tracks_added": result.tracks_added,
                    "tracks_failed": result.tracks_failed,
                    "duration_seconds": result.duration_seconds,
                    "errors": result.errors,
                }
                self._save_state()

                # Log result
                if result.success:
                    logger.info(
                        f"Sync completed successfully: "
                        f"{result.playlists_synced} playlists, "
                        f"{result.tracks_added} tracks, "
                        f"{result.duration_seconds:.1f}s"
                    )
                else:
                    logger.warning(
                        f"Sync completed with errors: "
                        f"{result.playlists_synced} playlists synced, "
                        f"{result.playlists_failed} playlists failed, "
                        f"{len(result.errors)} errors"
                    )
                    for error in result.errors:
                        logger.error(f"  - {error}")

            except Exception as e:
                logger.error("Sync failed with exception: %s", e, exc_info=True)

                # Update state with failure
                self.state["last_sync"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                self.state["last_sync_result"] = {
                    "success": False,
                    "playlists_synced": 0,
                    "playlists_failed": 0,
                    "tracks_added": 0,
                    "tracks_failed": 0,
                    "duration_seconds": time.time() - start_time,
                    "errors": [str(e)],
                }
                self._save_state()

            finally:
                self._sync_in_progress = False

    def _listen_for_triggers(self) -> None:
        """Listen for manual sync commands via Unix socket."""
        logger.info(f"Starting socket listener on {self.sync_socket_path}")

        # Remove old socket if it exists
        if self.sync_socket_path.exists():
            try:
                self.sync_socket_path.unlink()
            except Exception as e:
                logger.error(f"Error removing old socket: {e}")
                return

        # Create socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self.sync_socket_path))
            sock.listen(5)
            sock.settimeout(1.0)  # Allow checking _running flag periodically
        except Exception as e:
            logger.error(f"Error creating socket: {e}")
            return

        logger.info("Socket listener started")

        try:
            while self._running:
                try:
                    conn, _ = sock.accept()
                except TimeoutError:
                    continue
                except Exception as e:
                    if self._running:
                        logger.error(f"Socket accept error: {e}")
                    continue

                # Handle connection in separate thread
                threading.Thread(
                    target=self._handle_socket_connection, args=(conn,), daemon=True
                ).start()

        finally:
            sock.close()
            if self.sync_socket_path.exists():
                try:
                    self.sync_socket_path.unlink()
                except Exception:
                    pass

        logger.info("Socket listener stopped")

    # ------------------------------------------------------------------
    # Socket command dispatch
    # ------------------------------------------------------------------

    def _handle_socket_connection(self, conn: socket.socket) -> None:
        """Handle a single socket connection."""
        try:
            conn.settimeout(5.0)
            data = conn.recv(1024).decode("utf-8").strip()
            if not data:
                return

            logger.debug("Received command: %s", data)

            parts = data.split()
            cmd = parts[0] if parts else ""
            args = parts[1:]

            # Dispatch
            if cmd == "sync":
                response = self._cmd_sync()
            elif cmd == "status":
                response = self._cmd_status()
            elif cmd == "list":
                response = self._cmd_list()
            elif cmd == "quit":
                response = self._cmd_quit()
            elif cmd == "provider-status":
                response = self._cmd_provider_status()
            elif cmd == "radio":
                if args:
                    provider, remaining_args = self._parse_provider_args(args)
                    # Positional track_id is the first remaining arg (if any)
                    track_id = remaining_args[0] if remaining_args else None
                else:
                    provider, track_id = None, None
                response = self._cmd_radio(provider, track_id)
            elif cmd == "search-json":
                # search-json [--provider yt|all] [--limit N] QUERY
                response = self._cmd_search_json(parts[1:])
            elif cmd == "history-json":
                # history-json [--mode time|count] [--since ISO|all] [--limit N]
                response = self._cmd_history_json(parts[1:])
            elif cmd == "history-backfill":
                # history-backfill [--log PATH] [--dry-run]
                response = self._cmd_history_backfill(parts[1:])
            elif cmd == "play":
                provider, track_id = self._parse_play_queue_args(args)
                response = self._cmd_play(provider, track_id)
            elif cmd == "queue":
                provider, track_id = self._parse_play_queue_args(args)
                response = self._cmd_queue(provider, track_id)
            elif cmd == "like":
                provider = args[0] if len(args) > 0 else None
                track_id = args[1] if len(args) > 1 else None
                response = self._cmd_like(provider, track_id)
            elif cmd == "dislike":
                provider = args[0] if len(args) > 0 else None
                track_id = args[1] if len(args) > 1 else None
                response = self._cmd_dislike(provider, track_id)
            elif cmd == "like-toggle":
                provider = args[0] if len(args) > 0 else None
                track_id = args[1] if len(args) > 1 else None
                response = self._cmd_like_toggle(provider, track_id)
            else:
                response = {"success": False, "error": f"Unknown command: {cmd}"}

            conn.sendall((json.dumps(response) + "\n").encode("utf-8"))

        except TimeoutError:
            logger.warning("Socket connection timed out waiting for command")
            try:
                conn.sendall(
                    (json.dumps({"success": False, "error": "Connection timeout"}) + "\n").encode()
                )
            except Exception:
                pass
        except BrokenPipeError:
            logger.debug("Client disconnected before response could be sent (broken pipe)")
        except Exception as e:
            logger.error("Error handling socket connection: %s", e, exc_info=True)
            try:
                conn.sendall((json.dumps({"success": False, "error": str(e)}) + "\n").encode())
            except (BrokenPipeError, ConnectionResetError, Exception):
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Argument helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_provider_args(args: list[str]) -> tuple[str | None, list[str]]:
        """Pop ``--provider <name>`` from *args*.

        Returns ``(provider_name_or_none, remaining_args)``.
        """
        remaining: list[str] = []
        provider: str | None = None
        it = iter(args)
        for tok in it:
            if tok == "--provider":
                provider = next(it, None)
            elif tok.startswith("--provider="):
                provider = tok.split("=", 1)[1]
            else:
                remaining.append(tok)
        return provider, remaining

    @staticmethod
    def _parse_play_queue_args(args: list[str]) -> tuple[str, str | None]:
        """Parse args for play/queue: ``[provider] track_id``.

        Returns ``(provider, track_id)``.  If only one arg, assumes ``yt``.
        """
        if len(args) >= 2:
            return args[0], args[1]
        if len(args) == 1:
            return "yt", args[0]
        return "yt", None

    def _extract_provider_and_track(
        self,
        url: str,
    ) -> tuple[str | None, str | None]:
        """Extract (provider, track_id) from a proxy URL.

        Handles ``/proxy/{provider}/{track_id}`` (new) and
        ``/proxy/{11-char-id}`` (legacy, assumes yt).
        """
        if not url:
            return None, None
        # New shape: /proxy/yt/VIDEO_ID or /proxy/tidal/12345
        match = re.search(r"/proxy/([a-z]+)/([^/?]+)", url)
        if match:
            return match.group(1), match.group(2)
        # Legacy shape: /proxy/VIDEO_ID (11-char YT id)
        legacy = re.search(r"/proxy/([A-Za-z0-9_-]{11})$", url)
        if legacy:
            return "yt", legacy.group(1)
        return None, None

    # ------------------------------------------------------------------
    # Socket commands
    # ------------------------------------------------------------------

    def _cmd_sync(self) -> dict[str, Any]:
        """Handle 'sync' command."""
        logger.info("Manual sync triggered via socket")
        threading.Thread(target=self._perform_sync, daemon=True).start()
        return {"success": True, "message": "Sync triggered"}

    def _cmd_status(self) -> dict[str, Any]:
        """Handle 'status' command - return sync status.

        Backward-compatible: shape matches pre-Phase-8.  The ``auth_valid``
        field probes the first authenticated provider (yt in practice).
        """
        last_sync_result = self.state.get("last_sync_result", {})

        # Auth status: use the yt provider if present, else report False
        yt = self.provider_registry.get("yt")
        if yt is not None:
            try:
                auth_valid, auth_error = yt.is_authenticated()
            except Exception:
                auth_valid, auth_error = False, "probe failed"
        else:
            auth_valid, auth_error = False, "yt provider not in registry"

        return {
            "success": True,
            "last_sync": self.state.get("last_sync"),
            "daemon_start_time": self.state.get("daemon_start_time"),
            "sync_in_progress": self._sync_in_progress,
            "playlists_synced": last_sync_result.get("playlists_synced", 0),
            "playlists_failed": last_sync_result.get("playlists_failed", 0),
            "tracks_added": last_sync_result.get("tracks_added", 0),
            "tracks_failed": last_sync_result.get("tracks_failed", 0),
            "errors": last_sync_result.get("errors", []),
            "last_sync_success": last_sync_result.get("success", False),
            "auth_valid": auth_valid,
            "auth_error": auth_error,
            "auto_auth_enabled": self._auto_auth_enabled,
            "last_auto_refresh": self.state.get("last_auto_refresh"),
            "auto_refresh_failures": self.state.get("auto_refresh_failures", 0),
        }

    def _cmd_list(self) -> dict[str, Any]:
        """Handle 'list' command - list playlists from all providers."""
        try:
            all_playlists: list[dict[str, Any]] = []
            for name, provider in self.provider_registry.items():
                try:
                    is_auth, _ = provider.is_authenticated()
                    if not is_auth:
                        continue
                    playlists = provider.list_playlists()
                    for p in playlists:
                        all_playlists.append(
                            {
                                "name": p.name,
                                "id": p.playlist_id,
                                "track_count": p.track_count,
                                "provider": name,
                            }
                        )
                except Exception as e:
                    logger.warning("Error listing playlists for %s: %s", name, e)
            return {"success": True, "playlists": all_playlists}
        except Exception as e:
            logger.error("Error listing playlists: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    def _cmd_quit(self) -> dict[str, Any]:
        """Handle 'quit' command - shutdown daemon."""
        logger.info("Shutdown requested via socket")
        threading.Thread(target=self.stop, daemon=True).start()
        return {"success": True, "message": "Shutting down"}

    def _cmd_provider_status(self) -> dict[str, Any]:
        """Return per-provider enabled/authenticated status."""
        statuses: dict[str, dict[str, bool]] = {}
        for name in ("yt", "tidal"):
            cfg_section = self.config.get(name, {})
            default = True if name == "yt" else False
            if isinstance(cfg_section, dict):
                enabled = cfg_section.get("enabled", default)
            else:
                enabled = default

            provider = self.provider_registry.get(name)
            if provider is not None:
                try:
                    is_auth, _ = provider.is_authenticated()
                except Exception:
                    is_auth = False
            else:
                is_auth = False
            statuses[name] = {"enabled": bool(enabled), "authenticated": bool(is_auth)}
        return {"success": True, "providers": statuses}

    def _cmd_radio(
        self,
        provider: str | None,
        track_id: str | None,
    ) -> dict[str, Any]:
        return playback.radio(self, provider, track_id)

    @staticmethod
    def _ensure_seed_first(
        prov: Provider,
        provider: str,
        seed_id: str,
        tracks: list[Any],
    ) -> list[Any]:
        """Return ``tracks`` with the seed track at index 0.

        - If seed already at index 0: unchanged.
        - If seed elsewhere: moved to index 0.
        - If seed missing: prepended via ``get_track_metadata`` (silently skipped
          on lookup failure so radio still plays).
        """
        from xmpd.providers.base import Track

        seed_idx = next(
            (i for i, t in enumerate(tracks) if t.track_id == seed_id),
            None,
        )
        if seed_idx == 0:
            return tracks
        if seed_idx is not None:
            return [tracks[seed_idx], *tracks[:seed_idx], *tracks[seed_idx + 1 :]]

        try:
            meta = prov.get_track_metadata(seed_id)
        except Exception as e:
            logger.warning(
                "Could not fetch seed metadata for %s/%s: %s",
                provider,
                seed_id,
                e,
            )
            return tracks
        if meta is None:
            logger.warning(
                "Seed track %s/%s metadata unavailable; not prepending",
                provider,
                seed_id,
            )
            return tracks
        return [Track(provider=provider, track_id=seed_id, metadata=meta), *tracks]

    def _get_liked_ids(self) -> set[str]:
        """Return liked track IDs by reading local favorites playlists."""
        now = time.time()
        if now - self._liked_ids_cache_time < self._liked_ids_cache_ttl:
            return self._liked_ids_cache

        from xmpd.sync_engine import DEFAULT_FAVORITES_NAMES

        prefix_map = _build_playlist_prefix(self.config)
        fmt = self.config.get("playlist_format", "m3u")
        if fmt == "xspf":
            music_dir = Path(self.config.get("mpd_music_directory", "~/Music")).expanduser()
            playlist_dir = music_dir / "_xmpd"
        else:
            playlist_dir = Path(
                self.config.get("mpd_playlist_directory", "~/.config/mpd/playlists")
            ).expanduser()
        favorites_names = {
            **DEFAULT_FAVORITES_NAMES,
            **self.config.get("favorites_playlist_name_per_provider", {}),
        }

        liked: set[str] = set()
        for pname in self.provider_registry:
            prefix = prefix_map.get(pname, f"{pname.upper()}: ")
            fav_name = favorites_names.get(pname, "Favorites")
            playlist_path = playlist_dir / f"{prefix}{fav_name}.{fmt}"
            if not playlist_path.exists():
                continue
            try:
                self._parse_liked_from_playlist(playlist_path, fmt, pname, liked)
            except Exception as e:
                logger.warning("Failed to parse liked playlist %s: %s", playlist_path, e)

        self._liked_ids_cache = liked
        self._liked_ids_cache_time = now
        logger.debug("Refreshed liked IDs cache: %d tracks", len(liked))
        return self._liked_ids_cache

    @staticmethod
    def _parse_liked_from_playlist(
        path: Path,
        fmt: str,
        pname: str,
        liked: set[str],
    ) -> None:
        text = path.read_text(encoding="utf-8")
        if fmt == "xspf":
            for match in re.finditer(r"/proxy/[^/]+/([^<\s]+)", text):
                liked.add(f"{pname}:{match.group(1)}")
        else:
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                line_match = re.search(r"/proxy/[^/]+/([^?\s]+)", line)
                if line_match:
                    liked.add(f"{pname}:{line_match.group(1)}")

    _TIDAL_QUALITY_LABELS: dict[str, str] = {
        "HI_RES_LOSSLESS": "HiRes",
        "LOSSLESS": "HiFi",
        "HIGH": "320k",
        "LOW": "96k",
    }

    def _quality_for_provider(self, provider_name: str) -> str:
        """Return fallback quality label when per-track data is unavailable."""
        if provider_name == "tidal":
            ceiling = self.config.get("tidal", {}).get("quality_ceiling", "LOSSLESS")
            return self._TIDAL_QUALITY_LABELS.get(ceiling, "HiFi")
        return "Lo"

    def _cmd_search_json(self, args: list[str]) -> dict[str, Any]:
        return queries.search_json(self, args)

    def _cmd_history_json(self, args: list[str]) -> dict[str, Any]:
        return queries.history_json(self, args)

    def _cmd_history_backfill(self, args: list[str]) -> dict[str, Any]:
        return queries.history_backfill(self, args)

    def _autodetect_mpd_log_path(self) -> str | None:
        """Walk candidate mpd.conf paths and extract the log_file directive.

        Returns:
            Expanded absolute path to the MPD log file, or None if not found.
        """
        from xmpd.history_backfill import MPDCONF_LOG_FILE_RE

        for candidate in _MPDCONF_CANDIDATES:
            conf_path = os.path.expanduser(candidate)
            if not os.path.isfile(conf_path):
                continue
            try:
                with open(conf_path, encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError:
                continue
            m = MPDCONF_LOG_FILE_RE.search(content)
            if m:
                return os.path.expanduser(m.group(1))
        return None

    def _ensure_mpd(self) -> MPDClientBase:
        """Reconnect to MPD if the connection was lost."""
        try:
            client = self.mpd_client._client
            if client is None:
                raise MPDConnectionError("MPD client not connected")
            client.ping()
        except Exception:
            logger.warning("MPD connection lost, reconnecting")
            self.mpd_client.connect()

        client = self.mpd_client._client
        if client is None:
            raise MPDConnectionError("MPD client unavailable after reconnect")
        return client

    def _cmd_play(self, provider: str, track_id: str | None) -> dict[str, Any]:
        return playback.play(self, provider, track_id)

    def _cmd_queue(self, provider: str, track_id: str | None) -> dict[str, Any]:
        return playback.queue(self, provider, track_id)

    def _cmd_like(self, provider: str | None, track_id: str | None) -> dict[str, Any]:
        return ratings.like(self, provider, track_id)

    def _cmd_dislike(self, provider: str | None, track_id: str | None) -> dict[str, Any]:
        return ratings.dislike(self, provider, track_id)

    def _cmd_like_toggle(self, provider: str | None, track_id: str | None) -> dict[str, Any]:
        return ratings.like_toggle(self, provider, track_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _format_duration(self, seconds: int) -> str:
        """Format duration in seconds as MM:SS."""
        if not seconds or seconds <= 0:
            return "Unknown"
        mins = seconds // 60
        secs = seconds % 60
        return f"{mins}:{secs:02d}"

    def _get_track_info(self, provider: str, track_id: str) -> dict[str, Any]:
        """Get track metadata via the provider registry.

        Returns title and artist as strings (falling back to "Unknown" if the
        provider lookup fails) plus the optional album/duration_seconds/art_url
        fields the provider supplies. The optional fields are needed so the
        proxy's FLAC patcher can read duration_seconds back out of TrackStore
        when streaming DASH; dropping them here is why many Tidal rows still
        show 0:00 in mpc.
        """
        prov = self.provider_registry.get(provider)
        if prov is not None:
            try:
                meta = prov.get_track_metadata(track_id)
                if meta is not None:
                    return {
                        "title": meta.title or "Unknown",
                        "artist": meta.artist or "Unknown Artist",
                        "album": meta.album,
                        "duration_seconds": meta.duration_seconds,
                        "art_url": meta.art_url,
                    }
            except Exception as e:
                logger.warning("Failed to get track info for %s/%s: %s", provider, track_id, e)
        return {
            "title": "Unknown",
            "artist": "Unknown Artist",
            "album": None,
            "duration_seconds": None,
            "art_url": None,
        }

    def _signal_handler(self, signum: int, frame: Any) -> None:
        """Handle signals.

        Args:
            signum: Signal number.
            frame: Current stack frame.
        """
        sig_name = signal.Signals(signum).name
        logger.info(f"Received signal: {sig_name}")

        if signum in (signal.SIGTERM, signal.SIGINT):
            # Signal shutdown - just set the flag and let main loop handle cleanup
            self._running = False
        elif signum == signal.SIGHUP:
            # Reload config and trigger sync
            logger.info("Reloading configuration...")
            try:
                self.config = load_config()
                logger.info("Configuration reloaded")
                # Trigger immediate sync
                threading.Thread(target=self._perform_sync, daemon=True).start()
            except Exception as e:
                logger.error(f"Error reloading config: {e}", exc_info=True)

    def _load_state(self) -> dict[str, Any]:
        """Load persisted state from sync_state.json.

        Returns:
            State dictionary.
        """
        default_state: dict[str, Any] = {
            "last_sync": None,
            "last_sync_result": {},
            "daemon_start_time": None,
            "last_auto_refresh": None,
            "auto_refresh_failures": 0,
        }

        if not self.state_file.exists():
            logger.info("No state file found, starting fresh")
            return dict(default_state)

        try:
            with open(self.state_file) as f:
                state: dict[str, Any] = json.load(f)
            # Ensure all default keys exist (for upgrades from older state files)
            for key, value in default_state.items():
                state.setdefault(key, value)
            logger.info("State loaded from %s", self.state_file)
            return state
        except Exception as e:
            logger.warning(f"Error loading state file: {e}, starting fresh")
            return dict(default_state)

    def _save_state(self) -> None:
        """Save state to sync_state.json."""
        try:
            with open(self.state_file, "w") as f:
                json.dump(self.state, f, indent=2)
            logger.debug(f"State saved to {self.state_file}")
        except Exception as e:
            logger.error(f"Error saving state: {e}")
