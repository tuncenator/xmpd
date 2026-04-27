# Phase 9: Tidal foundation (tidalapi dep, OAuth, scaffold)

**Feature**: tidal-init
**Estimated Context Budget**: ~50k tokens

**Difficulty**: medium

**Execution Mode**: sequential
**Batch**: 6

---

## Objective

Lay the dependency, auth, and class-skeleton groundwork for Tidal as a second provider. Three independent pieces, all in one phase because they share `tidalapi` as a new dependency:

1. Add `tidalapi>=0.8.11,<0.9` to `pyproject.toml` and install it.
2. Implement `xmpd/auth/tidal_oauth.py` -- the OAuth device flow, JSON token persistence, and a clipboard helper. This module is the single source of truth for "load / save / refresh a Tidal session."
3. Scaffold `xmpd/providers/tidal.py` -- only `name`, `is_enabled()`, `is_authenticated()`, and `_ensure_session()` are implemented. The 12 remaining `Provider` Protocol methods raise `NotImplementedError("Phase 10")`. Wire the tidal branch into `build_registry`.

Phase 10 fills the 12 method bodies. Phase 11 wires `xmpctl auth tidal` to call `run_oauth_flow`. Phase 9's job is to produce a stable, importable foundation with a working OAuth flow that the user can run live and persist a session for.

This is a medium phase. The Sonnet 4.6 coder should treat the requirements below as a literal spec.

---

## Deliverables

1. **`pyproject.toml`** -- add `tidalapi>=0.8.11,<0.9` to `[project] dependencies`, with a comment explaining the version pin (unofficial library, tightened upper bound). Reinstall the editable package.

2. **`xmpd/exceptions.py`** -- add `class TidalAuthRequired(XMPDError)` with a docstring documenting when it is raised.

3. **`xmpd/auth/tidal_oauth.py`** (NEW) -- public functions:
   - `run_oauth_flow(session_path: Path, fn_print: Callable[[str], None] = print) -> tidalapi.Session`
   - `load_session(session_path: Path) -> tidalapi.Session | None`
   - `save_session(session: tidalapi.Session, session_path: Path) -> None`
   - `_copy_to_clipboard(url: str) -> bool` -- private helper, never raises.

4. **`xmpd/providers/tidal.py`** (NEW) -- `TidalProvider` class with `name`, `is_enabled`, `is_authenticated`, `_ensure_session` implemented; the 12 Protocol methods raise `NotImplementedError("Phase 10")`.

5. **`xmpd/providers/__init__.py`** -- extend `build_registry` to construct `TidalProvider` when the tidal config block is enabled.

6. **`tests/test_tidal_oauth.py`** (NEW) -- unit tests for save/load/clipboard helpers. All tests use mocks for `tidalapi.Session` and `subprocess.run`; no live network calls.

7. **`tests/test_providers_tidal_scaffold.py`** (NEW) -- unit tests for the scaffold class: name, is_enabled, is_authenticated false-when-no-session, registry construction, all 12 stub methods raise `NotImplementedError`.

8. **Live verification** -- run `run_oauth_flow()` against the user's actual Tidal account once, persist the session, then load it back via `load_session()` and confirm `check_login()` returns True. Capture the session JSON shape (with tokens REDACTED) and paste into the phase summary's Evidence Captured.

---

## Detailed Requirements

### File ownership

This phase owns:

- `pyproject.toml` (single line addition + comment).
- `xmpd/exceptions.py` (single class addition).
- `xmpd/auth/tidal_oauth.py` (new file).
- `xmpd/providers/tidal.py` (new file).
- `xmpd/providers/__init__.py` (add the tidal branch in `build_registry`; do NOT touch the yt branch -- Phase 8 owns it).
- `tests/test_tidal_oauth.py` (new).
- `tests/test_providers_tidal_scaffold.py` (new).

This phase does NOT touch:

- `bin/xmpctl` -- Phase 11 wires `xmpctl auth tidal` to call `run_oauth_flow`. Phase 8 created the placeholder; do not modify it here.
- `xmpd/daemon.py` -- Phase 8's daemon registry-wiring covers daemon-side concerns. Phase 9's daemon-relevant work is purely additive: `build_registry` now knows about tidal, but the daemon's call site is unchanged.
- `examples/config.yaml` -- Phase 11 owns the config-shape rewrite.
- The 12 `TidalProvider` Protocol methods' bodies -- those are Phase 10.

### 1. `pyproject.toml`

Locate the `dependencies = [` block in `[project]`. Add `tidalapi` as a new entry. Final dependencies block:

```toml
dependencies = [
    "ytmusicapi>=1.0.0",
    "pyyaml>=6.0",
    "python-mpd2>=3.1.0",
    "yt-dlp>=2023.0.0",
    "aiohttp>=3.9.0",
    # Unofficial Tidal client. Pin to a known-good range; bump if Tidal's
    # API changes break us. PKCE flow not used (HiRes deferred per
    # PROJECT_PLAN.md Cross-Cutting Concerns > Tidal HiRes Streaming).
    "tidalapi>=0.8.11,<0.9",
]
```

Then install:

```bash
uv pip install -e '.[dev]'
python -c "import tidalapi; print(tidalapi.__version__)"
```

The version printed must be in the `0.8.x` range (>= 0.8.11 and < 0.9).

### 2. `xmpd/exceptions.py`

Append to the bottom of the file:

```python
class TidalAuthRequired(XMPDError):
    """Raised when the Tidal session cannot be loaded or has expired without a refresh path.

    The daemon catches this at the provider boundary and converts it to a
    warn-and-skip per the spec; the CLI surfaces it to the user with a hint
    to run ``xmpctl auth tidal``.
    """

    pass
```

`TidalAuthRequired` MUST inherit from `XMPDError` (not `YTMusicAuthError`, not `Exception`). The naming mirrors `YTMusicAuthError` but it is a distinct subclass.

### 3. `xmpd/auth/tidal_oauth.py` (NEW)

#### Imports and module setup

```python
"""Tidal OAuth device flow + token persistence.

This module is the single source of truth for "load / save / refresh a Tidal
session." It is invoked from two call sites:

1. ``xmpctl auth tidal`` (Phase 11) -- runs the interactive device flow.
2. ``TidalProvider._ensure_session()`` -- loads a persisted session for
   non-interactive use; raises ``TidalAuthRequired`` if missing or invalid.

Daemon-mode behavior: never block on input. If no token exists, the daemon
warns and skips Tidal sync. Interactive setup runs only via the CLI.

OAuth (device flow) is used, NOT PKCE. Per PROJECT_PLAN.md Cross-Cutting
Concerns > Tidal HiRes Streaming Constraint, the effective ceiling for this
iteration is LOSSLESS (16-bit/44.1 kHz FLAC). PKCE complications (required
for HI_RES_LOSSLESS) are deferred.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import tidalapi
from tidalapi.media import Quality

from xmpd.exceptions import TidalAuthRequired

logger = logging.getLogger(__name__)
```

#### `run_oauth_flow`

```python
def run_oauth_flow(
    session_path: Path,
    fn_print: Callable[[str], None] = print,
) -> tidalapi.Session:
    """Initiate the OAuth device flow.

    1. Construct a ``tidalapi.Session()``.
    2. Set ``session.config.quality = Quality.high_lossless`` (effective
       LOSSLESS ceiling per project policy).
    3. Call ``session.login_oauth()`` which returns a tuple ``(LinkLogin,
       concurrent.futures.Future)``.
    4. Compute the verification URL: ``"https://" + link.verification_uri_complete``.
       (The ``LinkLogin`` field returns the URL without the scheme.)
    5. Print the URL via ``fn_print`` so the user sees it on stdout regardless
       of clipboard support. Try ``_copy_to_clipboard(url)``; on success print
       "URL copied to clipboard.", on failure print
       "Open this URL in your browser: <url>" and instruct the user to install
       wl-copy or xclip if they want auto-copy.
    6. Block on ``future.result()`` (no timeout argument; tidalapi handles
       expiry of the device-code internally and the future raises if the
       device-code expires before the user authorizes).
    7. On success, call ``save_session(session, session_path)`` and return the
       session.
    8. On any exception from ``future.result()``, raise ``TidalAuthRequired``
       chained from the original (``raise TidalAuthRequired(...) from e``).

    Args:
        session_path: where to write the persisted JSON. Parent directory is
            created if missing.
        fn_print: print sink, defaults to builtin ``print``. Tests inject a
            list-appender to capture output.

    Returns:
        A ``tidalapi.Session`` ready for use.

    Raises:
        TidalAuthRequired: on device-code timeout or any failure in the
            ``login_oauth`` future.
    """
    session = tidalapi.Session()
    session.config.quality = Quality.high_lossless

    link, future = session.login_oauth()
    url = f"https://{link.verification_uri_complete}"

    fn_print(f"Open this URL in your browser to authorize xmpd:")
    fn_print(f"  {url}")
    if _copy_to_clipboard(url):
        fn_print("URL copied to clipboard.")
    else:
        fn_print("(Install wl-copy or xclip for automatic clipboard support.)")
    fn_print("")
    fn_print("Waiting for authorization...")

    try:
        future.result()
    except Exception as e:
        raise TidalAuthRequired(f"Tidal OAuth flow failed: {e}") from e

    save_session(session, session_path)
    fn_print(f"Tidal session saved to {session_path}.")
    return session
```

Notes:

- Do NOT pass a timeout to `future.result()`. The tidalapi library handles device-code expiry on its side; the user is told "Waiting for authorization..." and either authorizes (future resolves) or doesn't (future raises with a message about expiry that gets wrapped in `TidalAuthRequired`).
- The print sequence (URL first, then clipboard message, then "Waiting...") is intentional. Tests verify the URL is always printed regardless of clipboard outcome.
- The "https://" prefix is load-bearing. `link.verification_uri_complete` returns something like `link.tidal.com/ABCDE` (no scheme). MPD-style URL parsers and most browsers tolerate the schemeless form pasted in, but copying the schemed form is more user-friendly.

#### `load_session`

```python
def load_session(session_path: Path) -> tidalapi.Session | None:
    """Load a persisted Tidal session from JSON.

    1. If ``session_path`` does not exist, return ``None``.
    2. Read and parse JSON. On ``json.JSONDecodeError`` or ``OSError``, log
       at WARNING and return ``None``.
    3. Construct ``tidalapi.Session()`` and set
       ``session.config.quality = Quality.high_lossless``.
    4. Call ``session.load_oauth_session(token_type, access_token,
       refresh_token, expiry_time)``. (Note: tidalapi 0.8.x's
       ``load_oauth_session`` does NOT take ``is_pkce``; that field is
       persisted by us for forward compatibility but ignored on load.)
    5. Validate via ``session.check_login()``. If False, return ``None``.
       (Do NOT raise -- the caller treats ``None`` as "not authenticated"
       and decides whether to warn-and-skip or raise ``TidalAuthRequired``.)
    6. Return the validated session.

    Args:
        session_path: path to ``~/.config/xmpd/tidal_session.json`` (or test
            override).

    Returns:
        A loaded, validated ``tidalapi.Session``, or ``None`` if no valid
        session exists at the given path.
    """
    if not session_path.is_file():
        return None

    try:
        data = json.loads(session_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read Tidal session at %s: %s", session_path, e)
        return None

    session = tidalapi.Session()
    session.config.quality = Quality.high_lossless

    try:
        session.load_oauth_session(
            data["token_type"],
            data["access_token"],
            data["refresh_token"],
            data["expiry_time"],
        )
    except (KeyError, Exception) as e:
        logger.warning("Failed to load Tidal session: %s", type(e).__name__)
        return None

    if not session.check_login():
        logger.info("Tidal session at %s exists but check_login() returned False", session_path)
        return None

    return session
```

Edge cases the tests must cover:

- File missing: `load_session(Path("/nonexistent")) is None`.
- File present but malformed JSON: returns `None`, logs warning.
- File present, valid JSON, but `check_login()` returns False: returns `None`.
- File present, valid, `check_login()` True: returns the session.

#### `save_session`

```python
def save_session(session: tidalapi.Session, session_path: Path) -> None:
    """Persist a Tidal session to JSON.

    Writes the JSON shape from PROJECT_PLAN.md "Tidal session JSON" with
    file mode 0600 (user read/write only). The parent directory is created
    if missing (mode 0700).

    Note: tidalapi exposes ``save_session_to_file`` but it does NOT persist
    ``expiry_time``; we roll our own to capture it. The ``is_pkce`` field is
    persisted for forward compatibility (Phase 9 always writes False because
    we use the device flow).

    JSON shape:

    .. code-block:: json

        {
          "token_type": "Bearer",
          "access_token": "...",
          "refresh_token": "...",
          "expiry_time": "2026-05-04T12:34:56",
          "is_pkce": false
        }
    """
    session_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    payload = {
        "token_type": session.token_type,
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "expiry_time": session.expiry_time.isoformat() if session.expiry_time else None,
        "is_pkce": False,
    }
    # Write to a temp file first, fchmod, then rename, to avoid leaving a
    # world-readable file even momentarily.
    tmp = session_path.with_suffix(session_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, session_path)
    logger.info("Tidal session persisted to %s", session_path)
```

Edge cases:

- `session.expiry_time` may be a `datetime.datetime` object; calling `.isoformat()` produces an ISO-8601 string. Defensive `if session.expiry_time` handles the unlikely None case.
- The atomic rename (write-tmp + chmod + replace) prevents a window where the file is world-readable.
- Mode 0600 verified by the test via `Path(session_path).stat().st_mode & 0o777 == 0o600`.

#### `_copy_to_clipboard`

```python
def _copy_to_clipboard(url: str) -> bool:
    """Try wl-copy (Wayland) -> xclip (X11) -> return False on no clipboard tool.

    Subprocess call; never crashes; never raises. Returns ``True`` if a
    clipboard write succeeded.

    Detection order:
    1. If ``$WAYLAND_DISPLAY`` is set AND ``wl-copy`` is on PATH: try wl-copy.
    2. Else if ``$DISPLAY`` is set AND ``xclip`` is on PATH: try xclip with
       ``-selection clipboard``.
    3. Else: return False.

    A subprocess that fails (non-zero exit, missing binary at exec time, etc.)
    returns False without raising.
    """
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        try:
            subprocess.run(
                ["wl-copy"],
                input=url.encode(),
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            return False

    if os.environ.get("DISPLAY") and shutil.which("xclip"):
        try:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=url.encode(),
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            return False

    return False
```

Edge cases:

- Both `$WAYLAND_DISPLAY` and `$DISPLAY` set (XWayland): prefer Wayland.
- Headless (neither set): return False.
- Tool present but fails (e.g. clipboard locked): return False, do not raise.
- Tool exec'd to a missing path: caught by FileNotFoundError.

### 4. `xmpd/providers/tidal.py` (NEW)

```python
"""Tidal provider implementation.

Phase 9 scaffolds this class with auth wiring (name, is_enabled,
is_authenticated, _ensure_session). Phase 10 implements the 12 Provider
Protocol methods. Until Phase 10 lands, every method except is_enabled and
is_authenticated raises ``NotImplementedError("Phase 10")``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from xmpd.exceptions import TidalAuthRequired
from xmpd.providers.base import Playlist, Track, TrackMetadata

logger = logging.getLogger(__name__)


class TidalProvider:
    """Provider implementation for Tidal HiFi.

    Wraps a ``tidalapi.Session`` to satisfy the ``xmpd.providers.base.Provider``
    Protocol. Auth is handled lazily: the session is loaded the first time a
    method needs it, and ``TidalAuthRequired`` is raised if the persisted
    session is missing or invalid.
    """

    name = "tidal"

    SESSION_PATH = Path("~/.config/xmpd/tidal_session.json").expanduser()

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        # Late binding: we don't import tidalapi.Session here to keep the
        # scaffold importable without the dep loaded at class-definition time.
        self._session: Any = None  # tidalapi.Session, lazily loaded
        # Reserved for Phase 10's get_like_state caching.
        self._favorites_ids: set[str] | None = None

    def is_enabled(self) -> bool:
        return bool(self._config.get("enabled", False))

    def is_authenticated(self) -> bool:
        """Check by attempting to load the persisted session and validate it.

        Returns False (does NOT raise) when:
        - The session file is missing.
        - The session file is unparseable.
        - The persisted tokens fail ``check_login()``.

        The daemon uses this for warn-and-skip; the CLI uses it to decide
        whether to invoke ``run_oauth_flow``.
        """
        if not self.SESSION_PATH.is_file():
            return False
        from xmpd.auth.tidal_oauth import load_session
        session = load_session(self.SESSION_PATH)
        return session is not None

    def _ensure_session(self) -> Any:
        """Lazy-load and validate the session; raise TidalAuthRequired if unavailable.

        Cached: the second call returns the same session object.

        Raises:
            TidalAuthRequired: if the persisted session is missing, malformed,
                or fails ``check_login()``.
        """
        if self._session is None:
            from xmpd.auth.tidal_oauth import load_session
            self._session = load_session(self.SESSION_PATH)
            if self._session is None:
                raise TidalAuthRequired(
                    "Tidal session missing or invalid. Run `xmpctl auth tidal`."
                )
        return self._session

    # ----------------------------------------------------------------------
    # Phase 10 implements all of these. Until then they MUST raise
    # NotImplementedError so any accidental dispatch surfaces immediately
    # rather than silently no-op'ing.
    # ----------------------------------------------------------------------

    def list_playlists(self) -> list[Playlist]:
        raise NotImplementedError("Phase 10")

    def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        raise NotImplementedError("Phase 10")

    def get_favorites(self) -> list[Track]:
        raise NotImplementedError("Phase 10")

    def resolve_stream(self, track_id: str) -> str:
        raise NotImplementedError("Phase 10")

    def get_track_metadata(self, track_id: str) -> TrackMetadata:
        raise NotImplementedError("Phase 10")

    def search(self, query: str, limit: int = 25) -> list[Track]:
        raise NotImplementedError("Phase 10")

    def get_radio(self, seed_track_id: str, limit: int = 25) -> list[Track]:
        raise NotImplementedError("Phase 10")

    def like(self, track_id: str) -> None:
        raise NotImplementedError("Phase 10")

    def dislike(self, track_id: str) -> None:
        raise NotImplementedError("Phase 10")

    def unlike(self, track_id: str) -> None:
        raise NotImplementedError("Phase 10")

    def get_like_state(self, track_id: str) -> bool:
        raise NotImplementedError("Phase 10")

    def report_play(self, track_id: str, duration_seconds: int) -> None:
        raise NotImplementedError("Phase 10")
```

Notes for the coder:

- The `_session: Any` annotation (rather than `tidalapi.Session | None`) is intentional. It keeps mypy happy without forcing tidalapi to be imported at class-definition time. Phase 10 may tighten this once all method bodies are filled in.
- The order of the Phase-10-stub methods follows the order in `xmpd/providers/base.py`'s Protocol declaration. Match that order exactly so a side-by-side review is mechanical.
- The stub methods MUST take their full signatures (including default values), not just `*args, **kwargs`. This locks in the shape so Phase 10 only fills in bodies.

### 5. `xmpd/providers/__init__.py`

This phase ADDs the tidal branch in `build_registry`. Find the function (Phase 1 created the skeleton; Phase 2 added the `yt` branch). Add the tidal branch immediately after the yt branch:

```python
def build_registry(config: dict[str, Any]) -> dict[str, Provider]:
    """..."""
    registry: dict[str, Provider] = {}
    enabled = get_enabled_provider_names(config)

    if "yt" in enabled:
        # ... existing yt branch from Phase 2 ...

    if "tidal" in enabled:
        from xmpd.providers.tidal import TidalProvider
        registry["tidal"] = TidalProvider(config["tidal"])

    return registry
```

Two important constraints:

- **Lazy import**: import `TidalProvider` *inside* the `if "tidal" in enabled` branch, not at module top. This keeps `from xmpd.providers import build_registry` working even if `tidalapi` fails to import on a system where the user has not installed the Tidal extras.
- **Do NOT modify the yt branch.** Phase 8 owns the yt branch's daemon-aware shape. If Phase 8's branch construction looks different from a naive `YTMusicProvider(config["yt"])`, mirror its style for tidal but do not edit the yt code path.

If `get_enabled_provider_names` does not exist yet (some sibling phase may rename it), use whatever helper Phase 8 introduced. Read `xmpd/providers/__init__.py` before editing to confirm the current shape.

### 6. Tests

#### `tests/test_tidal_oauth.py` (NEW)

```python
"""Unit tests for xmpd.auth.tidal_oauth.

All tests use mocks for tidalapi.Session and subprocess.run; no live network
calls. Live verification happens separately via the OAuth flow against the
user's actual Tidal account.
"""

import json
import os
import stat
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xmpd.auth.tidal_oauth import (
    _copy_to_clipboard,
    load_session,
    run_oauth_flow,
    save_session,
)
from xmpd.exceptions import TidalAuthRequired


def _make_fake_session(
    *,
    token_type: str = "Bearer",
    access_token: str = "AT-TOKEN-PLACEHOLDER",
    refresh_token: str = "RT-TOKEN-PLACEHOLDER",
    expiry: datetime | None = None,
    check_login: bool = True,
) -> MagicMock:
    """Build a MagicMock that quacks like a tidalapi.Session for save/load."""
    s = MagicMock()
    s.token_type = token_type
    s.access_token = access_token
    s.refresh_token = refresh_token
    s.expiry_time = expiry or (datetime.utcnow() + timedelta(days=30))
    s.check_login.return_value = check_login
    return s


# ---------- save_session ----------

def test_save_session_writes_correct_json_shape(tmp_path: Path) -> None:
    s = _make_fake_session(
        access_token="AT-PLACEHOLDER",
        refresh_token="RT-PLACEHOLDER",
        expiry=datetime(2026, 5, 4, 12, 34, 56),
    )
    target = tmp_path / "tidal_session.json"
    save_session(s, target)

    assert target.is_file()
    data = json.loads(target.read_text())
    assert data == {
        "token_type": "Bearer",
        "access_token": "AT-PLACEHOLDER",
        "refresh_token": "RT-PLACEHOLDER",
        "expiry_time": "2026-05-04T12:34:56",
        "is_pkce": False,
    }


def test_save_session_writes_mode_0600(tmp_path: Path) -> None:
    s = _make_fake_session()
    target = tmp_path / "tidal_session.json"
    save_session(s, target)

    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_save_session_creates_parent_dir(tmp_path: Path) -> None:
    s = _make_fake_session()
    target = tmp_path / "nested" / "deeper" / "tidal_session.json"
    save_session(s, target)
    assert target.is_file()


# ---------- load_session ----------

def test_load_session_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_session(tmp_path / "does_not_exist.json") is None


def test_load_session_returns_none_when_unparseable(tmp_path: Path) -> None:
    target = tmp_path / "garbage.json"
    target.write_text("not valid json {{{")
    assert load_session(target) is None


def test_load_session_returns_none_when_check_login_false(tmp_path: Path) -> None:
    target = tmp_path / "tidal_session.json"
    target.write_text(json.dumps({
        "token_type": "Bearer",
        "access_token": "AT",
        "refresh_token": "RT",
        "expiry_time": "2026-05-04T12:34:56",
        "is_pkce": False,
    }))
    fake = MagicMock()
    fake.check_login.return_value = False
    with patch("xmpd.auth.tidal_oauth.tidalapi.Session", return_value=fake):
        assert load_session(target) is None


def test_load_session_returns_session_when_check_login_true(tmp_path: Path) -> None:
    target = tmp_path / "tidal_session.json"
    target.write_text(json.dumps({
        "token_type": "Bearer",
        "access_token": "AT",
        "refresh_token": "RT",
        "expiry_time": "2026-05-04T12:34:56",
        "is_pkce": False,
    }))
    fake = MagicMock()
    fake.check_login.return_value = True
    with patch("xmpd.auth.tidal_oauth.tidalapi.Session", return_value=fake):
        result = load_session(target)
    assert result is fake
    fake.load_oauth_session.assert_called_once_with(
        "Bearer", "AT", "RT", "2026-05-04T12:34:56"
    )


# ---------- _copy_to_clipboard ----------

def test_copy_to_clipboard_uses_wl_copy_when_wayland(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    with patch("xmpd.auth.tidal_oauth.shutil.which", side_effect=lambda x: "/usr/bin/wl-copy" if x == "wl-copy" else None):
        with patch("xmpd.auth.tidal_oauth.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert _copy_to_clipboard("https://link.tidal.com/ABCDE") is True
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args == ["wl-copy"]


def test_copy_to_clipboard_uses_xclip_when_x11(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    with patch("xmpd.auth.tidal_oauth.shutil.which", side_effect=lambda x: "/usr/bin/xclip" if x == "xclip" else None):
        with patch("xmpd.auth.tidal_oauth.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert _copy_to_clipboard("https://link.tidal.com/ABCDE") is True
            args = mock_run.call_args[0][0]
            assert args == ["xclip", "-selection", "clipboard"]


def test_copy_to_clipboard_returns_false_when_no_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    with patch("xmpd.auth.tidal_oauth.shutil.which", return_value=None):
        assert _copy_to_clipboard("https://link.tidal.com/ABCDE") is False


def test_copy_to_clipboard_returns_false_on_subprocess_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess as sp
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    with patch("xmpd.auth.tidal_oauth.shutil.which", return_value="/usr/bin/wl-copy"):
        with patch("xmpd.auth.tidal_oauth.subprocess.run", side_effect=sp.CalledProcessError(1, "wl-copy")):
            assert _copy_to_clipboard("x") is False


# ---------- run_oauth_flow (with mocked tidalapi) ----------

def test_run_oauth_flow_persists_session_on_success(tmp_path: Path) -> None:
    target = tmp_path / "tidal_session.json"

    fake_session = _make_fake_session(
        access_token="AT", refresh_token="RT",
        expiry=datetime(2026, 5, 4, 12, 34, 56),
    )
    fake_link = MagicMock()
    fake_link.verification_uri_complete = "link.tidal.com/ABCDE"
    fake_future = MagicMock()
    fake_future.result.return_value = None
    fake_session.login_oauth.return_value = (fake_link, fake_future)

    captured: list[str] = []
    with patch("xmpd.auth.tidal_oauth.tidalapi.Session", return_value=fake_session):
        with patch("xmpd.auth.tidal_oauth._copy_to_clipboard", return_value=True):
            result = run_oauth_flow(target, fn_print=captured.append)

    assert result is fake_session
    assert target.is_file()
    out = "\n".join(captured)
    assert "https://link.tidal.com/ABCDE" in out
    assert "URL copied to clipboard." in out


def test_run_oauth_flow_raises_tidal_auth_required_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "tidal_session.json"

    fake_session = _make_fake_session()
    fake_link = MagicMock()
    fake_link.verification_uri_complete = "link.tidal.com/ABCDE"
    fake_future = MagicMock()
    fake_future.result.side_effect = RuntimeError("device-code expired")
    fake_session.login_oauth.return_value = (fake_link, fake_future)

    with patch("xmpd.auth.tidal_oauth.tidalapi.Session", return_value=fake_session):
        with patch("xmpd.auth.tidal_oauth._copy_to_clipboard", return_value=False):
            with pytest.raises(TidalAuthRequired) as excinfo:
                run_oauth_flow(target, fn_print=lambda _s: None)

    assert "device-code expired" in str(excinfo.value)
    assert not target.is_file()  # nothing persisted on failure
```

#### `tests/test_providers_tidal_scaffold.py` (NEW)

```python
"""Unit tests for the Phase 9 TidalProvider scaffold.

Verifies the scaffold-only methods (name, is_enabled, is_authenticated,
_ensure_session). Verifies the 12 Phase-10 stub methods raise
NotImplementedError. Verifies build_registry constructs TidalProvider when
the tidal config block is enabled.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from xmpd.exceptions import TidalAuthRequired
from xmpd.providers import build_registry
from xmpd.providers.tidal import TidalProvider


def test_tidal_provider_name() -> None:
    assert TidalProvider({}).name == "tidal"


def test_tidal_provider_is_enabled_true() -> None:
    assert TidalProvider({"enabled": True}).is_enabled() is True


def test_tidal_provider_is_enabled_false() -> None:
    assert TidalProvider({}).is_enabled() is False
    assert TidalProvider({"enabled": False}).is_enabled() is False


def test_tidal_provider_is_authenticated_false_when_no_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(TidalProvider, "SESSION_PATH", tmp_path / "nonexistent.json")
    p = TidalProvider({"enabled": True})
    assert p.is_authenticated() is False


def test_tidal_provider_ensure_session_raises_when_no_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(TidalProvider, "SESSION_PATH", tmp_path / "nonexistent.json")
    p = TidalProvider({"enabled": True})
    with pytest.raises(TidalAuthRequired):
        p._ensure_session()


@pytest.mark.parametrize(
    "method_name, args",
    [
        ("list_playlists", ()),
        ("get_playlist_tracks", ("p1",)),
        ("get_favorites", ()),
        ("resolve_stream", ("12345",)),
        ("get_track_metadata", ("12345",)),
        ("search", ("query",)),
        ("get_radio", ("12345",)),
        ("like", ("12345",)),
        ("dislike", ("12345",)),
        ("unlike", ("12345",)),
        ("get_like_state", ("12345",)),
        ("report_play", ("12345", 60)),
    ],
)
def test_tidal_provider_phase10_stubs_raise(method_name: str, args: tuple) -> None:
    p = TidalProvider({"enabled": True})
    method = getattr(p, method_name)
    with pytest.raises(NotImplementedError) as excinfo:
        method(*args)
    assert "Phase 10" in str(excinfo.value)


def test_build_registry_constructs_tidal_when_enabled() -> None:
    config = {"tidal": {"enabled": True}}
    # If your project's get_enabled_provider_names also requires yt config to
    # be present, add a minimal one. Phase 9's TidalProvider construction
    # must not depend on yt's presence.
    registry = build_registry(config)
    assert "tidal" in registry
    assert isinstance(registry["tidal"], TidalProvider)
    assert registry["tidal"].name == "tidal"


def test_build_registry_skips_tidal_when_disabled() -> None:
    config = {"tidal": {"enabled": False}}
    registry = build_registry(config)
    assert "tidal" not in registry
```

### 7. Live verification

After all unit tests pass, perform the live OAuth flow against the user's actual Tidal account. This is the only way to confirm that the JSON shape, `LinkLogin` field names, and `Session.token_type` / `access_token` / `refresh_token` / `expiry_time` attributes match what tidalapi 0.8.x actually emits.

Step-by-step:

1. **Confirm dependency is installed**: `python -c "import tidalapi; print(tidalapi.__version__)"`. Must print a version `>= 0.8.11, < 0.9`.
2. **Run the OAuth flow manually**:

   ```bash
   python - <<'EOF'
   from pathlib import Path
   from xmpd.auth.tidal_oauth import run_oauth_flow
   p = Path("~/.config/xmpd/tidal_session.json").expanduser()
   p.parent.mkdir(parents=True, exist_ok=True)
   session = run_oauth_flow(p)
   print("OK; expiry:", session.expiry_time)
   EOF
   ```

   Open the printed URL in a browser, authorize, and wait for the script to finish. The expected output is:

   - "Open this URL in your browser to authorize xmpd:"
   - "  https://link.tidal.com/XXXXX"
   - "URL copied to clipboard." (or the install-helpers message if neither wl-copy nor xclip is present)
   - "Waiting for authorization..."
   - "Tidal session saved to /home/tunc/.config/xmpd/tidal_session.json."
   - "OK; expiry: 2026-..."

3. **Verify the file**:

   ```bash
   ls -l ~/.config/xmpd/tidal_session.json   # mode -rw------- (0600)
   python -c "import json; d=json.load(open('/home/tunc/.config/xmpd/tidal_session.json')); print({k: (v[:20]+'...' if isinstance(v,str) and len(v)>20 else v) for k,v in d.items()})"
   ```

   Expected: prints a dict with keys `token_type`, `access_token` (truncated), `refresh_token` (truncated), `expiry_time` (ISO-8601), `is_pkce`. Paste the truncated dict into Evidence Captured.

4. **Round-trip via load_session**:

   ```bash
   python - <<'EOF'
   from pathlib import Path
   from xmpd.auth.tidal_oauth import load_session
   p = Path("~/.config/xmpd/tidal_session.json").expanduser()
   s = load_session(p)
   print("loaded:", s is not None)
   print("check_login:", s.check_login() if s else None)
   print("user.id:", s.user.id if s else None)
   EOF
   ```

   Expected: `loaded: True`, `check_login: True`, `user.id: <some integer>`.

5. **Token redaction discipline**: never print the full `access_token` or `refresh_token`. Use `[:20] + "..."` truncation. The pre-commit hook would catch a verbatim token slipping into a commit, but stdout/logs in your phase summary must also be safe.

---

## Dependencies

**Requires**:

- Phase 1: `xmpd/providers/base.py` defines `Provider`, `Track`, `TrackMetadata`, `Playlist`. These are imported into `xmpd/providers/tidal.py`.
- Phase 1: `xmpd/auth/` package directory exists.
- Phase 8: `xmpd/providers/__init__.py`'s `build_registry` understands the daemon-aware multi-provider shape; `bin/xmpctl auth tidal` placeholder prints a friendly "not yet implemented" message that Phase 11 replaces. Although Phase 9's OAuth is CLI-side (xmpctl auth tidal hits xmpd.auth.tidal_oauth directly, not the daemon socket), Phase 8's daemon must already understand the multi-provider registry shape so that flipping `tidal.enabled: true` works without further daemon changes.

**Enables**:

- Phase 10: TidalProvider methods. Phase 10 fills in the 12 stubs raising NotImplementedError today, using `self._ensure_session()` to access the validated session.
- Phase 11: `xmpctl auth tidal` end-to-end (CLI wires `run_oauth_flow` from `xmpd/auth/tidal_oauth.py`).

---

## Completion Criteria

- [ ] `tidalapi` imports cleanly: `python -c "import tidalapi; print(tidalapi.__version__)"` prints a version in `[0.8.11, 0.9)`.
- [ ] `pyproject.toml` lists `tidalapi>=0.8.11,<0.9` in `[project] dependencies` with the explanatory comment.
- [ ] `xmpd/exceptions.py` defines `class TidalAuthRequired(XMPDError)`.
- [ ] `xmpd/auth/tidal_oauth.py` exists with `run_oauth_flow`, `load_session`, `save_session`, `_copy_to_clipboard`.
- [ ] `xmpd/providers/tidal.py` exists with `TidalProvider` (name="tidal", `is_enabled`, `is_authenticated`, `_ensure_session` working; 12 Protocol methods raising `NotImplementedError("Phase 10")`).
- [ ] `xmpd/providers/__init__.py` `build_registry` constructs `TidalProvider` when `config["tidal"]["enabled"] is True` (lazy import inside the branch).
- [ ] `pytest -q tests/test_tidal_oauth.py tests/test_providers_tidal_scaffold.py` passes -- both new test files green.
- [ ] `pytest -q` (full suite) passes -- nothing regressed in YT-side or daemon tests.
- [ ] `mypy xmpd/auth/tidal_oauth.py xmpd/providers/tidal.py` passes (no errors). Project-wide `mypy xmpd/` does not need to pass in this phase, but the two new files must.
- [ ] `ruff check xmpd/auth/tidal_oauth.py xmpd/providers/tidal.py tests/test_tidal_oauth.py tests/test_providers_tidal_scaffold.py` passes.
- [ ] Live verification: OAuth flow run successfully against the user's actual Tidal account; `~/.config/xmpd/tidal_session.json` exists with mode `0600` and the expected JSON shape; `load_session()` returns a valid session and `check_login()` returns True.
- [ ] `xmpctl auth tidal` placeholder from Phase 8 still prints its "not yet implemented" message (Phase 11 will wire it).
- [ ] Phase summary `summaries/PHASE_09_SUMMARY.md` created with Evidence Captured (LinkLogin shape, persisted JSON shape with REDACTED tokens) and any deviations noted.

---

## Testing Requirements

### Unit tests

- All tests in `tests/test_tidal_oauth.py` and `tests/test_providers_tidal_scaffold.py` (listed above) MUST pass.
- Tests use `unittest.mock.MagicMock` and `unittest.mock.patch` for tidalapi and subprocess. No live network calls in any unit test.
- `pytest -q tests/test_tidal_oauth.py tests/test_providers_tidal_scaffold.py` is the per-phase verification command.
- `pytest -q` (full suite) MUST pass -- nothing the phase touches is permitted to regress YT-side tests.

### Live verification (in addition to unit tests)

Performed manually after unit tests pass:

1. Run `run_oauth_flow()` against the user's real Tidal account (see "Live verification" subsection above).
2. Verify `~/.config/xmpd/tidal_session.json` is created with mode 0600.
3. Verify `load_session()` returns a valid session and `check_login()` returns True.
4. Capture the JSON shape (REDACTED) into the phase summary.

### HARD GUARDRAIL

Phase 9 makes no like/unlike/favorites calls. The HARD GUARDRAIL (never destructively touch the user's Tidal favorites or playlists) applies but is structurally not at risk in this phase: only auth and class-scaffolding work. Phase 10 owns the favorites round-trip and is the first phase where the guardrail bites.

---

## Helpers Required

> No helpers required. Phase 9 is self-contained. The clipboard helper
> (`_copy_to_clipboard`) is a private function inside `xmpd/auth/tidal_oauth.py`,
> not a project-level shell script.

---

## External Interfaces Consumed

> The coder MUST observe each interface live (against the user's actual
> Tidal account) and paste the captured sample (with tokens REDACTED) into
> the phase summary's "Evidence Captured" section before writing the
> matching parser/saver. Without these samples the JSON shape is a guess.

- **`tidalapi.Session.login_oauth()` return value (`LinkLogin` object + `Future`)**
  - **Consumed by**: `xmpd/auth/tidal_oauth.py::run_oauth_flow`. Specifically, the code reads `link.verification_uri_complete`. If tidalapi 0.8.11 also exposes `link.expires_in` or `link.interval`, capture those for documentation purposes (the implementation does not currently use them).
  - **How to capture**: in a Python REPL with the editable install active:

    ```python
    import tidalapi
    s = tidalapi.Session()
    link, future = s.login_oauth()
    print("LinkLogin type:", type(link).__name__)
    print("verification_uri_complete:", link.verification_uri_complete)
    print("dir(link):", [a for a in dir(link) if not a.startswith("_")])
    # ... user authorizes in browser ...
    future.result()
    print("future done:", future.done())
    ```

  - **If not observable**: Phase 9 cannot proceed without a live OAuth flow against the user's account. If the user's Tidal subscription is unavailable, escalate to the user; do NOT fabricate the shape from documentation.

- **`tidalapi.Session` post-login attribute shape**
  - **Consumed by**: `xmpd/auth/tidal_oauth.py::save_session` (reads `token_type`, `access_token`, `refresh_token`, `expiry_time`).
  - **How to capture**: continuing the REPL session above:

    ```python
    print("token_type:", s.token_type)
    print("access_token (first 20):", s.access_token[:20] + "...")
    print("refresh_token (first 20):", s.refresh_token[:20] + "...")
    print("expiry_time:", s.expiry_time, type(s.expiry_time).__name__)
    ```

  - **Critical fields**: `token_type` (str, expected "Bearer"), `access_token` (str), `refresh_token` (str), `expiry_time` (`datetime.datetime` -- if it is a string in the installed version, adjust `save_session` to skip `.isoformat()`).
  - **NEVER print the full token values.** Use `[:20] + "..."` truncation in any captured output.

- **Persisted JSON shape at `~/.config/xmpd/tidal_session.json`**
  - **Consumed by**: `xmpd/auth/tidal_oauth.py::load_session` (reads back what `save_session` wrote).
  - **How to capture**: after a successful `run_oauth_flow`:

    ```bash
    python - <<'EOF'
    import json
    d = json.load(open('/home/tunc/.config/xmpd/tidal_session.json'))
    redacted = {k: (v[:20] + '...' if isinstance(v, str) and len(v) > 20 else v) for k, v in d.items()}
    print(json.dumps(redacted, indent=2))
    EOF
    ```

  - **Verify**: keys are exactly `{"token_type", "access_token", "refresh_token", "expiry_time", "is_pkce"}`. File mode is `-rw-------` (0600).

- **`tidalapi.Session.check_login()` return type**
  - **Consumed by**: `xmpd/auth/tidal_oauth.py::load_session` (boolean check).
  - **How to capture**: in the REPL after `load_oauth_session(...)`:

    ```python
    val = s.check_login()
    print("type:", type(val).__name__, "value:", val)
    ```

  - **Expected**: `bool` (True for valid session). If 0.8.11 returns a different truthy/falsy type, document it; the `if not session.check_login()` check still works for any falsy sentinel.

- **`tidalapi.media.Quality.high_lossless` enum value**
  - **Consumed by**: both `run_oauth_flow` and `load_session` set `session.config.quality` to this. Phase 10's `resolve_stream` will also reference quality enum values.
  - **How to capture**:

    ```python
    from tidalapi.media import Quality
    print("Quality members:", [q.name for q in Quality])
    print("high_lossless:", Quality.high_lossless)
    ```

  - **Expected**: a member named `high_lossless` exists. If 0.8.x renamed it (e.g. to `lossless` or `LOSSLESS`), update the constant import. Document the actual member names in Evidence Captured for Phase 10 to reference.

---

## Technical Reference

### tidalapi (Python, unofficial)

Primary library used by Phase 9 (auth) and Phase 10 (provider methods).

**Library/SDK**

- Package name: `tidalapi`. Version pin for this iteration: `>=0.8.11,<0.9`.
- Source: https://github.com/tamland/python-tidal (fork with active maintenance: https://github.com/tehkillerbee/tidalapi).
- Status: unofficial. Tidal periodically changes their API; the library tracks changes but lags. Pin a known-good version. Wrap calls with `try/except` at the provider boundary.
- Python compatibility: 3.9+.
- Sync (not async). All HTTP calls are blocking. The `TidalProvider` is sync; the proxy handles the async-to-sync hop the same way as the YT path (`loop.run_in_executor`).

**Authentication**

Two mutually exclusive flows in 0.8.x:

1. **OAuth device flow** -- `session.login_oauth()`. Returns a tuple `(LinkLogin, concurrent.futures.Future)`. The user opens `link.verification_uri_complete` in a browser, authorizes, and the future resolves. Persist token via `session.token_type`, `session.access_token`, `session.refresh_token`, `session.expiry_time`. Re-load via `session.load_oauth_session(token_type, access_token, refresh_token, expiry_time)`. Validate via `session.check_login()`. Auto-refreshes on subsequent uses while the refresh token is valid (months). When the refresh token expires, the daemon must warn-and-skip until `xmpctl auth tidal` is re-run.
2. **PKCE flow** -- `session.login_pkce()` and friends. Required for HI_RES_LOSSLESS streams (which return DASH manifests). NOT used in this iteration. See Cross-Cutting Concerns > Tidal HiRes Streaming Constraint.

`LinkLogin` shape (verify live):

- `verification_uri_complete: str` -- the `link.tidal.com/XXXXX` URL (no scheme). Prepend `https://` for user-friendliness.
- `expires_in: int` (seconds) -- device-code TTL, typically ~300s.
- `interval: int` (seconds) -- polling interval for the future internally.

After `future.result()` resolves successfully, the `Session` is logged in:

- `session.token_type: str` -- typically `"Bearer"`.
- `session.access_token: str` -- bearer token; ~600 chars.
- `session.refresh_token: str` -- long-lived refresh token; ~600 chars.
- `session.expiry_time: datetime.datetime` -- access-token expiry. Refresh token has its own (much longer) expiry not exposed here.

`session.config.quality = Quality.high_lossless` clamps the session to LOSSLESS. The actual stream-quality decision happens at `track.get_url(quality=...)` call time (Phase 10).

**Persisting tokens**

`tidalapi`'s built-in `session.save_session_to_file()` does NOT persist `expiry_time`. We roll our own JSON shape:

```json
{
  "token_type": "Bearer",
  "access_token": "...",
  "refresh_token": "...",
  "expiry_time": "2026-05-04T12:34:56",
  "is_pkce": false
}
```

Stored at `~/.config/xmpd/tidal_session.json`, mode `0600`. The `is_pkce` field is reserved for forward compatibility; Phase 9 always writes False.

**Quality enum** (`tidalapi.media.Quality` in 0.8.x)

| Enum member | Audio | Notes |
|---|---|---|
| `low_320k` | AAC 96 kbps | enum-name confusing; actually low quality |
| `high_320k` | AAC 320 kbps | "high" tier |
| `high_lossless` | FLAC 16-bit/44.1 kHz | LOSSLESS tier; effective ceiling for this iteration |
| `hi_res_lossless` | FLAC 24-bit MQA / DASH manifest | requires PKCE; deferred |

The exact enum names sometimes drift across tidalapi versions. The live-verify step above prints the actual enum members; if 0.8.11 has renamed them, update the imports.

**Key operations** (Phase 10 will use; Phase 9 only needs Session/login)

- `session.user` -- the logged-in user (`User` object). Has `.id`, `.playlists()` -> own playlists, `.favorites` -> `Favorites` object.
- `session.user.favorites.tracks()` -> list of `Track`s (favorites = "Liked Songs" equivalent).
- `session.user.favorites.playlists()` -> list of `Playlist`s the user has favorited (NOT created).
- `session.user.favorites.add_track(track_id: int)` / `.remove_track(track_id: int)` -- toggle a favorite. Note: integer track ID, NOT string.
- `session.playlist(playlist_id: str)` -> `Playlist`. `.tracks()` returns tracks.
- `session.track(track_id: str | int)` -> `Track`. `.get_url(quality=Quality.high_lossless)` returns a playable URL string for LOW/HIGH/LOSSLESS. For HI_RES_LOSSLESS, returns a DASH manifest XML (NOT directly consumable by MPD; this is why we clamp to LOSSLESS).
- `session.search(query, models=[tidalapi.media.Track])` -> dict-like with `"tracks"` key OR `SearchResult` object with `.tracks` attribute. The 0.8.x shape varies; defensive parsing required.
- `track.get_track_radio()` -> list of `Track`s (Tidal's "Track Radio" recommendation seed). No `limit` parameter; slice locally.
- `track.audio_quality` -> str ("LOW", "HIGH", "LOSSLESS", "HI_RES_LOSSLESS"). Used by Phase 10's quality-min(ceiling, available) logic.
- `track.album.image(640)` -> str URL of album art at 640x640. Use for `art_url` in `TrackMetadata`. Common sizes: 80, 160, 320, 640, 1280.

**Track ID type**

- `Track.id` is an `int` in 0.8.x.
- The xmpd `Track.track_id` is `str` (Provider Protocol contract, shared across YT and Tidal).
- Conversion at the boundary: `str(t.id)` when constructing a Track; `int(track_id)` when calling `favorites.add_track(...)` / `.remove_track(...)`.

**Streaming / HLS / DASH**

- LOW / HIGH / LOSSLESS via OAuth: `track.get_url(quality=...)` returns a single direct URL (FLAC for LOSSLESS). MPD can play these directly (HLS support is built in, FLAC is native).
- HI_RES_LOSSLESS via PKCE: returns a DASH-segmented MPEG manifest. MPD cannot consume DASH without an external muxer (e.g. ffmpeg piping into a stream). PKCE complications are deferred. Phase 10 clamps the effective ceiling to LOSSLESS regardless of `tidal.quality_ceiling` config.
- Stream URL TTL: unknown precisely; tidalapi documentation suggests minutes to ~1 hour. Default `tidal.stream_cache_hours: 1` (set in Phase 11's config). Refresh on 403/410 from the redirect target.

**Search**

- `session.search(query, models=[tidalapi.media.Track])` returns either a dict with `"tracks"`, `"albums"`, etc. keys OR a `SearchResult` namedtuple-like object (varies by 0.8.x patch version). Phase 10's parser handles both shapes:

  ```python
  raw_tracks = results.get("tracks", []) if isinstance(results, dict) else getattr(results, "tracks", [])
  ```

- Slice locally for `limit`; the lib does not always honor a `limit=` kwarg.

**Radio**

- `session.track(seed_id).get_track_radio()` returns up to ~50 tracks. No `limit` parameter; slice locally.

**Favorites**

- `session.user.favorites.tracks()` -- returns the user's favorited tracks (the "Liked Songs" equivalent). Synthesize as the `TD: Favorites` pseudo-playlist in `TidalProvider.list_playlists()` (Phase 10).
- `session.user.favorites.add_track(int_id)` / `remove_track(int_id)` -- toggle. Best-effort: if the API returns 4xx, log at debug and continue (the `get_like_state` cache will resync on next sync).

**Rate limits**

- tidalapi observes Tidal's per-IP rate limits implicitly; on 429 it raises `tidalapi.exceptions.TooManyRequests` with a `retry_after` attribute (seconds).
- For sync-loop safety: cache the favorites set during sync (`self._favorites_cache: set[str]`) so `get_like_state` does not issue one API call per track. Phase 10 implements this caching pattern.

**Exception hierarchy**

(Module path: `tidalapi.exceptions` in 0.8.x.)

- `tidalapi.exceptions.AuthenticationError` -- token issues; Phase 9/10 maps to `TidalAuthRequired`.
- `tidalapi.exceptions.ObjectNotFound` -- track/playlist 404; log at debug, skip.
- `tidalapi.exceptions.URLNotAvailable` -- track URL fetch failed (region-locked, etc.); log at debug, skip.
- `tidalapi.exceptions.StreamNotAvailable` -- similar to URLNotAvailable but for stream-level errors.
- `tidalapi.exceptions.TooManyRequests` -- 429; back off using `e.retry_after`.

Phase 9 only needs to catch `Exception` from `future.result()` and re-raise as `TidalAuthRequired`. Phase 10 deals with the per-method exceptions.

**Gotchas**

1. `Track.id` is `int`, not `str`. xmpd uses string IDs across providers; convert with `str(t.id)` outbound and `int(track_id)` when calling favorites methods.
2. `LinkLogin.verification_uri_complete` does NOT include a scheme. Prepend `https://` before printing or copying.
3. tidalapi's `save_session_to_file` does NOT persist `expiry_time`; we roll our own JSON.
4. The `Quality` enum member names sometimes change across versions. Live-verify and update the import if 0.8.11 differs.
5. `session.search()` return shape varies (dict vs. SearchResult). Use defensive parsing (`isinstance(results, dict)` + `getattr` fallback).
6. `track.get_track_radio()` does not honor a `limit=` parameter; slice locally.
7. The `Future` from `login_oauth()` does not accept a `timeout=` argument in 0.8.x; the device-code expiry is enforced inside the future (raises if it expires).
8. HI_RES_LOSSLESS via OAuth returns a DASH manifest, not a playable URL. Clamp to LOSSLESS for this iteration.
9. `tidalapi.Session()` at construction time reads no environment; quality must be set after construction via `session.config.quality = Quality.high_lossless`.
10. tidalapi auto-refreshes the access token on subsequent calls when the refresh token is valid. Phase 9 does NOT need explicit refresh logic; just persist whatever the latest `session.access_token` / `expiry_time` is at logout/exit.

**Working code example (Phase 9 OAuth flow)**

```python
from datetime import datetime
from pathlib import Path
import json
import os
import shutil
import subprocess

import tidalapi
from tidalapi.media import Quality


def run_oauth(session_path: Path) -> tidalapi.Session:
    session = tidalapi.Session()
    session.config.quality = Quality.high_lossless

    link, future = session.login_oauth()
    url = f"https://{link.verification_uri_complete}"
    print(f"Open: {url}")
    if shutil.which("wl-copy") and os.environ.get("WAYLAND_DISPLAY"):
        subprocess.run(["wl-copy"], input=url.encode(), check=False)
        print("(URL copied to clipboard.)")

    future.result()  # blocks until user authorizes

    session_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    payload = {
        "token_type": session.token_type,
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "expiry_time": session.expiry_time.isoformat(),
        "is_pkce": False,
    }
    tmp = session_path.with_suffix(session_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, session_path)

    return session


def load_oauth(session_path: Path) -> tidalapi.Session | None:
    if not session_path.is_file():
        return None
    data = json.loads(session_path.read_text())
    session = tidalapi.Session()
    session.config.quality = Quality.high_lossless
    session.load_oauth_session(
        data["token_type"], data["access_token"], data["refresh_token"], data["expiry_time"]
    )
    return session if session.check_login() else None
```

---

## Notes

- **HiRes ceiling clamping**: set `session.config.quality = Quality.high_lossless` in BOTH `run_oauth_flow` AND `load_session`. The actual stream-resolution clamping happens in Phase 10's `resolve_stream`, but setting it on the Session at construction is best practice (some tidalapi internals may use it for non-stream calls).

- **Token redaction**: when logging or printing session info during testing, NEVER log the full `access_token` / `refresh_token`. Use `[:20] + "..."` truncation. The pre-commit hook would catch verbatim tokens, but be careful in stdout/logs.

- **Clipboard helper graceful fallback**: the `_copy_to_clipboard` MUST never raise. Detection order: `$WAYLAND_DISPLAY` -> wl-copy, `$DISPLAY` -> xclip, neither -> just print. Even if both env vars and a tool exist, the tool may fail (clipboard locked, no daemon running); catch all subprocess errors and return False.

- **Phase 10 stub method order**: keep the order in `xmpd/providers/tidal.py` matching the order in `xmpd/providers/base.py`'s `Provider` Protocol declaration. This makes Phase 10's diff (filling in bodies) a side-by-side review.

- **`build_registry` lazy import**: importing `TidalProvider` at the top of `xmpd/providers/__init__.py` would force `tidalapi` to load on every `xmpd` import, including from environments where the user has not installed Tidal extras. Keep the import inside the `if "tidal" in enabled` branch so a yt-only install never imports tidalapi.

- **Rollback safety**: if Phase 9 lands but Phase 10 is delayed, the daemon still boots cleanly with `tidal.enabled: false` (the default). With `tidal.enabled: true`, build_registry constructs `TidalProvider`; any sync-engine call into a stub method will surface a clear `NotImplementedError("Phase 10")` in the logs and the per-provider failure-isolation in Phase 6's sync engine will skip Tidal for that cycle. This is acceptable.

- **Live verification cleanup**: after the live OAuth round-trip, the persisted `tidal_session.json` is left in place -- it is the user's actual session, used by subsequent phases. Do NOT delete it.

- **Sentinel-track avoidance**: Phase 9 makes no like/unlike calls. The HARD GUARDRAIL for Tidal favorites is structurally not at risk. Phase 10 owns the favorites round-trip and is where sentinel-track discipline begins.
