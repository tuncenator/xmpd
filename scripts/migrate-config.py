#!/usr/bin/env python3
"""Migrate ~/.config/xmpd/config.yaml from the legacy single-provider shape
to the multi-source (yt: / tidal:) shape introduced in xmpd 1.5.

Idempotent: safe to run repeatedly. Preserves user comments and key ordering
via ruamel.yaml round-trip mode.

This script is invoked by install.sh during the migration step. It can also
be run directly:
    python3 scripts/migrate-config.py [--dry-run] [--check]
"""
from __future__ import annotations

import argparse
import os
import sys
from io import StringIO

try:
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap
except ImportError:
    print(
        "error: ruamel.yaml is required. Install it with: uv pip install -e '.[dev]'",
        file=sys.stderr,
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def is_already_migrated(data: CommentedMap) -> bool:
    """Return True if the document already has the multi-source shape."""
    return (
        "auto_auth" not in data
        and "yt" in data
        and "tidal" in data
        and isinstance(data.get("playlist_prefix"), dict)
        and "yt" in data.get("playlist_prefix", {})
    )


def needs_migration(data: CommentedMap) -> tuple[bool, list[str]]:
    """Return (needs_migration, list_of_pending_transforms)."""
    pending: list[str] = []
    if "auto_auth" in data and "yt" not in data:
        pending.append("nest_auto_auth_under_yt")
    if "tidal" not in data:
        pending.append("add_tidal_block")
    pp = data.get("playlist_prefix")
    if pp is not None and not isinstance(pp, dict):
        pending.append("convert_playlist_prefix_to_dict")
    elif isinstance(pp, dict) and "tidal" not in pp:
        pending.append("add_tidal_to_playlist_prefix")
    elif pp is None:
        pending.append("add_playlist_prefix")
    return (bool(pending), pending)


# ---------------------------------------------------------------------------
# Transformation helpers
# ---------------------------------------------------------------------------


def _transform_nest_auto_auth(data: CommentedMap) -> None:
    """Move top-level auto_auth: into a new yt: block."""
    auto_auth_idx = list(data.keys()).index("auto_auth")
    auto_auth_value = data.pop("auto_auth")

    yt_block = CommentedMap()
    yt_block["enabled"] = True
    yt_block["stream_cache_hours"] = data.get("stream_cache_hours", 5)
    yt_block["auto_auth"] = auto_auth_value

    data.insert(auto_auth_idx, "yt", yt_block)


def _transform_add_tidal(data: CommentedMap) -> None:
    """Insert a tidal: block after the yt: block."""
    tidal_block = CommentedMap()
    tidal_block["enabled"] = False
    tidal_block["stream_cache_hours"] = 1
    tidal_block["quality_ceiling"] = "HI_RES_LOSSLESS"
    tidal_block["sync_favorited_playlists"] = True

    # Find insertion point: after yt: if present, else at position 0.
    keys = list(data.keys())
    if "yt" in keys:
        insert_idx = keys.index("yt") + 1
    else:
        insert_idx = 0

    data.insert(insert_idx, "tidal", tidal_block)
    data.yaml_set_comment_before_after_key(
        "tidal",
        before=(
            "\nTidal source (added by xmpd multi-source migration).\n"
            "Set enabled: true after running 'xmpctl auth tidal'."
        ),
    )


def _transform_playlist_prefix(data: CommentedMap) -> None:
    """Convert scalar playlist_prefix to a per-provider dict, or add missing keys."""
    pp = data.get("playlist_prefix")

    if pp is None:
        new_pp = CommentedMap()
        new_pp["yt"] = "YT: "
        new_pp["tidal"] = "TD: "
        # Insert after tidal: if present, else at end.
        keys = list(data.keys())
        if "tidal" in keys:
            insert_idx = keys.index("tidal") + 1
        else:
            insert_idx = len(keys)
        data.insert(insert_idx, "playlist_prefix", new_pp)
    elif not isinstance(pp, dict):
        # Scalar -> dict: preserve user's value as yt prefix.
        yt_prefix = str(pp)
        keys = list(data.keys())
        pp_idx = keys.index("playlist_prefix")
        del data["playlist_prefix"]
        new_pp = CommentedMap()
        new_pp["yt"] = yt_prefix
        new_pp["tidal"] = "TD: "
        data.insert(pp_idx, "playlist_prefix", new_pp)
        # Clear any block comments that ruamel.yaml may have carried from the
        # deleted scalar onto the new mapping key -- they would render inline
        # between the key and its nested content, causing invalid indentation.
        if hasattr(data, "ca") and "playlist_prefix" in data.ca.items:
            data.ca.items["playlist_prefix"] = [None, None, None, None]
    else:
        # Already a dict; ensure tidal key exists.
        if "tidal" not in pp:
            pp["tidal"] = "TD: "


# ---------------------------------------------------------------------------
# Core migration
# ---------------------------------------------------------------------------


def _make_yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    return yaml


def migrate(path: str) -> bool:
    """Apply all needed transformations to the config at path.

    Returns True if changes were written, False if already migrated.
    Raises SystemExit(2) on error.
    """
    yaml = _make_yaml()
    try:
        with open(path) as f:
            data = yaml.load(f)
    except FileNotFoundError:
        print(f"error: config file not found at {path}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"error: failed to parse YAML: {exc}", file=sys.stderr)
        sys.exit(2)

    if data is None:
        data = CommentedMap()

    needed, pending = needs_migration(data)
    if not needed:
        print("Config already in multi-source shape; no changes.")
        return False

    # Apply transformations in order.
    if "nest_auto_auth_under_yt" in pending:
        _transform_nest_auto_auth(data)
    if "add_tidal_block" in pending:
        _transform_add_tidal(data)
    if "convert_playlist_prefix_to_dict" in pending:
        _transform_playlist_prefix(data)
    elif "add_tidal_to_playlist_prefix" in pending:
        _transform_playlist_prefix(data)
    elif "add_playlist_prefix" in pending:
        _transform_playlist_prefix(data)

    # Atomic write.
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            yaml.dump(data, f)
        os.replace(tmp_path, path)
    except Exception as exc:
        print(f"error: failed to write config: {exc}", file=sys.stderr)
        # Clean up tmp if write failed.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        sys.exit(2)

    return True


def migrate_dry_run(path: str) -> None:
    """Print the migrated YAML to stdout without writing."""
    yaml = _make_yaml()
    try:
        with open(path) as f:
            data = yaml.load(f)
    except FileNotFoundError:
        print(f"error: config file not found at {path}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"error: failed to parse YAML: {exc}", file=sys.stderr)
        sys.exit(2)

    if data is None:
        data = CommentedMap()

    needed, _ = needs_migration(data)
    if not needed:
        print("Config already in multi-source shape; no changes.")
        return

    if "nest_auto_auth_under_yt" in needs_migration(data)[1]:
        _transform_nest_auto_auth(data)
    _, pending2 = needs_migration(data)
    if "add_tidal_block" in pending2:
        _transform_add_tidal(data)
    _, pending3 = needs_migration(data)
    if "convert_playlist_prefix_to_dict" in pending3:
        _transform_playlist_prefix(data)
    elif "add_tidal_to_playlist_prefix" in pending3:
        _transform_playlist_prefix(data)
    elif "add_playlist_prefix" in pending3:
        _transform_playlist_prefix(data)

    buf = StringIO()
    yaml.dump(data, buf)
    print(buf.getvalue(), end="")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate xmpd config.yaml from legacy single-provider shape to multi-source.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes:\n"
            "  0  success or already migrated\n"
            "  1  --check and migration is needed\n"
            "  2  error (file not found, parse error, write error)"
        ),
    )
    parser.add_argument(
        "--config",
        default=os.path.expanduser("~/.config/xmpd/config.yaml"),
        metavar="PATH",
        help="Path to config.yaml (default: ~/.config/xmpd/config.yaml).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the migrated YAML to stdout; do not write.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 0 if already migrated, 1 if migration is needed, 2 on error. No writes.",
    )
    args = parser.parse_args()

    if args.check:
        yaml = _make_yaml()
        try:
            with open(args.config) as f:
                data = yaml.load(f)
        except FileNotFoundError:
            print(f"error: config file not found at {args.config}", file=sys.stderr)
            sys.exit(2)
        except Exception as exc:
            print(f"error: failed to parse YAML: {exc}", file=sys.stderr)
            sys.exit(2)

        if data is None:
            data = CommentedMap()

        needed, pending = needs_migration(data)
        if needed:
            print(f"migration needed: {', '.join(pending)}")
            sys.exit(1)
        else:
            print("already migrated")
            sys.exit(0)

    if args.dry_run:
        migrate_dry_run(args.config)
        return

    migrate(args.config)


if __name__ == "__main__":
    main()
