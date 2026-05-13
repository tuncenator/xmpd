# Phase 8: Integration Testing on Test Peers - Summary

**Date Completed:** 2026-05-13
**Actual Token Usage:** ~90k tokens

---

## Objective

Run all five User Loops from FUNCTIONAL_QA_STRATEGY.md end-to-end against real daemons on STORMTREE and VICAR plus the receiver on WATCHTOWER. Fix bugs found with surgical commits. Produce INTEGRATION_TEST_REPORT.md.

---

## Work Completed

### Top-level outcome

5 loops executed. 3 passed, 2 failed-and-fixed, 0 escalated. See `docs/agent/xmpd-history/INTEGRATION_TEST_REPORT.md` for byte-for-byte evidence.

### Bug fixes (4 commits)

1. **51c40f8** `xmpd/history_syncer.py`: add `-F ~/.ssh/config` to SSH command. OpenSSH 10.2 rejects bad-permissions system config includes inside systemd user services.
2. **1b91ef2** `bin/xmpd-history`: remove `< /dev/null` stdin redirect. fzf 0.70.0 exits immediately with `/dev/null` stdin.
3. **aadb2d9** `bin/xmpd-history`: change `--tab-stop=8` to `--tabstop=8`. fzf 0.70.0 rejects the hyphenated form.
4. **e838496** `bin/xmpd-doctor`: add `-F ~/.ssh/config` to all SSH invocations. Same root cause as fix 1.

### Files Created

- `docs/agent/xmpd-history/INTEGRATION_TEST_REPORT.md`
- `docs/agent/xmpd-history/summaries/PHASE_08_SUMMARY.md`

### Files Modified

- `xmpd/history_syncer.py` - added `os` import and `-F user_ssh_config` to SSH command
- `bin/xmpd-history` - removed `/dev/null` stdin redirect, fixed `--tabstop` flag
- `bin/xmpd-doctor` - added `SSH_CONFIG` constant and `-F` to all SSH invocations
- `tests/test_history_syncer.py` - regression test + updated command assertion
- `tests/test_xmpd_history.py` - two regression tests (stdin, tabstop)
- `tests/test_xmpd_doctor.py` - regression test for SSH `-F` flag

---

## Completion Criteria Status

- [x] INTEGRATION_TEST_REPORT.md exists with all five loop sections
- [x] Each section contains pre-conditions, commands, observed outputs, verdict
- [x] Top-level summary table filled in
- [x] All risky-action gates honored (track choice, iptables technique, observation mode, DB rename)
- [x] Bug fixes follow protocol (regression test first, single-file scope)
- [x] No fix commit touches ARCHON state
- [x] All offline simulations rolled back; final state on both peers is steady-state
- [x] All bug fixes have passing tests: 103 history tests pass
- [x] Phase summary references the report

---

## Testing

### Tests Written

- `tests/test_history_syncer.py::TestSSHConfigBypass::test_bidir_ssh_command_uses_user_config_only`
- `tests/test_xmpd_history.py::TestFzfStdinNotDevNull::test_wrapper_does_not_redirect_fzf_stdin_from_devnull`
- `tests/test_xmpd_history.py::TestFzfTabstopFlag::test_wrapper_uses_tabstop_without_hyphen`
- `tests/test_xmpd_doctor.py::test_ssh_commands_use_user_config`

### Test Results

103 history-related tests pass. No regressions.

---

## Codebase Context Updates

- `xmpd/history_syncer.py` now passes `-F ~/.ssh/config` to all SSH subprocesses to bypass system config.
- `bin/xmpd-history` uses `--tabstop` (no hyphen) for fzf 0.70.0 compatibility; does not redirect fzf stdin from `/dev/null`.
- `bin/xmpd-doctor` passes `-F $HOME/.ssh/config` to all SSH invocations via `SSH_CONFIG` constant.
- Infrastructure: peers need `history:` + `history_reporting:` config sections, `SSH_AUTH_SOCK` in systemd user env, and WATCHTOWER `~/.bashrc` PATH for non-interactive SSH.

---

## Known Issues / Technical Debt

- WATCHTOWER `~/.bashrc` PATH modification is a manual infrastructure step not codified anywhere.
- Systemd drop-in for SSH access (`ProtectSystem=no`) loosens security; a tighter solution would add only the needed `ReadPaths`.
- Backfill batch drain: 2464 rows inserted but only 500 synced per bidir call (batch limit). Remaining 1964 will drain incrementally via play events. No mechanism for bulk drain without repeated plays or daemon restarts.
- Loop E yellow run: iptables port-22 block triggers red (exit 1) not yellow (exit 2) because tailscale still reports WATCHTOWER online. A true yellow scenario requires tailscale-level offline status.

---

## Next Steps

**Next Phase:** None. This is the final phase (8/8). Feature is shipped.

**Recommended Actions:**
1. Enable `history:` config on ARCHON to complete the three-host topology.
2. Consider a bulk-drain mechanism (e.g. loop bidir until unsynced=0) for large backfills.

---

**Phase Status:** COMPLETE
