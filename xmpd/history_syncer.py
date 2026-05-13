"""History syncer for xmpd.

Bidirectional sync between this host and the WATCHTOWER aggregator over SSH.
Streams unsynced rows up as NDJSON on stdin, reads peer rows down on stdout.
Tailscale precheck gates the SSH call; single-flight lock coalesces concurrent
invocations. Failures log and return cleanly so the next play event drives retry.
"""

import json
import logging
import socket
import subprocess
import threading
import time
from typing import Any

from xmpd.history_store import HistoryStore

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
TAILSCALE_TIMEOUT_SECONDS = 5
SSH_TIMEOUT_SECONDS = 30
RECEIVER_STDERR_TRUNCATE = 200

# Keys sent on the wire (excludes synced_at which is local-only).
_WIRE_KEYS = (
    "host",
    "local_id",
    "played_at",
    "provider",
    "track_id",
    "title",
    "artist",
    "album",
    "duration_seconds",
    "art_url",
    "quality",
    "play_seconds",
)


class HistorySyncer:
    """Bidirectional history sync between this host and WATCHTOWER.

    Each call to ``bidir_push`` or ``startup_nudge`` runs at most one SSH
    subprocess that streams unsynced rows up and reads peer rows down.
    Concurrent calls are coalesced by ``_inflight_lock``.
    """

    def __init__(
        self,
        *,
        history_store: HistoryStore,
        ssh_target: str,
        tailscale_hostname: str,
        bidir_batch: int,
        pull_batch: int,
    ) -> None:
        self._history_store = history_store
        self._ssh_target = ssh_target
        self._tailscale_hostname = tailscale_hostname
        self._bidir_batch = bidir_batch
        self._pull_batch = pull_batch
        self._inflight_lock = threading.Lock()
        self._self_host = socket.gethostname().upper()

    def bidir_push(self) -> None:
        """Push unsynced rows up and pull peer rows down in one SSH round-trip."""
        if not self._inflight_lock.acquire(blocking=False):
            logger.debug("history_syncer: bidir already in flight, coalescing")
            return
        try:
            if not self._tailscale_online():
                return
            unsynced_rows = self._history_store.unsynced_rows(limit=self._bidir_batch)
            cursor_str = self._history_store.get_sync_state("last_received_server_id")
            cursor = int(cursor_str) if cursor_str else 0
            self._run_bidir(unsynced_rows, cursor)
        finally:
            self._inflight_lock.release()

    def startup_nudge(self) -> None:
        """Pull-only bidir on daemon startup (empty stdin, still reads peer rows)."""
        if not self._inflight_lock.acquire(blocking=False):
            logger.debug("history_syncer: bidir already in flight, coalescing nudge")
            return
        try:
            if not self._tailscale_online():
                return
            cursor_str = self._history_store.get_sync_state("last_received_server_id")
            cursor = int(cursor_str) if cursor_str else 0
            self._run_bidir([], cursor)
        finally:
            self._inflight_lock.release()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _tailscale_online(self) -> bool:
        """Check whether the target peer is reachable via Tailscale."""
        try:
            proc = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True,
                timeout=TAILSCALE_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            logger.warning("history_syncer: tailscale binary not found, skipping bidir")
            return False
        except subprocess.TimeoutExpired:
            logger.warning(
                "history_syncer: tailscale precheck timed out after %ds",
                TAILSCALE_TIMEOUT_SECONDS,
            )
            return False

        if proc.returncode != 0:
            stderr_raw = proc.stderr.decode("utf-8", errors="replace")
            stderr_preview = stderr_raw[:RECEIVER_STDERR_TRUNCATE]
            logger.warning(
                "history_syncer: tailscale status exit=%d stderr=%s",
                proc.returncode,
                stderr_preview,
            )
            return False

        try:
            data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            logger.warning(
                "history_syncer: tailscale status returned unparseable JSON, skipping bidir"
            )
            return False

        peers = data.get("Peer", {}) or {}
        for peer_info in peers.values():
            if peer_info.get("HostName", "").lower() == self._tailscale_hostname.lower():
                if peer_info.get("Online") is True:
                    return True
                logger.warning(
                    "history_syncer: tailscale peer %s offline, skipping bidir",
                    self._tailscale_hostname,
                )
                return False

        logger.warning(
            "history_syncer: tailscale peer %s not found, skipping bidir",
            self._tailscale_hostname,
        )
        return False

    def _run_bidir(
        self,
        unsynced_rows: list[dict[str, Any]],
        cursor: int,
    ) -> None:
        """Execute the SSH subprocess for a single bidir round-trip."""
        cmd = [
            "ssh",
            self._ssh_target,
            "xmpd-history-receiver",
            "bidir",
            "--as",
            self._self_host,
            "--since",
            str(cursor),
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("history_syncer: ssh binary not found, cannot run bidir")
            return
        except OSError:
            logger.error("history_syncer: failed to spawn ssh subprocess", exc_info=True)
            return

        t0 = time.monotonic()

        # Write unsynced rows as NDJSON to stdin.
        try:
            assert proc.stdin is not None
            for row in unsynced_rows:
                wire_row = {k: row[k] for k in _WIRE_KEYS}
                proc.stdin.write(json.dumps(wire_row).encode("utf-8") + b"\n")
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            logger.warning(
                "history_syncer: stdin write failed (receiver died?), falling through",
                exc_info=True,
            )
            # Fall through to wait/error path.

        # Read stdout to EOF and parse peer rows.
        assert proc.stdout is not None
        stdout_bytes = proc.stdout.read()
        peer_rows: list[dict[str, Any]] = []
        for line in stdout_bytes.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(
                    "history_syncer: malformed peer row, skipping: %s",
                    line[:80],
                )
                continue
            if "server_id" not in row:
                logger.warning(
                    "history_syncer: peer row missing server_id, skipping: %s",
                    line[:80],
                )
                continue
            peer_rows.append(row)

        # Wait for subprocess completion.
        try:
            rc = proc.wait(timeout=SSH_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            logger.error(
                "history_syncer: ssh timed out after %ds, killed",
                SSH_TIMEOUT_SECONDS,
            )
            return

        if rc != 0:
            assert proc.stderr is not None
            stderr_preview = proc.stderr.read().decode("utf-8", errors="replace")[
                :RECEIVER_STDERR_TRUNCATE
            ]
            logger.error(
                "history_syncer: ssh exit=%d stderr=%s",
                rc,
                stderr_preview,
            )
            return

        # Post-success state updates.
        if peer_rows:
            inserted = self._history_store.insert_remote_rows(peer_rows)
            max_server_id = max(int(row["server_id"]) for row in peer_rows)
            if max_server_id > cursor:
                self._history_store.set_sync_state("last_received_server_id", str(max_server_id))
        else:
            inserted = 0

        if unsynced_rows:
            local_ids = [int(row["local_id"]) for row in unsynced_rows]
            self._history_store.mark_synced(local_ids)

        round_trip_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "history_syncer: bidir ok pushed=%d pulled=%d inserted=%d round_trip_ms=%d",
            len(unsynced_rows),
            len(peer_rows),
            inserted,
            round_trip_ms,
        )
