"""Sync daemon for xmpd - multi-provider music sync to MPD.

This module implements the XMPDaemon class which coordinates provider-agnostic
playlist syncing to MPD, with support for periodic auto-sync, history reporting,
rating dispatch, and an HTTP stream-redirect proxy.
"""

import asyncio
import json
import logging
import re
import signal
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from xmpd.config import get_config_dir, load_config
from xmpd.exceptions import MPDConnectionError
from xmpd.history_reporter import HistoryReporter
from xmpd.mpd_client import MPDClient
from xmpd.providers import build_registry
from xmpd.providers.base import Provider
from xmpd.rating import RatingAction, RatingManager, apply_to_provider
from xmpd.stream_proxy import StreamRedirectProxy, resolve_stream_cache_hours
from xmpd.stream_resolver import StreamResolver
from xmpd.sync_engine import SyncEngine
from xmpd.track_store import TrackStore

logger = logging.getLogger(__name__)


def _build_yt_config(config: dict[str, Any]) -> dict[str, Any]:
    """Synthesize a ``yt`` provider config section from legacy top-level keys.

    During the Phase 8-10 transition the user's ``config.yaml`` may still
    use the legacy flat shape (no ``yt:`` section).  This helper bridges
    the gap so ``build_registry`` always receives a well-formed dict.
    """
    if "yt" in config and isinstance(config["yt"], dict):
        # Already has the new shape; ensure ``enabled`` defaults to True
        section = dict(config["yt"])
        section.setdefault("enabled", True)
        return section
    # Legacy config: synthesize from top-level keys
    return {"enabled": True}


def _build_playlist_prefix(config: dict[str, Any]) -> dict[str, str]:
    """Normalise ``playlist_prefix`` into a per-provider dict.

    Phase 11 will make the config natively a dict.  Until then, a bare
    string is treated as the ``yt`` prefix.
    """
    raw = config.get("playlist_prefix", "YT: ")
    if isinstance(raw, dict):
        return raw
    return {"yt": str(raw)}


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
                        name, err or "no credentials", name,
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

        # Liked IDs cache for search-json like-state population
        self._liked_ids_cache: set[str] = set()
        self._liked_ids_cache_time: float = 0.0
        self._liked_ids_cache_ttl: float = 300.0  # 5 minutes

        # History reporting
        self._history_reporter: HistoryReporter | None = None
        self._history_thread: threading.Thread | None = None
        self._history_shutdown = threading.Event()
        history_config = self.config.get("history_reporting", {})
        if history_config.get("enabled", False) and self.track_store is not None:
            self._history_reporter = HistoryReporter(
                mpd_socket_path=self.config["mpd_socket_path"],
                provider_registry=self.provider_registry,
                track_store=self.track_store,
                proxy_config=self.proxy_config or {},
                min_play_seconds=history_config.get("min_play_seconds", 30),
            )
            logger.info(
                "History reporting enabled (min_play_seconds=%d)",
                history_config.get("min_play_seconds", 30),
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

                async with self.proxy_server:
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

    def _perform_sync(self) -> None:
        """Execute sync and update state."""
        # Skip if sync already in progress
        if self._sync_in_progress:
            logger.warning("Sync already in progress, skipping")
            return

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
                conn.sendall(
                    (json.dumps({"success": False, "error": str(e)}) + "\n").encode()
                )
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
        self, url: str,
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
            # Legacy fields retained for backward compat (auto-auth removed)
            "auto_auth_enabled": False,
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
        self, provider: str | None, track_id: str | None,
    ) -> dict[str, Any]:
        """Handle 'radio' command - generate radio playlist.

        Args:
            provider: Provider name, or None to infer from current track.
            track_id: Track ID, or None to infer from current track.
        """
        logger.info("Radio command: provider=%s track_id=%s", provider, track_id)

        try:
            # Infer provider + track_id from current MPD track if needed
            if track_id is None:
                try:
                    current = self.mpd_client.currentsong()
                except Exception as e:
                    logger.error("Failed to get current song from MPD: %s", e)
                    return {"success": False, "error": "Failed to get current track"}

                if not current:
                    return {"success": False, "error": "No track currently playing"}

                file_url = current.get("file", "")
                provider, track_id = self._extract_provider_and_track(file_url)

                if not provider or not track_id:
                    return {"success": False, "error": "Current track is not a provider track"}

                logger.info(
                    "Inferred from current track: provider=%s track_id=%s", provider, track_id,
                )

            # Default provider to yt for backward compat
            if provider is None:
                provider = "yt"

            if provider not in self.provider_registry:
                return {"success": False, "error": f"Unknown provider: {provider}"}

            prov = self.provider_registry[provider]
            is_auth, err = prov.is_authenticated()
            if not is_auth:
                return {"success": False, "error": f"{provider} not authenticated: {err}"}

            # Fetch radio tracks via Provider Protocol
            radio_tracks = prov.get_radio(
                track_id, limit=self.config.get("radio_playlist_limit", 25),
            )
            if not radio_tracks:
                return {"success": False, "error": "No tracks found in radio playlist"}

            logger.info("Fetched %d radio tracks from %s", len(radio_tracks), provider)

            # Build TrackWithMetadata objects for MPD playlist creation
            from xmpd.mpd_client import TrackWithMetadata

            track_objects: list[TrackWithMetadata] = []
            for t in radio_tracks:
                # Persist to TrackStore for on-demand proxy resolution
                if self.track_store:
                    try:
                        self.track_store.add_track(
                            provider=t.provider,
                            track_id=t.track_id,
                            stream_url=None,
                            title=t.metadata.title,
                            artist=t.metadata.artist,
                        )
                    except Exception as e:
                        logger.warning("Failed to save track %s: %s", t.track_id, e)

                track_objects.append(
                    TrackWithMetadata(
                        url="",
                        title=t.metadata.title,
                        artist=t.metadata.artist or "Unknown Artist",
                        video_id=t.track_id,
                        duration_seconds=t.metadata.duration_seconds,
                        provider=t.provider,
                    )
                )

            if not track_objects:
                return {"success": False, "error": "No valid tracks to add to playlist"}

            # Build liked set for like indicator
            like_indicator = self.config.get(
                "like_indicator", {"enabled": False, "tag": "+1", "alignment": "right"},
            )
            liked_video_ids: set[str] = set()
            if like_indicator.get("enabled", False):
                try:
                    favs = prov.get_favorites()
                    liked_video_ids = {f.track_id for f in favs}
                except Exception as e:
                    logger.warning("Failed to fetch favorites for like indicator: %s", e)

            # Create MPD playlist
            prefix_map = _build_playlist_prefix(self.config)
            prefix = prefix_map.get(provider, "YT: " if provider == "yt" else "TD: ")
            playlist_name = f"{prefix}Radio"
            logger.info("Creating playlist '%s' with %d tracks", playlist_name, len(track_objects))

            self.mpd_client.create_or_replace_playlist(
                name=playlist_name,
                tracks=track_objects,
                proxy_config=self.proxy_config,
                playlist_format=self.config.get("playlist_format", "m3u"),
                mpd_music_directory=self.config.get("mpd_music_directory"),
                liked_video_ids=liked_video_ids,
                like_indicator=like_indicator,
            )

            return {
                "success": True,
                "message": f"Radio playlist created: {len(track_objects)} tracks",
                "tracks": len(track_objects),
                "playlist": playlist_name,
            }

        except Exception as e:
            logger.error("Radio generation failed: %s", e)
            return {"success": False, "error": f"Radio generation failed: {e}"}

    def _get_liked_ids(self) -> set[str]:
        """Return the cached set of liked track IDs across all providers.

        Uses provider_registry.get_favorites() to collect liked IDs.
        Results are cached for 5 minutes to avoid repeated API calls.

        Returns:
            Set of liked track IDs (compound: "provider:track_id"). Empty set
            if fetch fails.
        """
        now = time.time()
        if now - self._liked_ids_cache_time < self._liked_ids_cache_ttl:
            return self._liked_ids_cache

        liked: set[str] = set()
        for pname, prov in self.provider_registry.items():
            try:
                is_auth, _ = prov.is_authenticated()
                if not is_auth:
                    continue
                favorites = prov.get_favorites()
                for track in favorites:
                    liked.add(f"{pname}:{track.track_id}")
            except Exception as e:
                logger.warning("Failed to fetch favorites for %s: %s", pname, e)

        self._liked_ids_cache = liked
        self._liked_ids_cache_time = now
        logger.debug("Refreshed liked IDs cache: %d tracks", len(liked))

        return self._liked_ids_cache

    _TIDAL_QUALITY_LABELS: dict[str, str] = {
        "HI_RES_LOSSLESS": "HiRes",
        "LOSSLESS": "HiFi",
        "HIGH": "320k",
        "LOW": "96k",
    }

    def _quality_for_provider(self, provider_name: str) -> str:
        """Return fallback quality label when per-track data is unavailable."""
        if provider_name == "tidal":
            ceiling = self.config.get("tidal", {}).get(
                "quality_ceiling", "LOSSLESS"
            )
            return self._TIDAL_QUALITY_LABELS.get(ceiling, "HiFi")
        return "Lo"

    def _cmd_search_json(self, args: list[str]) -> dict[str, Any]:
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
            query, provider_filter, limit,
        )

        if not query:
            return {"success": False, "error": "Empty search query"}

        # Determine which providers to search
        if provider_filter and provider_filter != "all":
            if provider_filter not in self.provider_registry:
                return {"success": False, "error": f"Unknown provider: {provider_filter}"}
            targets = {provider_filter: self.provider_registry[provider_filter]}
        else:
            targets = self.provider_registry

        # Get liked IDs for like-state population
        liked_ids = self._get_liked_ids()

        results: list[dict[str, Any]] = []
        for pname, prov in targets.items():
            try:
                is_auth, _ = prov.is_authenticated()
                if not is_auth:
                    continue
                search_results = prov.search(query, limit=limit)
            except Exception as e:
                logger.warning("search-json: search failed for %s: %s", pname, e)
                continue

            fallback_quality = self._quality_for_provider(pname)
            for track in search_results:
                duration_secs = track.metadata.duration_seconds or 0
                quality = track.metadata.quality or fallback_quality
                results.append(
                    {
                        "provider": track.provider,
                        "track_id": track.track_id,
                        "title": track.metadata.title,
                        "artist": track.metadata.artist or "Unknown Artist",
                        "album": track.metadata.album or None,
                        "duration": self._format_duration(duration_secs),
                        "duration_seconds": duration_secs,
                        "quality": quality,
                        "liked": f"{track.provider}:{track.track_id}" in liked_ids
                        if track.track_id else None,
                    }
                )

        logger.info("search-json: returning %d results for %r", len(results), query)
        return {"success": True, "results": results}

    def _ensure_mpd(self) -> None:
        """Reconnect to MPD if the connection was lost."""
        try:
            self.mpd_client._client.ping()
        except Exception:
            logger.warning("MPD connection lost, reconnecting")
            self.mpd_client.connect()

    def _cmd_play(self, provider: str, track_id: str | None) -> dict[str, Any]:
        """Handle 'play' command - play track immediately.

        Args:
            provider: Provider canonical name (e.g. 'yt').
            track_id: Track identifier.
        """
        logger.info("Play command: provider=%s track_id=%s", provider, track_id)

        try:
            if not track_id:
                return {"success": False, "error": "Missing track ID"}

            # Get track metadata via provider
            track_info = self._get_track_info(provider, track_id)

            # Register in TrackStore so stream proxy can resolve the track
            if self.track_store:
                try:
                    self.track_store.add_track(
                        provider=provider,
                        track_id=track_id,
                        stream_url=None,
                        title=track_info.get("title", "Unknown"),
                        artist=track_info.get("artist", None),
                    )
                except Exception:
                    logger.warning("Failed to register track in store: %s/%s", provider, track_id)

            # Build proxy URL
            proxy_port = (self.proxy_config or {}).get("port", 8080)
            proxy_url = f"http://localhost:{proxy_port}/proxy/{provider}/{track_id}"

            # Clear queue, add track with metadata, start playback
            logger.info("Playing: %s - %s", track_info["title"], track_info["artist"])
            self._ensure_mpd()
            self.mpd_client._client.clear()
            song_id = self.mpd_client._client.addid(proxy_url)
            self.mpd_client._client.addtagid(song_id, "Title", track_info["title"])
            self.mpd_client._client.addtagid(song_id, "Artist", track_info["artist"])
            self.mpd_client._client.play()

            return {
                "success": True,
                "message": f"Now playing: {track_info['title']} - {track_info['artist']}",
                "title": track_info["title"],
                "artist": track_info["artist"],
            }

        except Exception as e:
            logger.error("Play command failed: %s", e)
            return {"success": False, "error": f"Play failed: {e}"}

    def _cmd_queue(self, provider: str, track_id: str | None) -> dict[str, Any]:
        """Handle 'queue' command - add track to MPD queue.

        Args:
            provider: Provider canonical name.
            track_id: Track identifier.
        """
        logger.info("Queue command: provider=%s track_id=%s", provider, track_id)

        try:
            if not track_id:
                return {"success": False, "error": "Missing track ID"}

            track_info = self._get_track_info(provider, track_id)

            # Register in TrackStore so stream proxy can resolve the track
            if self.track_store:
                try:
                    self.track_store.add_track(
                        provider=provider,
                        track_id=track_id,
                        stream_url=None,
                        title=track_info.get("title", "Unknown"),
                        artist=track_info.get("artist", None),
                    )
                except Exception:
                    logger.warning("Failed to register track in store: %s/%s", provider, track_id)

            proxy_port = (self.proxy_config or {}).get("port", 8080)
            proxy_url = f"http://localhost:{proxy_port}/proxy/{provider}/{track_id}"

            logger.info("Adding to queue: %s - %s", track_info["title"], track_info["artist"])
            self._ensure_mpd()
            song_id = self.mpd_client._client.addid(proxy_url)
            self.mpd_client._client.addtagid(song_id, "Title", track_info["title"])
            self.mpd_client._client.addtagid(song_id, "Artist", track_info["artist"])

            return {
                "success": True,
                "message": f"Added to queue: {track_info['title']} - {track_info['artist']}",
                "title": track_info["title"],
                "artist": track_info["artist"],
            }

        except Exception as e:
            logger.error("Queue command failed: %s", e)
            return {"success": False, "error": f"Queue failed: {e}"}

    def _cmd_like(self, provider: str | None, track_id: str | None) -> dict[str, Any]:
        """Handle 'like' command."""
        if not provider or not track_id:
            return {"success": False, "error": "Usage: like <provider> <track_id>"}
        if provider not in self.provider_registry:
            return {"success": False, "error": f"Unknown provider: {provider}"}

        prov = self.provider_registry[provider]
        try:
            is_auth, err = prov.is_authenticated()
        except Exception as exc:
            return {"success": False, "error": f"{provider} auth probe failed: {exc}"}
        if not is_auth:
            return {"success": False, "error": f"{provider} not authenticated: {err}"}

        try:
            raw_state = prov.get_like_state(track_id)
            from xmpd.rating import RatingState
            state_map = {
                "LIKED": RatingState.LIKED,
                "DISLIKED": RatingState.DISLIKED,
                "NEUTRAL": RatingState.NEUTRAL,
            }
            current = state_map.get(raw_state, RatingState.NEUTRAL)
            transition = self._rating_manager.apply_action(current, RatingAction.LIKE)
            apply_to_provider(prov, transition, track_id)
            # Invalidate favorites cache so next search-json reflects new state
            self._liked_ids_cache_time = 0.0
            return {
                "success": True,
                "message": transition.user_message,
                "new_state": transition.new_state.value,
            }
        except Exception as e:
            logger.error("Like failed: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    def _cmd_dislike(self, provider: str | None, track_id: str | None) -> dict[str, Any]:
        """Handle 'dislike' command."""
        if not provider or not track_id:
            return {"success": False, "error": "Usage: dislike <provider> <track_id>"}
        if provider not in self.provider_registry:
            return {"success": False, "error": f"Unknown provider: {provider}"}

        prov = self.provider_registry[provider]
        try:
            is_auth, err = prov.is_authenticated()
        except Exception as exc:
            return {"success": False, "error": f"{provider} auth probe failed: {exc}"}
        if not is_auth:
            return {"success": False, "error": f"{provider} not authenticated: {err}"}

        try:
            raw_state = prov.get_like_state(track_id)
            from xmpd.rating import RatingState
            state_map = {
                "LIKED": RatingState.LIKED,
                "DISLIKED": RatingState.DISLIKED,
                "NEUTRAL": RatingState.NEUTRAL,
            }
            current = state_map.get(raw_state, RatingState.NEUTRAL)
            transition = self._rating_manager.apply_action(current, RatingAction.DISLIKE)
            apply_to_provider(prov, transition, track_id)
            # Invalidate favorites cache so next search-json reflects new state
            self._liked_ids_cache_time = 0.0
            return {
                "success": True,
                "message": transition.user_message,
                "new_state": transition.new_state.value,
            }
        except Exception as e:
            logger.error("Dislike failed: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    def _cmd_like_toggle(self, provider: str | None, track_id: str | None) -> dict[str, Any]:
        """Handle 'like-toggle' command - toggle like state for arbitrary track.

        Unlike 'like', which toggles based on current provider state, this
        command is explicitly for the search interface: it reads current like
        state, applies the LIKE toggle action, updates the provider, then
        invalidates the favorites cache so the next search-json reflects the
        change.

        Args:
            provider: Provider canonical name (e.g. 'yt', 'tidal').
            track_id: Track identifier.

        Returns:
            Response dict with 'success', 'message', 'new_state', 'liked' (bool).
        """
        if not provider or not track_id:
            return {"success": False, "error": "Usage: like-toggle <provider> <track_id>"}
        if provider not in self.provider_registry:
            return {"success": False, "error": f"Unknown provider: {provider}"}

        prov = self.provider_registry[provider]
        try:
            is_auth, err = prov.is_authenticated()
        except Exception as exc:
            return {"success": False, "error": f"{provider} auth probe failed: {exc}"}
        if not is_auth:
            return {"success": False, "error": f"{provider} not authenticated: {err}"}

        try:
            raw_state = prov.get_like_state(track_id)
            from xmpd.rating import RatingState
            state_map = {
                "LIKED": RatingState.LIKED,
                "DISLIKED": RatingState.DISLIKED,
                "NEUTRAL": RatingState.NEUTRAL,
            }
            current = state_map.get(raw_state, RatingState.NEUTRAL)
            transition = self._rating_manager.apply_action(current, RatingAction.LIKE)
            apply_to_provider(prov, transition, track_id)

            # Invalidate the favorites cache so next search-json reflects new state
            self._liked_ids_cache_time = 0.0
            logger.debug(
                "like-toggle: invalidated favorites cache for %s:%s (new_state=%s)",
                provider, track_id, transition.new_state.value,
            )

            now_liked = transition.new_state == RatingState.LIKED

            # Patch on-disk playlists and live MPD queue immediately
            try:
                from xmpd.playlist_patcher import patch_mpd_queue, patch_playlist_files
                from xmpd.sync_engine import DEFAULT_FAVORITES_NAMES

                proxy_port = (self.proxy_config or {}).get("port", 8080)
                proxy_url = f"http://localhost:{proxy_port}/proxy/{provider}/{track_id}"

                like_indicator = self.config.get("like_indicator", {})
                if like_indicator.get("enabled", False):
                    playlist_dir = Path(
                        self.config.get("mpd_playlist_directory", "~/.config/mpd/playlists")
                    ).expanduser()
                    xspf_dir = None
                    if self.config.get("playlist_format") == "xspf":
                        music_dir = self.config.get("mpd_music_directory", "~/Music")
                        xspf_dir = Path(music_dir).expanduser() / "_xmpd"

                    prefix_map = self.config.get(
                        "playlist_prefix", {"yt": "YT: ", "tidal": "TD: "}
                    )
                    fav_names_cfg = self.config.get(
                        "favorites_playlist_name_per_provider", {}
                    )
                    fav_names = {**DEFAULT_FAVORITES_NAMES, **fav_names_cfg}
                    favorites_set = set()
                    for prov_name, fav_name in fav_names.items():
                        prov_prefix = prefix_map.get(prov_name, "")
                        favorites_set.add(f"{prov_prefix}{fav_name}")

                    patch_playlist_files(
                        proxy_url, now_liked, playlist_dir, xspf_dir,
                        like_indicator, favorites_set,
                    )

                    if self.mpd_client and self.mpd_client._client:
                        self._ensure_mpd()
                        track_info = self._get_track_info(provider, track_id)
                        base_title = (
                            f"{track_info.get('artist', 'Unknown')} - "
                            f"{track_info.get('title', 'Unknown')}"
                        )
                        patch_mpd_queue(
                            self.mpd_client._client, proxy_url, base_title,
                            now_liked, like_indicator,
                        )
            except Exception as patch_exc:
                logger.warning("Like-toggle playlist patching failed: %s", patch_exc)

            return {
                "success": True,
                "message": transition.user_message,
                "new_state": transition.new_state.value,
                "liked": now_liked,
            }
        except Exception as e:
            logger.error("Like-toggle failed: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

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

    def _get_track_info(self, provider: str, track_id: str) -> dict[str, str]:
        """Get track metadata via the provider registry.

        Falls back to "Unknown" if the provider or metadata lookup fails.
        """
        prov = self.provider_registry.get(provider)
        if prov is not None:
            try:
                meta = prov.get_track_metadata(track_id)
                if meta is not None:
                    return {
                        "title": meta.title or "Unknown",
                        "artist": meta.artist or "Unknown Artist",
                    }
            except Exception as e:
                logger.warning("Failed to get track info for %s/%s: %s", provider, track_id, e)
        return {"title": "Unknown", "artist": "Unknown Artist"}

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
