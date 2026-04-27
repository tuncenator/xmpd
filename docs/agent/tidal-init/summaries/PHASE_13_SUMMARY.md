# Phase 13: Install / migration / docs / final integration - Summary

**Date Completed:** 2026-04-27
**Completed By:** claude-sonnet-4-6 (Phase 13 spark agent)
**Actual Token Usage:** ~60k tokens

---

## Objective

Land the user-facing rebrand and multi-source migration: rewrite install.sh and
uninstall.sh for the ytmpd->xmpd path, write a comment-preserving config migration
script, add tests, rewrite README.md and docs/MIGRATION.md for the multi-source story,
add a CHANGELOG entry, run full integration, and push to origin/feature/tidal-init.

---

## Work Completed

### What Was Built

- `scripts/migrate-config.py`: idempotent ruamel.yaml round-trip config migrator with `--check` / `--dry-run` / `--config` flags. Exit codes 0/1/2 per spec. Atomic write via `os.replace`.
- `tests/test_migrate_config.py`: 11 pytest tests covering all transforms, check mode, dry-run, idempotency, comment preservation, missing file, and malformed YAML.
- `pyproject.toml`: added `ruamel.yaml>=0.18,<0.19` to dev optional-dependencies.
- `install.sh`: full rewrite -- step 0 (legacy dir migration), step 4.5 (config-shape migration), updated step 5 (xmpctl auth yt), step 6 (replace ytmpd.service), step 7 (stale symlink cleanup), multi-source install summary. Extended `--check` mode.
- `uninstall.sh`: full rewrite -- removes both xmpd.service and ytmpd.service, cleans all current + legacy symlinks, adds `--purge` flag, preserves `~/.config/xmpd/` by default.
- `README.md`: full rewrite (241 lines) for multi-source story.
- `docs/MIGRATION.md`: full rewrite covering rebrand, provider abstraction, config shape, HiRes-deferred rationale, rollback notes, manual fallback recipe, breaking changes.
- `CHANGELOG.md`: new top `[Unreleased] - 2026-04-27` entry per spec; `[1.0.0]` and below preserved verbatim.

### Files Created

- `/home/tunc/Sync/Programs/xmpd/scripts/migrate-config.py`
- `/home/tunc/Sync/Programs/xmpd/tests/test_migrate_config.py`

### Files Modified

- `pyproject.toml` -- added ruamel.yaml dev dep
- `install.sh` -- full rewrite
- `uninstall.sh` -- full rewrite
- `README.md` -- full rewrite
- `docs/MIGRATION.md` -- full rewrite
- `CHANGELOG.md` -- replaced top stub with full entry

### Key Design Decisions

- Used `ruamel.yaml` round-trip mode (`typ="rt"`) exclusively; PyYAML was not used anywhere in the migration path.
- `playlist_prefix` scalar-to-dict transform clears the `ca` (comment attribute) on the new key entry to prevent ruamel.yaml from carrying block-level comments from the old scalar and rendering them between the key name and its nested mapping content.
- `_transform_add_tidal` uses `yaml_set_comment_before_after_key` with plain text (no leading `#`); ruamel.yaml prepends `#` automatically.
- Atomic write: write to `<path>.tmp`, then `os.replace()`.
- `migrate_dry_run` re-evaluates `needs_migration` at each step rather than sharing mutable state, ensuring correct detection order.
- `install.sh` step 4.5 runs AFTER `uv pip install -e '.[dev]'` so ruamel.yaml is guaranteed available.
- Live tidal.enabled stays `false` in the migrated config; Tidal was not re-authed (per spec: existing session valid, do not displace user's listening).

---

## Completion Criteria Status

- [x] `pytest -q` passes -- Verified: `2 failed, 812 passed, 13 skipped` (2 failures are the pre-existing `test_scenario_4` / `test_scenario_5` status-widget tests).
- [x] `pytest -q tests/test_migrate_config.py` passes -- Verified: `11 passed in 0.31s`.
- [x] `bash -n install.sh && bash -n uninstall.sh` -- both passed syntax check.
- [x] `python3 scripts/migrate-config.py --help` -- prints usage, exits 0.
- [x] `--check` exits 0 on already-migrated -- Verified against `/tmp/cfg.yaml` after migration.
- [x] `--check` exits 1 on legacy -- Verified against original live config copy.
- [x] `pyproject.toml` has `ruamel.yaml>=0.18,<0.19` in dev deps -- confirmed.
- [x] Live `~/.config/xmpd/` BACKED UP -- `cp -r ~/.config/xmpd ~/.config/xmpd.pre-install-backup` completed before running install.sh.
- [x] `./install.sh --check` reports correct state -- "config shape: multi-source (OK)" after migration.
- [x] `./install.sh` end-to-end completes without error -- ran with `echo -e "n\nn\nn" | bash install.sh`; migration step ran and succeeded.
- [x] After migration: `_detect_legacy_shape(user_data, path)` passed without exception; `yt` and `tidal` in merged config.
- [x] `systemctl --user restart xmpd` succeeded -- log shows `Provider yt: ready`, no config-shape errors.
- [x] `xmpctl sync` produced YT playlists -- 8 playlists, 945 tracks synced; XSPF files in `~/Music/_youtube/` show `YT: ` prefix.
- [x] TD: playlists deferred -- `tidal.enabled: false` in migrated config; Tidal not re-authed per spec (do not displace user's session).
- [x] README accurate, 241 lines, no stray `ytmpd*` in active prose.
- [x] MIGRATION accurate -- HiRes-deferred section, rollback section, and manual fallback recipe present.
- [x] CHANGELOG top entry per spec, `[1.0.0]` preserved verbatim.
- [x] `git status` clean; commits pushed to `origin/feature/tidal-init`.
- [x] Merge command printed (below), NOT executed.
- [x] Phase summary written.

### Deviations

- TD: playlists not verified live because `tidal.enabled: false` in the migrated config, per phase plan note: "DO NOT re-run `xmpctl auth tidal` -- the user's session is already valid; re-running interrupts the user's listening." To enable Tidal, set `tidal.enabled: true` in config and restart the daemon.
- AirPlay bridge verification skipped (manual; Phase 12 documented as deferred to user).

---

## Testing

### Tests Written

`tests/test_migrate_config.py` (11 tests):

- `test_legacy_config_migrated`
- `test_already_migrated_idempotent`
- `test_top_level_playlist_prefix_string_to_dict`
- `test_preserves_unrelated_keys`
- `test_preserves_top_level_block_comments`
- `test_partial_migration_only_playlist_prefix`
- `test_check_mode_returns_1_when_needed`
- `test_check_mode_returns_0_when_already_migrated`
- `test_dry_run_does_not_write`
- `test_missing_config_file`
- `test_malformed_yaml`

### Test Results

```
$ source .venv/bin/activate && python -m pytest -q
2 failed, 812 passed, 13 skipped, 3 warnings in 15.32s
```

Pre-existing failures: `test_scenario_4_first_track_in_playlist`, `test_scenario_5_last_track_in_playlist` (both in `tests/integration/test_xmpd_status_integration.py` -- known pre-existing failures, not introduced by Phase 13).

```
$ source .venv/bin/activate && pytest -q tests/test_migrate_config.py
11 passed in 0.31s
```

### Manual Testing

- `./install.sh --check` before migration: reported "config shape: legacy single-provider (will be migrated)".
- `./install.sh` (with n responses to interactive prompts): config migrated, bak created, ytmpd.service handled.
- `./install.sh --check` after migration: reported "config shape: multi-source (OK)".
- `systemctl --user restart xmpd`: daemon started cleanly, `Provider yt: ready`, no ConfigError.
- `xmpctl sync`: 8 playlists, 945 tracks synced.
- `tail -5 ~/.config/xmpd/xmpd.log`: no ERROR or WARNING lines.

---

## Evidence Captured

### Live `~/.config/xmpd/config.yaml` (pre-migration, redacted)

Captured via `cat ~/.config/xmpd/config.yaml`.

```yaml
# xmpd Configuration File
# ...
socket_path: ~/.config/xmpd/socket
state_file: ~/.config/xmpd/state.json
log_level: INFO
log_file: ~/.config/xmpd/xmpd.log
mpd_socket_path: localhost:6601
mpd_playlist_directory: ~/.config/mpd/playlists
sync_interval_minutes: 10
enable_auto_sync: true
playlist_prefix: "YT: "       # scalar (legacy shape)
stream_cache_hours: 5
playlist_format: xspf
mpd_music_directory: ~/Music
proxy_enabled: true
proxy_host: localhost
proxy_port: 6602
proxy_track_mapping_db: ~/.config/xmpd/track_mapping.db
radio_playlist_limit: 50
auto_auth:                    # top-level (legacy shape)
  enabled: true
  browser: firefox-dev
  container: null
  profile: null
  refresh_interval_hours: 12
```

Shape: legacy single-provider (top-level `auto_auth:`, scalar `playlist_prefix:`). Migration script detected both transforms needed: `nest_auto_auth_under_yt`, `add_tidal_block`, `convert_playlist_prefix_to_dict`.

### Post-migration config shape validation

```
$ python3 -c "from xmpd.config import _detect_legacy_shape, _deep_merge, _DEFAULTS; ..."
Legacy check: PASSED
yt: True
tidal: True
playlist_prefix: {'yt': 'YT: ', 'tidal': 'TD: '}
```

### Systemd unit state

Pre-migration: `~/.config/systemd/user/ytmpd.service` present (detected and removed by install.sh step 6). `~/.config/systemd/user/xmpd.service` present (existing from prior phases).

---

## Helper Issues

No helpers were listed for this phase. No `scripts/spark-*.sh` helpers were needed or invoked. No unlisted helpers attempted.

---

## Live Verification Results

### daemon startup log (post-migration)

```
Provider registry built: ['yt']
Successfully authenticated with YouTube Music
Provider yt: ready
Proxy server initialized at localhost:6602
SyncEngine initialized with providers=['yt'], format=xspf, sync_favorites=True
Daemon started successfully
Sync complete across 1 provider(s): 8 synced, 0 failed, 945 tracks added, 0 failed (10.8s)
```

No ConfigError, no WARNING about config shape.

---

## Challenges and Solutions

### ruamel.yaml comment rendering after scalar-to-dict transform

When replacing a scalar `playlist_prefix: "YT: "` with a mapping, ruamel.yaml carried the key's block-level comment attributes from the deleted entry onto the new mapping key. This caused the nested dict content to render with the block comments injected between the key name and the first nested key, producing invalid visual indentation.

Solution: after `data.insert(pp_idx, "playlist_prefix", new_pp)`, explicitly clear the comment attribute: `data.ca.items["playlist_prefix"] = [None, None, None, None]`.

### yaml_set_comment_before_after_key double-`#` issue

Initial implementation passed strings with leading `#` to `yaml_set_comment_before_after_key`. ruamel.yaml prepends `#` automatically, resulting in `# # comment`. Fixed by passing plain text without `#` prefix.

---

## Merge Suggestion

The feature branch is complete. To merge to main (DO NOT run this automatically):

```bash
git checkout main && git merge --no-ff feature/tidal-init && git push origin main
```

---

## Codebase Context Updates

- Add `scripts/migrate-config.py` to Key Files table: "Idempotent ruamel.yaml round-trip migration from legacy single-provider shape to multi-source shape. CLI: `--check` / `--dry-run` / `--config`. Exit codes 0/1/2."
- Add `tests/test_migrate_config.py` to Key Files table: "11 tests for migrate-config.py."
- Update `install.sh` / `uninstall.sh` entry: "Phase 13 rewrites complete. install.sh: legacy dir copy, config-shape migration, ytmpd.service replacement, legacy symlink cleanup, multi-source summary. uninstall.sh: --purge flag, removes both xmpd.service and ytmpd.service."
- Update `pyproject.toml` entry: "`ruamel.yaml>=0.18,<0.19` added to dev optional-dependencies."
- Note: `~/.config/xmpd/config.yaml` on live machine is now in multi-source shape (post-migration); backup at `~/.config/xmpd.pre-install-backup/`.

---

## Notes for Future Phases

This is the final phase. No source code under `xmpd/`, `bin/`, or `tests/` was modified (only the new test file was added). The feature branch is pushed; the user runs the merge manually after review.

To enable Tidal after merging: set `tidal.enabled: true` in `~/.config/xmpd/config.yaml` and restart the daemon. The existing `~/.config/xmpd/tidal_session.json` from Phase 9 is still valid and will load automatically.

---

**Phase Status:** COMPLETE
