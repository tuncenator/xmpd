"""History syncer for xmpd.

Stub for Phase 2 wiring. The real bidir_push / startup_nudge bodies land in
Phase 3 (Tailscale precheck, ssh subprocess, NDJSON wire format, single-flight
lock). Phase 2 only needs the class to exist so HistoryReporter and XMPDaemon
can import it and tests can assert that bidir_push is submitted to the executor.
"""

import logging

from xmpd.history_store import HistoryStore

logger = logging.getLogger(__name__)


class HistorySyncer:
    """Bidirectional history sync between this host and WATCHTOWER.

    Phase 2: stub. Methods log and return.
    Phase 3: real implementation.
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

    def bidir_push(self) -> None:
        """Push unsynced rows up + pull peer rows down. STUB in Phase 2."""
        logger.info("history_syncer stub: bidir_push called")

    def startup_nudge(self) -> None:
        """Trigger one bidir round-trip on daemon startup. STUB in Phase 2."""
        logger.info("history_syncer stub: startup_nudge called")
