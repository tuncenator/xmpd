# Phase 02: YT module relocation + YTMusicProvider scaffold

**Feature**: tidal-init
**Estimated Context Budget**: ~30k tokens

**Difficulty**: easy

**Execution Mode**: parallel
**Batch**: 2

---

## Objective

Move the existing YouTube Music modules into the new package layout introduced by Phase 1 (`xmpd/providers/` and `xmpd/auth/`), update every import site across the codebase, and prepend a `YTMusicProvider` scaffold class to the relocated `xmpd/providers/ytmusic.py`. Method bodies for the full Provider Protocol arrive in Phase 3 -- this phase only ships `name`, `is_enabled`, `is_authenticated`, and `_ensure_client`.

No behavior change. The only externally observable effect is import paths.

---

## Deliverables

1. `git mv xmpd/ytmusic.py xmpd/providers/ytmusic.py` (preserves history).
2. `git mv xmpd/cookie_extract.py xmpd/auth/ytmusic_cookie.py` (preserves history).
3. Sed import updates across every `*.py` file in the repo (excluding `.venv/` and `.git/`):
   - `from xmpd.ytmusic` -> `from xmpd.providers.ytmusic`
   - `from xmpd.cookie_extract` -> `from xmpd.auth.ytmusic_cookie`
   - `import xmpd.ytmusic` -> `import xmpd.providers.ytmusic`
   - `import xmpd.cookie_extract` -> `import xmpd.auth.ytmusic_cookie`
4. `YTMusicProvider` scaffold class prepended to `xmpd/providers/ytmusic.py` (above the existing `Playlist`/`Track`/`YTMusicClient` body).
5. `xmpd/providers/__init__.py` `build_registry()` updated to instantiate `YTMusicProvider` when `yt` is enabled (replacing Phase 1's empty stub branch).
6. Cosmetic fix in the relocated `xmpd/auth/ytmusic_cookie.py`: `prefix="ytmpd_cookies_"` -> `prefix="xmpd_cookies_"`.
7. New `tests/test_providers_ytmusic.py` scaffold tests (4 tests).
8. All work committed in a single commit so git rename detection treats the file moves as renames (not delete+add).

---

## Detailed Requirements

### Step 1: Verify Phase 1 deliverables exist

Before doing anything, confirm Phase 1 landed:

```bash
test -f /home/tunc/Sync/Programs/xmpd/xmpd/providers/__init__.py
test -f /home/tunc/Sync/Programs/xmpd/xmpd/providers/base.py
test -d /home/tunc/Sync/Programs/xmpd/xmpd/auth/
test -f /home/tunc/Sync/Programs/xmpd/xmpd/auth/__init__.py
```

If any of these is missing, stop and surface a blocker -- Phase 1 has not landed yet and Phase 2 cannot proceed.

Also peek at `xmpd/providers/__init__.py` so you know exactly what to replace in Step 5:

```bash
cat /home/tunc/Sync/Programs/xmpd/xmpd/providers/__init__.py
```

### Step 2: Audit current import sites (baseline)

```bash
cd /home/tunc/Sync/Programs/xmpd
grep -rn "from xmpd\.ytmusic\|import xmpd\.ytmusic\|from xmpd\.cookie_extract\|import xmpd\.cookie_extract" \
  --include='*.py' . 2>/dev/null | grep -v '\.venv/' | grep -v '\.git/'
```

Expected files (recorded by setup -- treat as a baseline; reality may differ slightly):

- `xmpd/sync_engine.py` -- `from xmpd.ytmusic import Playlist, YTMusicClient`
- `xmpd/history_reporter.py` -- `from xmpd.ytmusic import YTMusicClient`
- `xmpd/daemon.py` -- `from xmpd.cookie_extract import FirefoxCookieExtractor` and `from xmpd.ytmusic import YTMusicClient`
- `tests/test_ytmusic.py` -- `from xmpd.ytmusic import Playlist, Track, YTMusicClient`
- `tests/test_ytmusic_history.py` -- `from xmpd.ytmusic import YTMusicClient`
- `tests/test_ytmusic_rating.py` -- `from xmpd.ytmusic import YTMusicClient`
- `tests/test_sync_engine.py` -- `from xmpd.ytmusic import Playlist, Track`
- `tests/test_like_indicator.py` -- `from xmpd.ytmusic import Playlist, Track`
- `tests/test_cookie_extract.py` -- `from xmpd.cookie_extract import _ORIGIN, FirefoxCookieExtractor`
- `tests/test_auto_auth_daemon.py` -- multiple `from xmpd.ytmusic import YTMusicClient` lines
- `tests/integration/test_rating_workflow.py` -- multiple `from xmpd.ytmusic import YTMusicClient` lines
- `tests/integration/test_auto_auth.py` -- `from xmpd.cookie_extract import FirefoxCookieExtractor`
- `tests/integration/test_full_workflow.py` -- `from xmpd.ytmusic import Playlist, Track, YTMusicClient`

Save this baseline for later comparison (record the file count in your phase summary).

### Step 3: Perform the renames with `git mv`

```bash
cd /home/tunc/Sync/Programs/xmpd
git mv xmpd/ytmusic.py         xmpd/providers/ytmusic.py
git mv xmpd/cookie_extract.py  xmpd/auth/ytmusic_cookie.py
```

Use `git mv` -- not plain `mv` + `git add -A`. `git mv` stages the rename atomically so git's similarity heuristic (defaults to 50% -- our case is far above) reports these as renames in `git log --diff-filter=R`.

DO NOT run `pytest` between Step 3 and Step 4 -- imports are broken until sed fixes them.

### Step 4: Sed-update every import site

Run these four commands in order. Each rewrites all `*.py` files in the working tree, excluding `.venv` and `.git`:

```bash
cd /home/tunc/Sync/Programs/xmpd

find . -path ./.git -prune -o -path ./.venv -prune -o -name '*.py' -print \
  | xargs sed -i 's|from xmpd\.ytmusic |from xmpd.providers.ytmusic |g'

find . -path ./.git -prune -o -path ./.venv -prune -o -name '*.py' -print \
  | xargs sed -i 's|from xmpd\.cookie_extract |from xmpd.auth.ytmusic_cookie |g'

find . -path ./.git -prune -o -path ./.venv -prune -o -name '*.py' -print \
  | xargs sed -i 's|import xmpd\.ytmusic|import xmpd.providers.ytmusic|g'

find . -path ./.git -prune -o -path ./.venv -prune -o -name '*.py' -print \
  | xargs sed -i 's|import xmpd\.cookie_extract|import xmpd.auth.ytmusic_cookie|g'
```

Note the trailing space after `from xmpd.ytmusic ` in the first two sed commands -- this prevents accidentally matching a hypothetical `from xmpd.ytmusic_extra` or `from xmpd.ytmusicapi` (the latter exists upstream as `ytmusicapi`, distinct from our module). The `import xmpd.ytmusic` form does not need this trick because `import` statements end at whitespace or newline; sed matches line-anchored to the literal string.

Verify with:

```bash
cd /home/tunc/Sync/Programs/xmpd
grep -rn "from xmpd\.ytmusic\|import xmpd\.ytmusic\|from xmpd\.cookie_extract\|import xmpd\.cookie_extract" \
  --include='*.py' . 2>/dev/null | grep -v '\.venv/' | grep -v '\.git/'
```

Expected output: empty. If any line remains, do NOT manually patch it without first inspecting it -- the only legitimate hits are the new paths (which would not match the search pattern). A residual hit means a malformed sed substitution; stop and inspect.

### Step 5: Prepend `YTMusicProvider` scaffold to `xmpd/providers/ytmusic.py`

Insert the following block immediately after the existing module docstring + `import` block (which currently ends on the line `from xmpd.rating import RatingManager, RatingState`), and before the `def _truncate_error(...)` helper.

Specifically: keep lines 1-19 (the docstring + imports + `logger = logging.getLogger(__name__)`) intact, then insert the new class, then leave the rest of the file untouched.

Concrete structure of the post-edit top of `xmpd/providers/ytmusic.py`:

```python
"""YouTube Music API wrapper for xmpd.

This module provides a wrapper around ytmusicapi that handles authentication
and provides clean interfaces for search, playback, and song info retrieval.
"""

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ytmusicapi import YTMusic

from xmpd.config import get_config_dir
from xmpd.exceptions import YTMusicAPIError, YTMusicAuthError, YTMusicNotFoundError
from xmpd.providers.base import Playlist as ProviderPlaylist  # noqa: F401  (Phase 3 uses this)
from xmpd.providers.base import Track as ProviderTrack        # noqa: F401  (Phase 3 uses this)
from xmpd.providers.base import TrackMetadata                 # noqa: F401  (Phase 3 uses this)
from xmpd.rating import RatingManager, RatingState

logger = logging.getLogger(__name__)


class YTMusicProvider:
    """Provider implementation for YouTube Music.

    Wraps :class:`YTMusicClient` (defined later in this module). Method bodies
    for the full Provider Protocol arrive in Phase 3; this scaffold only
    declares the class so the registry can construct it and tests can
    isinstance-check it (note: ``isinstance(p, Provider)`` will return False
    until Phase 3 finishes the method surface).
    """

    name = "yt"

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._client: Any = None  # YTMusicClient lazily constructed via _ensure_client

    def is_enabled(self) -> bool:
        return bool(self._config.get("enabled", False))

    def is_authenticated(self) -> bool:
        # Defer to the existing browser.json check used by YTMusicClient.
        # Phase 3 may refine this to also check token validity.
        return Path("~/.config/xmpd/browser.json").expanduser().is_file()

    def _ensure_client(self) -> "YTMusicClient":
        if self._client is None:
            self._client = YTMusicClient()  # YTMusicClient defined below in this file
        return self._client

    # Phase 3 adds:
    #   list_playlists, get_playlist_tracks, get_favorites,
    #   resolve_stream, get_track_metadata,
    #   search, get_radio,
    #   like, dislike, unlike, get_like_state,
    #   report_play


def _truncate_error(error: Exception, max_length: int = 200) -> str:
    """Truncate error message for logging to prevent massive log lines.
    ...
```

Notes:

- The `Playlist as ProviderPlaylist` / `Track as ProviderTrack` / `TrackMetadata` imports are added now (with `# noqa: F401`) so Phase 3 has the imports waiting and so this phase can compile a forward reference without renaming the local module-level `Playlist` / `Track` dataclasses (which still exist further down the file with the original names). `# noqa: F401` is required because these names are not yet used in this phase -- we add them only to keep Phase 3's diff small.
- DO NOT delete the existing `Playlist` / `Track` local dataclasses defined further down. They are still used by `YTMusicClient` internally and by the existing tests. Phase 3 converts them at the provider boundary.
- DO NOT touch the `YTMusicClient` body itself.
- Place `YTMusicProvider` ABOVE `_truncate_error` and the `@dataclass class Playlist:` declaration so that when forward-resolving `"YTMusicClient"` (a string forward reference) it works regardless of module load order.

### Step 6: Update `xmpd/providers/__init__.py`

Phase 1 wrote a stub `build_registry()` that returns an empty dict. Replace its `yt` branch with the real one. Keep the `tidal` branch as a Phase 9 TODO comment (do not introduce `TidalProvider` -- it does not exist yet).

The full target file:

```python
"""Provider registry for xmpd.

Builds the dict of enabled+authenticated providers from config. Provider
canonical names (``yt``, ``tidal``) are the registry keys; class/module names
are descriptive (``YTMusicProvider``, ``xmpd/providers/ytmusic.py``).
"""

import logging
from typing import Any

from xmpd.providers.base import Provider

logger = logging.getLogger(__name__)


def get_enabled_provider_names(config: dict[str, Any]) -> list[str]:
    """Return the list of provider canonical names that have ``enabled: true``.

    Authentication state is NOT consulted here -- the daemon decides whether
    to skip an enabled-but-unauthenticated provider after registry construction.
    """
    names: list[str] = []
    for canonical in ("yt", "tidal"):
        section = config.get(canonical, {})
        if isinstance(section, dict) and section.get("enabled", False):
            names.append(canonical)
    return names


def build_registry(config: dict[str, Any]) -> dict[str, Provider]:
    """Build the provider registry from config.

    Lazy-imports each concrete provider module so unselected providers do not
    pull in their upstream library at import time.
    """
    registry: dict[str, Provider] = {}
    enabled = get_enabled_provider_names(config)

    if "yt" in enabled:
        from xmpd.providers.ytmusic import YTMusicProvider
        registry["yt"] = YTMusicProvider(config["yt"])

    # if "tidal" in enabled:    # Phase 9 enables this branch
    #     from xmpd.providers.tidal import TidalProvider
    #     registry["tidal"] = TidalProvider(config["tidal"])

    logger.info("Provider registry built: %s", sorted(registry.keys()))
    return registry
```

If Phase 1's `__init__.py` already exposes `get_enabled_provider_names`, keep that signature and only modify `build_registry`. If Phase 1 did NOT define `get_enabled_provider_names`, add it as shown.

Note: the assignment `registry["yt"] = YTMusicProvider(config["yt"])` triggers a mypy complaint because `YTMusicProvider` does not yet implement the full `Provider` Protocol (Phase 3 fixes this). The complaint is expected; do not silence it with a `type: ignore` -- Phase 3 will resolve it cleanly. If the project's mypy configuration treats Phase 2 as a hard gate, add `# type: ignore[assignment]` with a comment `# Phase 3 completes Provider Protocol surface`.

### Step 7: Fix the `ytmpd_cookies_` leftover

The setup brief says line ~67; actual location is `xmpd/cookie_extract.py:155` (now `xmpd/auth/ytmusic_cookie.py:155`). Find the exact line in the relocated file and fix:

Before:
```python
        tmpdir = tempfile.mkdtemp(prefix="ytmpd_cookies_")
```

After:
```python
        tmpdir = tempfile.mkdtemp(prefix="xmpd_cookies_")
```

Verify with:

```bash
cd /home/tunc/Sync/Programs/xmpd
grep -rn "ytmpd_cookies_" xmpd/ tests/
```

Expected output: empty.

### Step 8: Create `tests/test_providers_ytmusic.py`

This file is owned by Phase 3 for the per-method tests. Phase 2 only adds the four scaffold tests below. Phase 3 will append more tests later -- design the file so it can grow.

```python
"""Tests for YTMusicProvider (Phase 2 scaffold).

Phase 3 will append per-method tests once the Provider Protocol surface is
implemented. The tests below intentionally exercise only the four scaffold
attributes/methods declared in Phase 2: ``name``, ``is_enabled``,
``is_authenticated``, and (implicitly via ``isinstance``) the partial
Protocol conformance.
"""

from typing import Any

from xmpd.providers.base import Provider
from xmpd.providers.ytmusic import YTMusicProvider


def test_ytmusic_provider_name() -> None:
    """The provider canonical name is ``yt``."""
    p = YTMusicProvider({})
    assert p.name == "yt"


def test_ytmusic_provider_is_enabled() -> None:
    """``is_enabled()`` reflects ``config['enabled']`` with default False."""
    assert YTMusicProvider({"enabled": True}).is_enabled() is True
    assert YTMusicProvider({"enabled": False}).is_enabled() is False
    assert YTMusicProvider({}).is_enabled() is False


def test_ytmusic_provider_is_authenticated_returns_bool() -> None:
    """``is_authenticated()`` returns a bool regardless of whether the file exists.

    We do NOT pre-create ``~/.config/xmpd/browser.json`` here -- this test only
    asserts the return type, not the value, so it is non-destructive on real
    user environments.
    """
    result = YTMusicProvider({}).is_authenticated()
    assert isinstance(result, bool)


def test_ytmusic_provider_isinstance_protocol_partial() -> None:
    """Phase 2 declares only ``name``/``is_enabled``/``is_authenticated``.

    ``Provider`` is ``@runtime_checkable``; full conformance requires the
    method surface that Phase 3 adds (list_playlists, get_playlist_tracks,
    get_favorites, resolve_stream, get_track_metadata, search, get_radio,
    like, dislike, unlike, get_like_state, report_play).

    This test asserts the CURRENT (Phase 2) state: isinstance returns False
    because the Provider Protocol's required methods are not yet present.
    Phase 3 flips this to ``is True`` -- update this assertion when Phase 3
    lands.
    """
    p: Any = YTMusicProvider({})
    assert isinstance(p, Provider) is False  # becomes True in Phase 3
```

If `Provider` is not `@runtime_checkable` in Phase 1's `base.py`, the last test will fail with a `TypeError` from `isinstance`. In that case, the test should instead inspect for the missing method names. Use this fallback only if `Provider` is not runtime-checkable:

```python
def test_ytmusic_provider_isinstance_protocol_partial() -> None:
    p = YTMusicProvider({})
    # Phase 2 has not added these method names yet:
    for method_name in ("list_playlists", "get_playlist_tracks", "resolve_stream"):
        assert not hasattr(p, method_name), (
            f"{method_name} should not exist until Phase 3"
        )
```

Default to the `isinstance(p, Provider) is False` form unless Phase 1's Protocol is not `@runtime_checkable`.

### Step 9: Run the test suite

```bash
cd /home/tunc/Sync/Programs/xmpd
source .venv/bin/activate
pytest -q
```

Expected: all tests pass. If anything fails, the most likely cause is a missed sed substitution somewhere -- re-run the verify grep from Step 4. Other possible causes:

- `xmpd/providers/__init__.py` syntax error in your edit -- run `python -c "from xmpd.providers import build_registry"` to isolate.
- `tests/test_cookie_extract.py` still references `xmpd.cookie_extract` -- it should now read `from xmpd.auth.ytmusic_cookie import _ORIGIN, FirefoxCookieExtractor`.
- The `# noqa: F401` import block in `xmpd/providers/ytmusic.py` may flag a circular import if `xmpd/providers/base.py` imports anything from `xmpd.providers.ytmusic`. It should NOT -- `base.py` is leaf-level. If the test run reports a circular import, inspect Phase 1's `base.py` and surface the issue (do not work around it locally).

### Step 10: Smoke-test imports manually

```bash
cd /home/tunc/Sync/Programs/xmpd
python -c "from xmpd.providers.ytmusic import YTMusicProvider, YTMusicClient; from xmpd.auth.ytmusic_cookie import FirefoxCookieExtractor; print('imports OK')"
python -c "from xmpd.providers import build_registry; r = build_registry({'yt': {'enabled': True}}); assert 'yt' in r; print('registry OK')"
python -c "from xmpd.providers import build_registry; r = build_registry({}); assert r == {}; print('empty registry OK')"
python -c "from xmpd.providers import build_registry; r = build_registry({'yt': {'enabled': False}}); assert r == {}; print('disabled yt OK')"
```

Each must print its OK line and exit 0.

### Step 11: Commit everything in one commit

```bash
cd /home/tunc/Sync/Programs/xmpd
git status
git add -A
git diff --cached --stat
git commit -m "$(cat <<'EOF'
phase 02: relocate yt modules into providers/ + auth/ packages

- git mv xmpd/ytmusic.py -> xmpd/providers/ytmusic.py
- git mv xmpd/cookie_extract.py -> xmpd/auth/ytmusic_cookie.py
- update import sites across xmpd/, tests/, tests/integration/
- add YTMusicProvider scaffold (name, is_enabled, is_authenticated, _ensure_client)
- wire YTMusicProvider into providers.build_registry
- fix tempfile prefix ytmpd_cookies_ -> xmpd_cookies_
- add tests/test_providers_ytmusic.py scaffold (4 tests)

Method bodies for the full Provider Protocol arrive in Phase 3.
EOF
)"
git log --oneline --diff-filter=R -1
```

The final `git log` must show the renames as renames (`R100` or similar). If the moves are reported as delete+add (`D` + `A`), the commit is wrong -- amend with `git mv` correctly.

---

## Edge Cases to Handle

- **Trailing-space sed safety**: `from xmpd.ytmusic ` (trailing space) prevents accidental matches against hypothetical sibling modules. This is the only safe way to do this with plain sed.
- **Dotted attribute access**: search did not find any `xmpd.ytmusic.SomeAttr` style usage in this codebase, but if it appears (e.g. `xmpd.ytmusic.YTMusicClient` with no `from` import), the `import xmpd.ytmusic` -> `import xmpd.providers.ytmusic` substitution covers it because the rewritten module path resolves at attribute lookup time.
- **`xmpd.cookie_extract` referenced by string in any tests** (e.g. `mocker.patch("xmpd.cookie_extract.X")`): grep with the broader pattern `grep -rn "xmpd\.cookie_extract\|xmpd\.ytmusic" --include='*.py'` and rewrite any string-form references manually if they exist. Run this AFTER the four sed commands and before pytest.
- **`tests/test_cookie_extract.py` filename**: do NOT rename the test file itself. Phase 2 only updates its imports. If a future cleanup wants to rename it to `tests/test_auth_ytmusic_cookie.py`, that is out of scope here.
- **Phase 1 `Provider` not `@runtime_checkable`**: scaffold test 4 has a documented fallback. Use it.
- **mypy on the registry assignment**: documented in Step 6. Acceptable to add `# type: ignore[assignment]` with the comment shown.
- **Editor whitespace differences**: do not let your editor strip trailing whitespace from `xmpd/providers/ytmusic.py` outside the new `YTMusicProvider` block. Use `git diff xmpd/providers/ytmusic.py` to confirm only your scaffold lines were touched (plus the three new `# noqa: F401` import lines). The `git mv` keeps file content unchanged; if extra whitespace diffs appear, your editor wrote unrelated changes -- revert them.

---

## Dependencies

**Requires**:
- Phase 1 (Provider abstraction foundation): `xmpd/providers/base.py` must define `Provider`, `Track`, `Playlist`, `TrackMetadata`. `xmpd/providers/__init__.py` and `xmpd/auth/__init__.py` must exist as packages. Phase 2 imports `Provider` from `xmpd.providers.base` for both `YTMusicProvider` (forward-reference) and the registry typing.

**Enables**:
- Phase 3 (YTMusicProvider methods): wraps the local `YTMusicClient` to implement the full Provider Protocol surface; tests in `tests/test_providers_ytmusic.py` extend the file Phase 2 created.
- Phase 6 (Provider-aware sync engine): `SyncEngine` will pull `YTMusicClient` indirectly via `provider.list_playlists()` etc., not via the now-relocated import path -- but Phase 6 imports must already work against the new path.
- Phase 8 (Daemon registry wiring): daemon constructs the registry by calling `build_registry()` -- the form Phase 2 ships is final for the YT branch.

---

## Completion Criteria

- [ ] `git mv` performed on `xmpd/ytmusic.py` -> `xmpd/providers/ytmusic.py`.
- [ ] `git mv` performed on `xmpd/cookie_extract.py` -> `xmpd/auth/ytmusic_cookie.py`.
- [ ] `grep -rn "from xmpd\.ytmusic\|import xmpd\.ytmusic\|from xmpd\.cookie_extract\|import xmpd\.cookie_extract" --include='*.py' . | grep -v '\.venv/\|\.git/'` returns zero lines.
- [ ] `python -c "from xmpd.providers.ytmusic import YTMusicProvider, YTMusicClient; from xmpd.auth.ytmusic_cookie import FirefoxCookieExtractor"` exits 0.
- [ ] `python -c "from xmpd.providers import build_registry; r = build_registry({'yt': {'enabled': True}}); assert 'yt' in r"` exits 0.
- [ ] `python -c "from xmpd.providers import build_registry; r = build_registry({}); assert r == {}"` exits 0.
- [ ] `python -c "from xmpd.providers import build_registry; r = build_registry({'yt': {'enabled': False}}); assert r == {}"` exits 0.
- [ ] `pytest -q` passes (no test imports from `xmpd.ytmusic` or `xmpd.cookie_extract` remain).
- [ ] `pytest tests/test_providers_ytmusic.py -v` runs the 4 scaffold tests and all pass.
- [ ] `grep -rn "ytmpd_cookies_" xmpd/ tests/` returns zero lines.
- [ ] `git log --oneline --diff-filter=R` shows the two renames as renames (similarity score reported).
- [ ] Single commit covers all of the above.
- [ ] `ruff check xmpd/ tests/` passes (no new lint errors).

---

## Testing Requirements

- **`pytest -q`**: full suite, must pass. This is the primary signal that import paths were rewritten correctly across every consumer.
- **`pytest tests/test_providers_ytmusic.py -v`**: confirms the four new scaffold tests run. Expected:
  - `test_ytmusic_provider_name` PASSED
  - `test_ytmusic_provider_is_enabled` PASSED
  - `test_ytmusic_provider_is_authenticated_returns_bool` PASSED
  - `test_ytmusic_provider_isinstance_protocol_partial` PASSED (asserts isinstance is currently False)
- **Manual smoke imports**: the four `python -c` lines in Step 10. Each must exit 0.
- **`ruff check xmpd/ tests/`**: lint clean. The two relocated files were already clean before; rename should not introduce regressions.
- **`mypy xmpd/`** (best effort): may flag the `registry["yt"] = YTMusicProvider(...)` line as an incompatible assignment because Phase 3 has not implemented the full Protocol. If the project's CI gate requires green mypy, apply `# type: ignore[assignment]` with the inline comment `# Phase 3 completes Provider Protocol surface`. Do NOT broaden the ignore.

No new tests for `FirefoxCookieExtractor` are needed -- the existing `tests/test_cookie_extract.py` tests the renamed module after the sed pass; Phase 2 ships them unchanged in behavior.

---

## External Interfaces Consumed

This phase consumes no external interfaces. It is a pure refactor: file moves, import rewrites, scaffold class declaration, registry wiring, and a four-test scaffold file. No HTTP, no library response shapes, no file formats, no DB schemas. Section omitted.

---

## Notes

- **Why `git mv` (not `mv`)**: `git mv old new` stages `old` as deleted and `new` as added, then git's similarity heuristic at commit time pairs them and reports a rename. Plain `mv old new` followed by `git add -A` produces the same staged result, but only if no other change hits the file. Since we ALSO add `YTMusicProvider` to the relocated `xmpd/providers/ytmusic.py`, the diff exceeds 50% similarity threshold... usually. Using `git mv` is the safer guarantee.

- **Why a single commit**: rename detection works per-commit. Splitting the rename and the scaffold class into two commits would still report the rename, but log archeology (`git log --follow xmpd/providers/ytmusic.py`) is cleaner with one commit.

- **Why keep the local `Playlist` / `Track` dataclasses**: they are used internally by `YTMusicClient` methods (e.g. `get_user_playlists()` returns `list[Playlist]`). Phase 3 wraps `YTMusicClient` and converts the local types to the new shared `xmpd.providers.base.Track` / `xmpd.providers.base.Playlist` at the provider boundary. Keeping the local types untouched in Phase 2 means Phase 3's diff is small and surgical.

- **The `isinstance(p, Provider) is False` assertion is intentional**: Phase 2 leaves the Protocol intentionally unfulfilled. Documenting this in a test makes the Phase 3 transition explicit -- when Phase 3 lands and the assertion flips to `True`, the test author there will update this single line and reuse the rest of the scaffold.

- **Pre-Phase-2 leftovers to clean opportunistically** (NOT mandatory; only mention in your phase summary if you also fix them):
  - `tests/test_xmpd_status_cli.py` has internal var names `_ytmpd_status_code`, `ytmpd_status`. Cosmetic test-only introspection; can be cleaned any time, but is not part of Phase 2's contract.

- **Watch the `xmpd/auth/__init__.py` file**: Phase 1 created it. Verify it's an empty package marker (or has only a docstring) before relocating `cookie_extract.py` into the package -- if Phase 1 added imports that your relocated module breaks, surface as a blocker.

- **Logging deliverable carry-over from Phase 1**: nothing new to do. The relocated `xmpd/providers/ytmusic.py` and `xmpd/auth/ytmusic_cookie.py` already have `logger = logging.getLogger(__name__)` at module top -- the rename automatically picks up the new dotted name (`xmpd.providers.ytmusic` / `xmpd.auth.ytmusic_cookie`) which is exactly what we want.

- **xmpctl, daemon entry point, systemd unit, install scripts**: all untouched in this phase. They reference `from xmpd.daemon import XMPDaemon` and similar package-level imports, which are unaffected by the YT-internal relocation.
