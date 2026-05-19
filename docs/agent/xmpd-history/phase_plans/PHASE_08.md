# Phase 8: Integration Testing on Test Peers

**Feature**: xmpd-history
**Estimated Context Budget**: ~70k tokens

**Difficulty**: hard
**Visual**: no
**Functional**: yes

**Execution Mode**: sequential
**Batch**: 6

---

## Objective

Run all five User Loops from `FUNCTIONAL_QA_STRATEGY.md` end-to-end against the real running daemons on `[TEST_HOST_1]` and `[TEST_HOST_2]` plus the receiver on WATCHTOWER. Capture observed state byte-for-byte. Fix any bugs found with one surgical commit + one regression test per bug. Produce `INTEGRATION_TEST_REPORT.md` with per-loop sections.

This phase is the live cross-host validation gate. The loops ARE the deliverables. No new feature code lands here -- only the report and (if needed) small surgical fixes.

---

## Deliverables

1. **`docs/agent/xmpd-history/INTEGRATION_TEST_REPORT.md`** (NEW, OWNED) -- one section per loop (A-E), each containing:
   - Pre-conditions (what was set up before running the loop).
   - Commands run, full SSH heredocs included.
   - Observed stdout / journalctl / DB state pasted byte-for-byte.
   - Pass/fail verdict per check.
   - References to any bug-fix commits made during the loop.
   - Top-level summary line: `5 loops, M passed, K failed-and-fixed, J escalated`.
2. **Bug-fix commits** (zero or more, OWNED only when triggered by a loop failure) -- each is:
   - One bug -> one fix -> one regression test -> one commit.
   - Fix scope: ONE file or one tightly-scoped change.
   - Add a `# regression for Loop X failure: <one-line description>` comment near the fix and at the top of the regression test.
   - Regression test lives under the appropriate existing `tests/test_<module>.py`.
3. **No refactoring**, no architectural changes. If a loop reveals an architectural issue: STOP, document under `INTEGRATION_TEST_REPORT.md` -> `## Escalation`, return to user.

---

## Detailed Requirements

### File ownership and scope

This phase OWNS:
- `docs/agent/xmpd-history/INTEGRATION_TEST_REPORT.md` (new file).
- Any fix commits scoped to a single module (e.g. `xmpd/history_syncer.py` plus `tests/test_history_syncer.py`).

This phase MUST NOT touch:
- `xmpd/history_store.py` for refactoring (only single-line surgical fixes if a loop demands it).
- `xmpd/daemon.py` wiring (Phase 2).
- `bin/xmpctl` subcommand structure (Phases 5/6).
- `bin/xmpd-history` or `bin/xmpd-doctor` structure (Phases 5/7).
- `scripts/xmpd-history-receiver` (Phase 4).

If a fix would touch more than one module: STOP. Document and escalate.

### Multi-host coordination protocol (MANDATORY for every remote action)

1. **SSH transport**: ALWAYS use the heredoc pattern from QUICKSTART. Never `ssh HOST "cmd"`.
   ```bash
   ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   <commands>
   EOF
   ```
2. **Code replication wait**: BEFORE every `systemctl --user restart xmpd` on a peer, confirm Syncthing finished replicating. Use the helper `scripts/spark-restart-peer.sh <peer>` -- it computes `LOCAL_HEAD = git rev-parse HEAD`, polls the peer's HEAD via heredoc until match (60s timeout default), then restarts and dumps `is-active` + last 20 journalctl lines.
3. **Never restart `xmpd` on `[LIVE_HOST]`**. The user is actively listening. The phase has zero deliverables on `[LIVE_HOST]`.
4. **Never spawn `python -m xmpd` or `uv run python -m xmpd` on `[LIVE_HOST]`** -- port collision with the live daemon.
5. **WATCHTOWER inspection is read-only by default.** Reading aggregator DB rows via `sqlite3 ~/xmpd-history/history.db "SELECT ..."` is fine. Do not delete or alter aggregator rows except as part of a documented offline-simulation rollback.

### Risky-action gating: ASK USER before any of these

The phase plan deliberately leaves several decisions to the coder at execution time, gated by an explicit user prompt. Before performing any of the following, ASK USER and wait for confirmation:

- **Choosing a track to play on `[TEST_HOST_1]` for Loop A**. The user may not want surprising audio. Default suggestion: ask "Loop A needs to play a track on `[TEST_HOST_1]` for >=30s. Use the existing MPD queue on `[TEST_HOST_1]` (run `mpc play` if a queue is loaded), or queue a specific track first via `mpc add <URI>` then `mpc play`? If queue, which track?".
- **Choosing the offline-simulation technique for Loops B and E**. Two options:
  - (a) `systemctl --user stop tailscaled` on `[TEST_HOST_1]`. Risk: if `[LIVE_HOST]` only reaches `[TEST_HOST_1]` via tailnet, the planner LOSES SSH ACCESS until tailscaled is restarted. Safe only if both hosts share a LAN subnet and `~/.ssh/config` resolves to a non-tailnet IP for `[TEST_HOST_1]`.
  - (b) `iptables -I OUTPUT -d <WATCHTOWER_TAILSCALE_IP> -p tcp --dport 22 -j REJECT` on `[TEST_HOST_1]`. Tailscale stays UP, only the ssh-to-watchtower path is broken. Safer. Requires looking up WATCHTOWER's tailnet IP first via `tailscale status | grep WATCHTOWER` on `[TEST_HOST_1]`. Rollback: `iptables -D OUTPUT -d <ip> -p tcp --dport 22 -j REJECT`. May require `sudo` -- if so, ASK USER who should run it.
  - **Default recommendation**: option (b) per-host route block. ALWAYS ASK USER which to use before invoking either.
- **Loop C interactive observation mode**. ASK USER: "Loop C needs interactive fzf. Three options: (1) you run `xmpd-history` in your own ssh shell on `[TEST_HOST_1]` and report back what fzf shows; (2) I open a tmux session on `[TEST_HOST_1]` you can attach to; (3) I capture an ASCII transcript via `script -c xmpd-history /tmp/typescript.txt` and paste it. Which?"
- **Renaming `~/.config/xmpd/history.db` for Loop E red scenario**. ASK USER: "Loop E red scenario needs the local DB to be temporarily missing on `[TEST_HOST_1]`. I'll move it to `~/.config/xmpd/history.db.phase8_bak` and restore it after the doctor run. The xmpd daemon may notice the missing file -- expected behavior. OK to proceed?"

### Loop execution order

Run loops in order A, B, C, D, E. Do NOT proceed to the next loop until the previous one is fully captured in the report. If a loop reveals a bug, fix it (with a regression test commit) BEFORE continuing -- subsequent loops may depend on the fixed behavior.

---

### Loop A: play roundtrip

Cross-reference: FUNCTIONAL_QA_STRATEGY.md User Loop A.

Surfaces touched: HistoryReporter side effect, HistorySyncer, receiver `bidir`, HistoryStore reads (write side on `[TEST_HOST_1]`, read side on both peers + WATCHTOWER aggregator).

Steps:

1. Confirm both peers are at HEAD == `[LIVE_HOST]` HEAD via the helper for both peers in parallel:
   ```bash
   scripts/spark-restart-peer.sh [TEST_HOST_1] xmpd
   scripts/spark-restart-peer.sh [TEST_HOST_2] xmpd
   ```
   Capture output of both. Both must end with `is-active: active`.
2. ASK USER for the track choice (see Risky-action gating above). Once confirmed:
   - If user chose existing queue: `ssh [TEST_HOST_1] <<'EOF' ... ; mpc play ; EOF`.
   - If user chose new track: `ssh [TEST_HOST_1] <<'EOF' ... ; mpc add <URI> ; mpc play ; EOF`.
   Capture `mpc status` immediately after to confirm playback started.
3. Wait at least 35 seconds (the 30s gate plus margin).
4. Read `[TEST_HOST_1]` local DB:
   ```bash
   ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   sqlite3 -header -column ~/.config/xmpd/history.db "SELECT host, local_id, played_at, provider, track_id, title, artist, synced_at FROM plays ORDER BY local_id DESC LIMIT 1;"
   EOF
   ```
   Assert: row exists, `host = '[TEST_HOST_1]'`, `synced_at` is non-NULL within ~5s of insertion (rerun the query if first read shows NULL; capture both reads in the report).
5. Read WATCHTOWER aggregator:
   ```bash
   ssh WATCHTOWER <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   sqlite3 -header -column ~/xmpd-history/history.db "SELECT server_id, host, local_id, played_at, received_at FROM plays WHERE host='[TEST_HOST_1]' ORDER BY server_id DESC LIMIT 1;"
   EOF
   ```
   Assert: row present, `server_id` increased over any prior baseline, `(host, local_id)` matches step 4, `received_at` populated.
6. Restart `xmpd` on `[TEST_HOST_2]` to trigger `startup_nudge`:
   ```bash
   scripts/spark-restart-peer.sh [TEST_HOST_2] xmpd
   ```
7. Read `[TEST_HOST_2]` local DB:
   ```bash
   ssh [TEST_HOST_2] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   sqlite3 -header -column ~/.config/xmpd/history.db "SELECT host, local_id, played_at FROM plays WHERE host='[TEST_HOST_1]' ORDER BY local_id DESC LIMIT 1;"
   EOF
   ```
   Assert: `[TEST_HOST_1]`'s row from step 4 appears with originating host preserved.
8. Pull `journalctl --user -u xmpd -n 60 --no-pager` from `[TEST_HOST_1]` and `[TEST_HOST_2]`. Assert presence of the bidir INFO log line on `[TEST_HOST_1]` (round-trip with row count) and the startup_nudge INFO line on `[TEST_HOST_2]`.

Pass criteria: all four assertions hold; report captures all four DB query outputs and both journalctl excerpts.

Anti-pattern watch: #1 (don't trust `add_play` returning a `local_id`; SELECT the row back, which step 4 does), #5 (verify the bidir actually fired by checking the INFO log line in step 8, not just by seeing `synced_at` populated -- the latter alone could come from a coalesced earlier call).

---

### Loop B: offline drain

Cross-reference: FUNCTIONAL_QA_STRATEGY.md User Loop B.

Surfaces touched: HistoryReporter side effect, HistorySyncer offline-tolerance, receiver bidir on reconnect.

Steps:

1. ASK USER for the offline-simulation technique (see Risky-action gating above). Default recommendation is option (b) per-host route block.
2. Capture WATCHTOWER's tailnet IP from `[TEST_HOST_1]`:
   ```bash
   ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   tailscale status | grep -i watchtower
   EOF
   ```
   Record the IP (e.g. `100.x.y.z`).
3. Apply the chosen offline simulation. For option (b):
   ```bash
   ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   sudo iptables -I OUTPUT -d <WATCHTOWER_IP> -p tcp --dport 22 -j REJECT
   echo "applied"
   EOF
   ```
   If `sudo` is needed and the heredoc cannot supply it, ASK USER to apply it manually and confirm before continuing.
4. Confirm offline state:
   ```bash
   ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   timeout 5 ssh -o ConnectTimeout=3 -o BatchMode=yes WATCHTOWER true 2>&1 ; echo "exit=$?"
   EOF
   ```
   Expected: non-zero exit (connection refused/blocked).
5. Trigger a play >=30s on `[TEST_HOST_1]` (same approach as Loop A; reuse user's prior track choice if applicable -- ASK USER).
6. Wait 35s.
7. Read local DB on `[TEST_HOST_1]`:
   ```bash
   ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   sqlite3 -header -column ~/.config/xmpd/history.db "SELECT host, local_id, played_at, synced_at FROM plays ORDER BY local_id DESC LIMIT 1;"
   EOF
   ```
   Assert: row exists, `synced_at IS NULL`.
8. Read journalctl on `[TEST_HOST_1]`:
   ```bash
   ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   journalctl --user -u xmpd -n 30 --no-pager | grep -i 'history_syncer\|tailscale\|bidir\|offline'
   EOF
   ```
   Assert: a WARNING line is present indicating the precheck failed or the bidir was skipped due to peer offline. (Exact wording is set in Phase 3 -- accept any of: "tailscale precheck", "WATCHTOWER offline", "skipping bidir". If absent, this is a Loop B failure.)
9. Restore connectivity. For option (b):
   ```bash
   ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   sudo iptables -D OUTPUT -d <WATCHTOWER_IP> -p tcp --dport 22 -j REJECT
   timeout 5 ssh -o ConnectTimeout=3 -o BatchMode=yes WATCHTOWER true 2>&1 ; echo "exit=$?"
   EOF
   ```
   Assert: ssh exit 0.
10. Trigger another play >=30s on `[TEST_HOST_1]` to drive a fresh bidir attempt (do NOT restart the daemon -- we want the steady-state retry path, not startup_nudge). Wait 35s.
11. Re-read local DB:
    ```bash
    ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
    echo '__START__'
    sqlite3 -header -column ~/.config/xmpd/history.db "SELECT host, local_id, played_at, synced_at FROM plays WHERE synced_at IS NULL;"
    EOF
    ```
    Assert: zero rows returned (both queued plays are now synced).
12. Re-read aggregator on WATCHTOWER:
    ```bash
    ssh WATCHTOWER <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
    echo '__START__'
    sqlite3 -header -column ~/xmpd-history/history.db "SELECT server_id, host, local_id, played_at FROM plays WHERE host='[TEST_HOST_1]' ORDER BY server_id DESC LIMIT 3;"
    EOF
    ```
    Assert: both queued plays appear with consecutive `server_id`s.

Pass criteria: assertions in steps 4, 7, 8, 9, 11, 12 all hold.

Anti-pattern watch: #10 (don't trust an exit-0 bidir without checking BOTH `synced_at` populated AND `last_received_server_id` advanced -- we cover the former in step 11; for the cursor, optionally read `sync_state` table to confirm `last_received_server_id` increased).

Rollback safety: if anything goes wrong mid-loop, ALWAYS run the iptables `-D` (or `systemctl --user start tailscaled` if option (a) was chosen) before exiting the phase.

---

### Loop C: fzf cross-host browse

Cross-reference: FUNCTIONAL_QA_STRATEGY.md User Loop C.

Surfaces touched: `xmpctl history-json`, `bin/xmpd-history`.

Pre-condition: at least one play row from each of `[LIVE_HOST]`, `[TEST_HOST_1]`, `[TEST_HOST_2]` must be visible in `[TEST_HOST_1]`'s local DB (Loop A guarantees a `[TEST_HOST_1]` row; rows from `[LIVE_HOST]` and `[TEST_HOST_2]` arrive via Loop A's bidir if those hosts have any plays in the aggregator).

If any host is missing from `[TEST_HOST_1]`'s local DB, query the aggregator on WATCHTOWER first to confirm which hosts have plays at all:
```bash
ssh WATCHTOWER <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
echo '__START__'
sqlite3 -header -column ~/xmpd-history/history.db "SELECT host, COUNT(*) AS rows, MAX(played_at) AS latest FROM plays GROUP BY host;"
EOF
```
If `[LIVE_HOST]` is absent (the user has not played anything since the daemon was wired up): document this in the report under `### Loop C -> Pre-condition gap` and proceed with whatever hosts are present. The check is "fzf shows the rows it has", not "fzf shows three specific hosts at all costs".

Steps:

1. ASK USER which observation mode to use (see Risky-action gating). Pick one.
2. **Mode 1 (user-driven)**: write the steps in the report, ASK USER to perform them, paste their reported observations in.
3. **Mode 2 (tmux)**:
   ```bash
   ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   tmux new-session -d -s phase8_loopc 'xmpd-history' \; pipe-pane -t phase8_loopc -o 'cat >> /tmp/phase8_loopc.log'
   sleep 2
   tmux capture-pane -t phase8_loopc -p
   EOF
   ```
   Then for each interaction, send keys via `tmux send-keys -t phase8_loopc <keys>` and re-capture. Tear down: `tmux kill-session -t phase8_loopc`.
4. **Mode 3 (script transcript)**: ASK USER to run `script -c xmpd-history /tmp/phase8_loopc.txt` interactively on `[TEST_HOST_1]` and paste the typescript when done.

For all modes, capture verifiable observations:

   a. **fzf opens with multi-host rows**: capture the first ~10 lines fzf renders. Each line should match the design-spec format (provider tag, time, artist - title, host as dim suffix). Assert: lines include the host suffix; at least one line per host present in the aggregator is visible.

   b. **Hostname filter narrows results**: type `[TEST_HOST_1]` (or whichever host is present). Assert: visible row count drops to only that host's rows.

   c. **`ctrl-t` toggles mode**: press `ctrl-t`. Assert: line format changes from time mode (`May-12 19:39  Artist - Title`) to count mode (`x42  Artist - Title  last May-12`). Capture before/after lines.

   d. **`enter` plays the selected track**: select any row, press `enter`. Assert: `mpc status` on `[TEST_HOST_1]` shows the chosen track is now playing (or queued + playing). If the track is the same one that's already playing from Loop A, expect MPD to restart playback or no-op depending on player state -- either is acceptable as long as the chosen track is the current song.
   ```bash
   ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   mpc current
   mpc status
   EOF
   ```

Pass criteria: a, b, c, d all observed and recorded with byte-for-byte capture (or user confirmation paragraph if Mode 1 was chosen). If the fzf wrapper crashes, hangs, or shows empty output, this is a Loop C failure.

Anti-pattern watch: #4 (don't pass Loop C just because `bash -n bin/xmpd-history` succeeds; we exercise the real fzf bindings in steps a-d).

---

### Loop D: backfill from MPD log

Cross-reference: FUNCTIONAL_QA_STRATEGY.md User Loop D.

Surfaces touched: `xmpctl history-backfill`, post-commit bidir push.

Steps:

1. Determine whether `[TEST_HOST_1]` has an MPD log:
   ```bash
   ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   for p in ~/.mpdlog ~/.mpd/log /var/log/mpd/mpd.log /var/log/mpd.log; do
     if [ -f "$p" ]; then
       echo "FOUND: $p ($(wc -l < "$p") lines)"
       grep -c 'player: played' "$p" 2>/dev/null || true
     fi
   done
   echo '---'
   grep -E '^log_file' ~/.mpdconf ~/.mpd/mpd.conf /etc/mpd.conf 2>/dev/null || echo 'no log_file in any mpd.conf'
   EOF
   ```
   Capture output. Two outcomes:
   - **No log found**: document in the report under `### Loop D -> Skipped (no MPD log on [TEST_HOST_1])`. Record what was checked. Mark Loop D PASS-SKIPPED. Move on.
   - **Log found**: proceed.
2. **Dry-run**:
   ```bash
   ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   xmpctl history-backfill --dry-run
   echo '---'
   sqlite3 ~/.config/xmpd/history.db "SELECT COUNT(*) AS row_count FROM plays;"
   EOF
   ```
   Capture output. Assert: stdout contains `would-insert=N would-skip=M orphans=K`; the row count after dry-run matches the row count BEFORE the dry-run (record both in the report).
3. **Real run**:
   ```bash
   ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   PRE=$(sqlite3 ~/.config/xmpd/history.db "SELECT COUNT(*) FROM plays;")
   echo "pre=$PRE"
   xmpctl history-backfill
   POST=$(sqlite3 ~/.config/xmpd/history.db "SELECT COUNT(*) FROM plays;")
   echo "post=$POST"
   EOF
   ```
   Assert: stdout `inserted=N skipped=M orphans=K`; `post - pre == N`; the `N` and `K` from the real run match the `would-insert` and `orphans` from step 2 dry-run.
4. **Post-commit bidir verification**:
   ```bash
   ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   journalctl --user -u xmpd -n 50 --no-pager | grep -iE 'bidir|push|history_syncer' | tail -20
   sleep 3
   sqlite3 ~/.config/xmpd/history.db "SELECT COUNT(*) FROM plays WHERE synced_at IS NULL;"
   EOF
   ```
   Assert: an INFO bidir log line appears within the last 50 entries; unsynced count is 0 (or very small if the bidir is still in flight -- rerun after another 5s if non-zero).
5. **Idempotency**:
   ```bash
   ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   PRE=$(sqlite3 ~/.config/xmpd/history.db "SELECT COUNT(*) FROM plays;")
   echo "pre=$PRE"
   xmpctl history-backfill
   POST=$(sqlite3 ~/.config/xmpd/history.db "SELECT COUNT(*) FROM plays;")
   echo "post=$POST"
   EOF
   ```
   Assert: stdout reports `inserted=0 skipped=N orphans=K`; `post == pre`.
6. **Aggregator confirmation**:
   ```bash
   ssh WATCHTOWER <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   sqlite3 ~/xmpd-history/history.db "SELECT COUNT(*) FROM plays WHERE host='[TEST_HOST_1]';"
   EOF
   ```
   Assert: count is at least the post-backfill local row count for `host='[TEST_HOST_1]'`. (Aggregator may have additional plays from older Loop A runs -- comparison is `>=`, not `==`.)

Pass criteria: steps 2, 3, 4, 5, 6 all assertions hold (or step 1 documents skip).

---

### Loop E: doctor

Cross-reference: FUNCTIONAL_QA_STRATEGY.md User Loop E.

Surfaces touched: `bin/xmpd-doctor`, receiver `doctor` subcommand.

Steps:

1. **Green run**:
   ```bash
   ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
   echo '__START__'
   xmpd-doctor
   echo "exit=$?"
   EOF
   ```
   Assert: stdout contains all sections from the design spec (`Local`, `Cluster (via WATCHTOWER)`, `Per-host row state`); fields `Tailscale daemon`, `WATCHTOWER peer online`, `SSH WATCHTOWER`, `Receiver installed`, `Local history DB`, `Last successful bidir` are all populated; per-host counts match the counts in step D.6 (modulo any new plays). Exit code is 0.
2. **Yellow run** (offline-expected peer):
   - ASK USER for offline simulation (same gating as Loop B). Default: option (b) iptables block to WATCHTOWER.
   - Apply the simulation (capture the iptables command and its output).
   - Run `xmpd-doctor` again, capture output and exit code. Assert: exit code 2; the affected line (`WATCHTOWER peer online: NO` or `SSH WATCHTOWER: TIMEOUT/FAIL`) reflects the offline state; sections still render.
   - Restore connectivity (iptables `-D`); confirm with another `xmpd-doctor` returning exit 0.
3. **Red run** (local DB missing):
   - ASK USER before renaming (see Risky-action gating).
   - Move the DB:
     ```bash
     ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
     echo '__START__'
     mv ~/.config/xmpd/history.db ~/.config/xmpd/history.db.phase8_bak
     ls -la ~/.config/xmpd/history.db.phase8_bak
     EOF
     ```
   - Run `xmpd-doctor`, capture output and exit code. Assert: exit code 1; `Local history DB` line shows MISSING / FAIL state; the script still completes (no unhandled exception).
   - Restore the DB:
     ```bash
     ssh [TEST_HOST_1] <<'EOF' 2>/dev/null | sed -n '/^__START__$/,$p' | tail -n +2
     echo '__START__'
     mv ~/.config/xmpd/history.db.phase8_bak ~/.config/xmpd/history.db
     ls -la ~/.config/xmpd/history.db
     EOF
     ```
   - Run `xmpd-doctor` once more; assert exit 0 (back to green).

Pass criteria: green/yellow/red all behave per the design spec; rollbacks succeed; final state is green.

Note on the daemon during the red run: the live `xmpd` daemon may notice the missing DB. Do not restart it; let it self-recover when the DB returns. Capture any journalctl warnings about the missing DB in the report under `### Loop E -> Red run side effects`.

---

### Bug-fix protocol

If a loop reveals a bug:

1. **Stop the loop**. Capture the failure state (commands run, expected vs actual) in the report under `### Loop X -> Failure observed`.
2. **Identify scope**. The fix MUST be:
   - Single file under `xmpd/` OR `bin/` OR `scripts/` (one file, not multiple).
   - Tightly scoped (no module restructuring, no new abstractions).
3. If the scope is larger than one file or requires architectural changes: STOP. Document under `## Escalation` in the report. Return to user.
4. **Fix protocol** (when scope is acceptable):
   - Write a regression test FIRST in the appropriate `tests/test_<module>.py`. The test must FAIL against the current code. Capture the failing output in the report.
   - Make the minimal fix.
   - Re-run the regression test. Assert PASS. Capture output.
   - Re-run the full test suite for the affected module: `uv run pytest tests/test_<module>.py -xvs`. Assert all pass.
   - Run `uv run ruff check xmpd/ bin/ scripts/ tests/` and `uv run mypy xmpd/`. Both must be clean.
   - Add a `# regression for Loop <X> failure: <one-line description>` comment near the fix and at the top of the regression test.
   - Commit: `[Phase 8/8] fix: <module>: <one-line description> (Loop <X>)` + a body line `Regression: tests/test_<module>.py::<test_name>`.
5. After commit: replicate via the helper to both peers (`scripts/spark-restart-peer.sh [TEST_HOST_1] xmpd` and `[TEST_HOST_2]`). Re-run the failing loop from the start.
6. If the loop still fails after the fix: STOP, escalate.

### Report format

`docs/agent/xmpd-history/INTEGRATION_TEST_REPORT.md`:

```markdown
# Phase 8 Integration Test Report

**Date**: <YYYY-MM-DD>
**Branch HEAD at start**: <git rev-parse HEAD>
**Hosts under test**: [TEST_HOST_1], [TEST_HOST_2], WATCHTOWER

## Summary

5 loops, M passed, K failed-and-fixed, J escalated.

| Loop | Verdict | Bug-fix commits |
|------|---------|------------------|
| A: play roundtrip   | PASS / FAIL-FIXED / ESCALATED | <sha or "none"> |
| B: offline drain    | ... | ... |
| C: fzf browse       | ... | ... |
| D: backfill         | ... | ... |
| E: doctor           | ... | ... |

## Loop A: play roundtrip

### Pre-conditions
- Both peers at HEAD <sha>.
- User-chosen track: <name / URI>.
- (etc.)

### Commands and observations

#### Step 1: peer HEAD verification
<pasted heredoc + output>

#### Step 4: [TEST_HOST_1] local DB
<pasted query + result>

(... etc. one subsection per numbered step ...)

### Verdict
PASS / FAIL-FIXED / ESCALATED. <one-line rationale>.

(... repeat for Loops B, C, D, E ...)

## Escalation

(present only if any loop escalated; otherwise omit the section)
- **Loop X**: <description of the architectural issue, why it cannot be surgically fixed, recommended next step for the user>.
```

---

## Dependencies

**Requires**: All phases 1-7 complete and committed.
- Phase 1: HistoryStore + config + conftest.
- Phase 2: HistoryReporter wire-up + syncer stub.
- Phase 3: HistorySyncer real `bidir_push` and `startup_nudge`.
- Phase 4: `scripts/xmpd-history-receiver` deployed to WATCHTOWER.
- Phase 5: `xmpctl history-json` and `bin/xmpd-history`.
- Phase 6: `xmpctl history-backfill`.
- Phase 7: `bin/xmpd-doctor`.

**Enables**: feature is shipped. No further phases.

---

## Completion Criteria

- [ ] `INTEGRATION_TEST_REPORT.md` exists with all five loop sections.
- [ ] Each loop section contains: pre-conditions, commands run (heredocs included), observed outputs (byte-for-byte), verdict.
- [ ] Top-level summary table is filled in.
- [ ] All risky-action gates were honored (USER ASKED, response recorded in the report).
- [ ] Any bug fixes follow the protocol (regression test first, single-file scope, fix commit + regression comment).
- [ ] No fix commit touches `[LIVE_HOST]` state.
- [ ] All offline simulations were rolled back; final state on both peers is steady-state.
- [ ] All bug fixes have passing tests; `uv run pytest -xvs`, `uv run ruff check .`, `uv run mypy xmpd/` all clean.
- [ ] Phase summary written at `docs/agent/xmpd-history/summaries/PHASE_08_SUMMARY.md` referencing the report.
- [ ] STATUS.md updated marking Phase 8 complete and feature done.

---

## Testing Requirements

- Regression tests are written ONLY in response to a loop failure (one per bug, per the protocol above). No proactive new tests this phase.
- Existing test suite must remain green after any fix: `uv run pytest -xvs` passes.
- `uv run ruff check .` clean.
- `uv run mypy xmpd/` clean.

---

## Functional QA

The five User Loops below ARE the deliverables for this phase. Each is a check; pass/fail is the loop verdict captured in the report. No separate "Functional QA Results" section is required in the phase summary because the report IS the Functional QA Results -- the phase summary just references it.

- [ ] **(Loop A: play roundtrip; surfaces HistoryReporter side effect, HistorySyncer, receiver bidir, HistoryStore)** Triggered a >=30s play on `[TEST_HOST_1]` (user-confirmed track choice). New row appears in `[TEST_HOST_1]` local DB with `host='[TEST_HOST_1]'`. Within ~5s, `synced_at` is non-NULL. Same row is present in WATCHTOWER aggregator with a fresh `server_id`. After restarting xmpd on `[TEST_HOST_2]` via `scripts/spark-restart-peer.sh`, `[TEST_HOST_2]`'s local DB shows the `[TEST_HOST_1]` row with originating host preserved. Bidir INFO log line present in `[TEST_HOST_1]`'s journalctl; startup_nudge INFO line present in `[TEST_HOST_2]`'s. All four DB query outputs and both journalctl excerpts pasted byte-for-byte in the report.

- [ ] **(Loop B: offline drain; surfaces HistoryReporter side effect, HistorySyncer offline-tolerance)** With WATCHTOWER unreachable from `[TEST_HOST_1]` (user-confirmed simulation technique applied), a >=30s play results in a row with `synced_at IS NULL`. Journalctl on `[TEST_HOST_1]` shows the WARNING about offline precheck (any of: "tailscale precheck", "WATCHTOWER offline", "skipping bidir"). After restoring connectivity and triggering one more play, all queued rows have `synced_at` populated AND the aggregator on WATCHTOWER shows both rows with consecutive `server_id`s. Offline simulation rolled back successfully; final connectivity check returns ssh exit 0.

- [ ] **(Loop C: fzf cross-host browse; surfaces `xmpctl history-json`, `bin/xmpd-history`)** With cross-host rows present in `[TEST_HOST_1]`'s local DB (from Loop A's bidir), launching `xmpd-history` on `[TEST_HOST_1]` (user-chosen observation mode: interactive ssh OR tmux capture OR `script` transcript) shows fzf opening immediately. Initial render includes one or more lines per host present in the aggregator with the host as a dim suffix (per design spec format). Typing a hostname narrows results. Pressing `ctrl-t` flips the line format from time mode (`May-12 19:39  Artist - Title`) to count mode (`x42  Artist - Title  last May-12`). Pressing `enter` on a row results in the chosen track becoming the current MPD song on `[TEST_HOST_1]` (verified via `mpc current` after the action). All four observations recorded in the report.

- [ ] **(Loop D: backfill; surfaces `xmpctl history-backfill`)** If no MPD log exists on `[TEST_HOST_1]` (verified via `~/.mpdlog`, `~/.mpd/log`, `/var/log/mpd/*`, plus `log_file` in any mpd.conf): document the absence and mark PASS-SKIPPED. Otherwise: `xmpctl history-backfill --dry-run` reports `would-insert=N would-skip=M orphans=K` and adds zero new DB rows. The real `xmpctl history-backfill` reports counts matching the dry-run; local row count increases by exactly N; one bidir INFO log line appears in journalctl within 50 entries; unsynced count returns to 0 within ~5s. Rerun reports `inserted=0 skipped=N orphans=K`; row count unchanged. Aggregator on WATCHTOWER has `>= N` rows for `host='[TEST_HOST_1]'`.

- [ ] **(Loop E: doctor; surfaces `bin/xmpd-doctor`, receiver `doctor`)** Green run: `xmpd-doctor` on `[TEST_HOST_1]` renders all sections (Local, Cluster, Per-host row state) with all fields populated; exit code 0. Yellow run: with WATCHTOWER unreachable (user-confirmed simulation), `xmpd-doctor` exits 2 and the affected line reflects the offline state; sections still render. Restore -> exit 0 again. Red run: with `~/.config/xmpd/history.db` renamed to `.phase8_bak` (user-confirmed), `xmpd-doctor` exits 1 and the `Local history DB` line shows MISSING / FAIL; script completes without unhandled exception. Restore -> exit 0 again. All three exit codes and full stdout for each run captured in the report.

### Cross-cutting anti-patterns to watch

From `FUNCTIONAL_QA_STRATEGY.md`, this phase is especially prone to:
- **#1**: Asserting `add_play` worked by checking only the returned `local_id`. Loops A and B SELECT the row back via `sqlite3` -- never accept "the daemon logged it" as evidence.
- **#5**: Asserting `bidir_push` queued without checking `subprocess.Popen` was called. We can't observe `subprocess.Popen` directly in live runs, so use the journalctl INFO log line as the proxy (see Loop A step 8, Loop D step 4).
- **#6**: Restarting `xmpd` on `[LIVE_HOST]`. NEVER. The phase has zero deliverables on `[LIVE_HOST]`. If a loop seems to need this, you're misreading the loop -- escalate.
- **#7**: Restarting a peer before Syncthing replication. The `scripts/spark-restart-peer.sh` helper enforces the wait-for-HEAD-match contract. Use it for every restart.
- **#8**: `ssh HOST "command"` syntax. Always heredoc.
- **#10**: Skipping post-bidir verification. Loops A, B, D all check `synced_at` populated AND assert specific row data; do not shortcut.

---

## Helpers Required

- **`scripts/spark-restart-peer.sh`** -- wait for Syncthing-replicated HEAD match on a peer, then SSH-restart the user-service xmpd (or named service) and report fresh `is-active` + last 20 journalctl lines.
  - **Invocation**: `scripts/spark-restart-peer.sh <peer> [service]` -- `peer` is the `~/.ssh/config` alias (e.g. `[TEST_HOST_1]`); optional `service` defaults to `xmpd`.
  - **Used for**: every peer restart in Loops A, B, C, D, E. Roughly 5+ invocations per loop run; the helper is the difference between a reproducible run and a flaky one.
  - **On failure**: read the `# MANUAL FALLBACK:` block in the script, do the work manually (compute LOCAL_HEAD; ssh-heredoc to peer; loop on `git rev-parse HEAD` until match; ssh-heredoc to restart the service; print is-active + journalctl). Record the failure in the phase summary's "Helper Issues" section. Do NOT edit the helper.

---

## Notes

- This phase's value is in the rigor of observation, not the volume of new code. Do NOT add features. Do NOT refactor. The bar is "I can prove the feature works against real systems on real machines, with byte-for-byte evidence captured in a report".
- The risky-action gates exist because I cannot recover from a misjudgment that knocks an SSH session offline mid-phase. ASK USER. Wait for confirmation. Then act.
- The user is actively listening on `[LIVE_HOST]` while this phase runs. Treat `[LIVE_HOST]` as read-only at the OS level. Read-only DB queries against `~/.config/xmpd/history.db` on `[LIVE_HOST]` are NOT in scope -- the only `[LIVE_HOST]` interaction this phase requires is reading the WATCHTOWER aggregator (which contains `[LIVE_HOST]`'s rows if any have synced).
- WATCHTOWER's aggregator is a shared resource. Avoid mutating it (no DELETE, no DROP). Reads are unrestricted.
- If a loop reveals an architectural issue and you escalate: be specific. Name the file, the contract that's wrong, and what fix categories you considered before deciding it was beyond surgical scope.
- The phase summary at `docs/agent/xmpd-history/summaries/PHASE_08_SUMMARY.md` should be brief: reference `INTEGRATION_TEST_REPORT.md` for the loop-level detail and only summarize the top-level outcome plus any escalations. Do not duplicate the report's contents into the summary.
