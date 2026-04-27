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
from datetime import datetime
from pathlib import Path

import tidalapi
from tidalapi.media import Quality

from xmpd.exceptions import TidalAuthRequired

logger = logging.getLogger(__name__)


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
       "URL copied to clipboard.", on failure print the install-helpers hint.
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

    fn_print("Open this URL in your browser to authorize xmpd:")
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


def load_session(session_path: Path) -> tidalapi.Session | None:
    """Load a persisted Tidal session from JSON.

    1. If ``session_path`` does not exist, return ``None``.
    2. Read and parse JSON. On ``json.JSONDecodeError`` or ``OSError``, log
       at WARNING and return ``None``.
    3. Construct ``tidalapi.Session()`` and set
       ``session.config.quality = Quality.high_lossless``.
    4. Call ``session.load_oauth_session(token_type, access_token,
       refresh_token, expiry_time)``. Note: tidalapi 0.8.x's
       ``load_oauth_session`` expects ``expiry_time`` as a
       ``datetime.datetime`` object; the stored ISO-8601 string is parsed
       back to ``datetime`` before the call.
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
        expiry_time_raw = data["expiry_time"]
        # tidalapi 0.8.x load_oauth_session expects datetime, not str
        expiry_time: datetime | None = (
            datetime.fromisoformat(expiry_time_raw) if isinstance(expiry_time_raw, str) else None
        )
        session.load_oauth_session(
            data["token_type"],
            data["access_token"],
            data["refresh_token"],
            expiry_time,
        )
    except (KeyError, Exception) as e:
        logger.warning("Failed to load Tidal session: %s", type(e).__name__)
        return None

    if not session.check_login():
        logger.info("Tidal session at %s exists but check_login() returned False", session_path)
        return None

    return session


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
