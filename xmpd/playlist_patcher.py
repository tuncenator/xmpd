"""Playlist patching for immediate like-indicator updates.

After a like-toggle, patches on-disk M3U/XSPF playlist files and the live MPD
queue to reflect the new like state without waiting for the next periodic sync.
"""

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Matches #EXTINF lines: group(1) = duration, group(2) = title text
_EXTINF_RE = re.compile(r"^(#EXTINF:-?\d+,)(.*)$")


def _build_indicator(tag: str) -> str:
    return f"[{tag}]"


def _add_indicator_to_title(title: str, indicator: str, alignment: str) -> str:
    """Append or prepend indicator to title, respecting alignment."""
    if alignment == "left":
        return f"{indicator} {title}"
    return f"{title} {indicator}"


def _remove_indicator_from_title(title: str, indicator: str) -> str:
    """Strip indicator from title regardless of position."""
    # Remove right-aligned: " [+1]" at end
    right = f" {indicator}"
    if title.endswith(right):
        return title[: -len(right)]
    # Remove left-aligned: "[+1] " at start
    left = f"{indicator} "
    if title.startswith(left):
        return title[len(left):]
    return title


def _patch_m3u_file(
    file_path: Path,
    proxy_url: str,
    liked: bool,
    indicator: str,
    alignment: str,
) -> None:
    """Patch a single M3U file in-place.

    Finds #EXTINF lines whose following URL line matches proxy_url and updates
    the title portion. Writes back only if a change was made.
    """
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        logger.warning("playlist_patcher: cannot read %s: %s", file_path, exc)
        return

    changed = False
    new_lines = list(lines)

    for i, line in enumerate(lines):
        # URL lines follow their EXTINF lines; check if this line is our URL
        stripped = line.rstrip("\r\n")
        if stripped != proxy_url:
            continue
        # Found the URL line; look at the preceding line for EXTINF
        if i == 0:
            continue
        prev = new_lines[i - 1]
        m = _EXTINF_RE.match(prev.rstrip("\r\n"))
        if not m:
            continue
        prefix, title = m.group(1), m.group(2)
        has_indicator = indicator in title

        if liked and not has_indicator:
            new_title = _add_indicator_to_title(title, indicator, alignment)
            new_lines[i - 1] = f"{prefix}{new_title}\n"
            changed = True
        elif not liked and has_indicator:
            new_title = _remove_indicator_from_title(title, indicator)
            new_lines[i - 1] = f"{prefix}{new_title}\n"
            changed = True

    if changed:
        try:
            file_path.write_text("".join(new_lines), encoding="utf-8")
            logger.debug("playlist_patcher: patched M3U %s (liked=%s)", file_path.name, liked)
        except OSError as exc:
            logger.warning("playlist_patcher: cannot write %s: %s", file_path, exc)


def _patch_xspf_file(
    file_path: Path,
    proxy_url: str,
    liked: bool,
    indicator: str,
) -> None:
    """Patch a single XSPF file in-place using regex on the raw text.

    Finds <track> blocks containing the proxy URL in <location> and updates
    the <title> element. Writes back only if a change was made.
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("playlist_patcher: cannot read %s: %s", file_path, exc)
        return

    if proxy_url not in content:
        return

    # Split into <track>...</track> blocks; process each independently
    track_re = re.compile(r"(<track>.*?</track>)", re.DOTALL)
    title_re = re.compile(r"(<title>)(.*?)(</title>)")

    changed = False

    def patch_track_block(m: re.Match) -> str:
        nonlocal changed
        block = m.group(1)
        if proxy_url not in block:
            return block

        def patch_title(tm: re.Match) -> str:
            nonlocal changed
            open_tag, title_text, close_tag = tm.group(1), tm.group(2), tm.group(3)
            has_indicator = indicator in title_text

            if liked and not has_indicator:
                new_text = f"{title_text} {indicator}"
                changed = True
                return f"{open_tag}{new_text}{close_tag}"
            elif not liked and has_indicator:
                new_text = _remove_indicator_from_title(title_text, indicator)
                changed = True
                return f"{open_tag}{new_text}{close_tag}"
            return tm.group(0)

        return title_re.sub(patch_title, block)

    new_content = track_re.sub(patch_track_block, content)

    if changed:
        try:
            file_path.write_text(new_content, encoding="utf-8")
            logger.debug("playlist_patcher: patched XSPF %s (liked=%s)", file_path.name, liked)
        except OSError as exc:
            logger.warning("playlist_patcher: cannot write %s: %s", file_path, exc)


def patch_playlist_files(
    proxy_url_pattern: str,
    liked: bool,
    playlist_dir: Path,
    xspf_dir: Path | None,
    like_indicator_config: dict[str, Any],
    favorites_playlist_names: set[str],
) -> None:
    """Scan playlist directories and update the like indicator for a track.

    Args:
        proxy_url_pattern: Proxy URL for the track (exact match).
        liked: True if track was just liked, False if just unliked.
        playlist_dir: M3U playlist directory path.
        xspf_dir: XSPF playlist directory path, or None to skip XSPF.
        like_indicator_config: Dict with 'enabled', 'tag', 'alignment' keys.
        favorites_playlist_names: Base names (without extension) of favorites
            playlists that should be skipped.
    """
    if not like_indicator_config.get("enabled", False):
        return

    tag = like_indicator_config.get("tag", "+1")
    alignment = like_indicator_config.get("alignment", "right")
    indicator = _build_indicator(tag)

    # Patch M3U files
    if playlist_dir.is_dir():
        for m3u_file in playlist_dir.glob("*.m3u"):
            stem = m3u_file.stem
            if stem in favorites_playlist_names:
                logger.debug("playlist_patcher: skipping favorites M3U %s", m3u_file.name)
                continue
            _patch_m3u_file(m3u_file, proxy_url_pattern, liked, indicator, alignment)
    else:
        if playlist_dir.exists():
            logger.warning(
                "playlist_patcher: M3U playlist_dir is not a directory: %s", playlist_dir
            )

    # Patch XSPF files
    if xspf_dir is not None and xspf_dir.is_dir():
        for xspf_file in xspf_dir.glob("*.xspf"):
            stem = xspf_file.stem
            if stem in favorites_playlist_names:
                logger.debug("playlist_patcher: skipping favorites XSPF %s", xspf_file.name)
                continue
            _patch_xspf_file(xspf_file, proxy_url_pattern, liked, indicator)


def patch_mpd_queue(
    mpd_client_raw: Any,
    proxy_url_pattern: str,
    base_title: str,
    liked: bool,
    like_indicator_config: dict[str, Any],
) -> None:
    """Update title tags on all queue entries matching the proxy URL.

    Clears and re-sets the Title tag so ncmpcpp reflects the like state
    immediately without disrupting playback.

    Args:
        mpd_client_raw: Raw MPDClientBase instance (daemon's _client).
        proxy_url_pattern: Proxy URL to match against queue entries.
        base_title: Track title without any indicator (used as the new title
            when unliking, or as base when adding indicator).
        liked: True if track was just liked, False if just unliked.
        like_indicator_config: Dict with 'enabled', 'tag', 'alignment' keys.
    """
    if not like_indicator_config.get("enabled", False):
        return

    tag = like_indicator_config.get("tag", "+1")
    alignment = like_indicator_config.get("alignment", "right")
    indicator = _build_indicator(tag)

    if liked:
        new_title = _add_indicator_to_title(base_title, indicator, alignment)
    else:
        new_title = base_title

    try:
        queue = mpd_client_raw.playlistinfo()
    except Exception as exc:
        logger.warning("playlist_patcher: playlistinfo failed: %s", exc)
        return

    updated = 0
    for entry in queue:
        if entry.get("file") != proxy_url_pattern:
            continue
        song_id = entry.get("id")
        if not song_id:
            continue
        try:
            mpd_client_raw.cleartagid(song_id, "Title")
            mpd_client_raw.addtagid(song_id, "Title", new_title)
            updated += 1
        except Exception as exc:
            logger.warning(
                "playlist_patcher: failed to update queue entry %s: %s", song_id, exc
            )

    if updated:
        logger.debug(
            "playlist_patcher: updated %d queue entry(s) for %s (liked=%s)",
            updated, proxy_url_pattern, liked,
        )
