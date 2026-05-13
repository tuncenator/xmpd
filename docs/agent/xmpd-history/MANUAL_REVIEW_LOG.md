# Manual Review Log -- xmpd-history

> Tracks the user-driven review rounds that gate the final push to origin. Each round records the diff base, verdict, findings (if any), the fix commit(s) that resulted, and the new HEAD. Maintained by the manual-review-loop orchestrator per `MANUAL_REVIEW_HANDOFF.md`.

---

## Round 1

- **Date**: 2026-05-13
- **Start HEAD**: 9de4d1b23ee427d80a75cb80db38a72954a8a7f4
- **Diff base**: 18691caf59acfc42510a569c58169a0ade1f827c (merge-base main feature/xmpd-history)
- **Files in scope**: 64 files, +18,685 / -166. Of that, ~13K lines are agent docs; ~5.5K is code + tests. Surfaces: xmpd/history_store.py, xmpd/history_syncer.py, xmpd/history_reporter.py, xmpd/history_backfill.py, xmpd/daemon.py, xmpd/config.py, bin/xmpctl (extended), bin/xmpd-history (new), bin/xmpd-doctor (new), scripts/xmpd-history-receiver (new), plus the test suite and the agent docs tree.
- **Verdict**: FAIL_WITH_FINDINGS
- **Findings**:
  - **Finding 1**: important -- README.md / docs -- xmpd-history feature has no documentation explaining how to set up the WATCHTOWER SSH auth securely. A user enabling the feature defaults to using their personal SSH key, which (a) often fails because the systemd user-service environment lacks SSH_AUTH_SOCK, and (b) even when working, grants the daemon shell-level access on WATCHTOWER -- excessive privilege. The recommended setup (dedicated passphrase-less ed25519 key + a `WATCHTOWER_XMPD` SSH alias with `IdentityFile` + `IdentitiesOnly yes` + a wrapper script on WATCHTOWER that validates `SSH_ORIGINAL_COMMAND` against an allowlist + `authorized_keys` entry with `command="..."` + `restrict`) was discovered and validated end-to-end on three hosts (ARCHON, STORMTREE, VICAR) during this round. Suggested: add a "Setup -- Secure WATCHTOWER auth (recommended)" section to README and commit the wrapper script into the repo so users can scp it directly.
  - **Finding 2**: minor -- `bin/xmpd-doctor` + `tests/test_xmpd_doctor.py` -- when the syncer fails with `exit=255` + `Permission denied (publickey)` (the canonical "SSH auth broken" signature), the doctor reports the sync as failing but does not point the user at a fix. Suggested: detect the substring `Permission denied (publickey)` in recent journalctl output and emit a remediation hint pointing at the README's secure-auth section. Add a regression test using the existing journalctl-mocking pattern in `test_xmpd_doctor.py`.
- **Operational context (not findings, but the source of the findings)**:
  - At round start: history feature was not enabled on ARCHON. `~/.config/xmpd/history.db` did not exist; config.yaml had no `history:` block (default `enabled: false` per design).
  - During the round: enabled history on ARCHON, ran `xmpctl history-backfill` (2490 rows from `~/.mpd/mpd.log`), discovered bidir failing under systemd user service due to missing `SSH_AUTH_SOCK`, designed and validated a dedicated-key approach with `authorized_keys` lockdown, distributed the key to STORMTREE and VICAR via Syncthing+symlink+scp, retargeted all three hosts' xmpd configs to the new alias.
  - Validated end-to-end: ARCHON, STORMTREE, VICAR all successfully bidir with WATCHTOWER using the restricted key. The two findings are the documentation + UX gaps that surfaced during this exercise.
- **Fix commit(s)**:
  - 8bc52ad [Manual Review 1] fix: docs: add secure WATCHTOWER auth setup guide
  - c701079 [Manual Review 1] fix: xmpd-doctor: remediation hint for SSH publickey auth failures
- **End HEAD**: c701079f4a026af77a818a744de946b4426b89e5
- **Duration**: ~1.5 hours (single session, included operational rollout)

---
