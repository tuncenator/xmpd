# Phase 01: Provider abstraction foundation

**Feature**: tidal-init
**Estimated Context Budget**: ~25k tokens

**Difficulty**: easy

**Execution Mode**: sequential
**Batch**: 1

---

## Objective

Bootstrap the provider abstraction packages that every subsequent phase depends on. Create `xmpd/providers/` with `base.py` (frozen dataclasses + `Provider` runtime-checkable Protocol) and `__init__.py` (registry skeleton). Create the empty `xmpd/auth/` package marker. Verify the existing `logging.getLogger(__name__)` infrastructure survived the `ytmpd` -> `xmpd` rename intact (no rebuild -- just confirmation).

This phase is the foundation. Every other phase imports from `xmpd/providers/base.py` or `xmpd/auth/`. Get the dataclass shapes and Protocol signature exactly right; downstream phases will match them verbatim.

---

## Deliverables

1. **NEW** `xmpd/providers/__init__.py` -- registry skeleton with two functions:
   - `get_enabled_provider_names(config: dict) -> list[str]`
   - `build_registry(config: dict) -> dict[str, Provider]` (returns `{}` in Phase 1, with TODO comments for Phase 2 and Phase 9 to fill in branches)
2. **NEW** `xmpd/providers/base.py` -- module containing:
   - `TrackMetadata` (frozen dataclass)
   - `Track` (frozen dataclass)
   - `Playlist` (frozen dataclass)
   - `Provider` (`@runtime_checkable Protocol` with 14 method signatures, all bodies `...`)
3. **NEW** `xmpd/auth/__init__.py` -- single-line docstring `"""Per-provider auth helpers."""` (package marker only).
4. **NEW** `tests/test_providers_base.py` -- 4 tests (see Testing Requirements).
5. **NEW** `tests/test_providers_registry.py` -- 4 tests (see Testing Requirements).
6. **VERIFY** logging infrastructure intact: run `grep -rn "getLogger" xmpd/` and confirm every match is either `getLogger(__name__)` (per module) or `getLogger()` (root logger in `__main__.py:33` -- this is intentional, NOT a deviation). Document the audit result in the phase summary's "Notes" section.

---

## Detailed Requirements

### 1. `xmpd/providers/base.py` -- shared dataclasses + Protocol

Create `xmpd/providers/base.py` with the EXACT module contents below. The dataclasses come verbatim from PROJECT_PLAN.md "Data Models" -> "New shared dataclasses (Phase 1)". The Protocol signature must list all 14 methods downstream phases will implement.

```python
"""Shared types for the provider abstraction.

Every provider (YT Music, Tidal, ...) implements the `Provider` Protocol
defined in this module. The Protocol is `runtime_checkable` so a provider
instance can be validated with `isinstance(obj, Provider)` -- this is used by
`xmpd/providers/__init__.py::build_registry` once the concrete provider
classes land in Phase 2 (yt) and Phase 9 (tidal).

The dataclasses (`TrackMetadata`, `Track`, `Playlist`) are the cross-provider
exchange shape; concrete providers must convert their library-native objects
into these before returning them. They are frozen to keep them hashable and
to make accidental mutation a TypeError at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class TrackMetadata:
    """Provider-agnostic track metadata. All fields except `title` are nullable."""

    title: str
    artist: str | None
    album: str | None
    duration_seconds: int | None
    art_url: str | None


@dataclass(frozen=True)
class Track:
    """A track from any provider, identified by compound (provider, track_id) key."""

    provider: str  # canonical name: "yt" | "tidal"
    track_id: str
    metadata: TrackMetadata
    liked: bool | None = None
    liked_signature: str | None = None  # reserved for future cross-provider sync


@dataclass(frozen=True)
class Playlist:
    """A playlist from any provider."""

    provider: str
    playlist_id: str
    name: str
    track_count: int
    is_owned: bool
    is_favorites: bool


@runtime_checkable
class Provider(Protocol):
    """Protocol every concrete provider class must satisfy.

    Method bodies are `...` per Python Protocol convention. Concrete classes
    in `xmpd/providers/ytmusic.py` (Phase 3) and `xmpd/providers/tidal.py`
    (Phase 10) implement the bodies. Provider canonical names (`yt`, `tidal`)
    are exposed via the module-level `name` attribute, not a method.
    """

    name: str  # canonical short name, e.g. "yt" or "tidal"

    def is_enabled(self) -> bool: ...

    def is_authenticated(self) -> tuple[bool, str]:
        """Return (ok, error_msg). error_msg is empty when ok is True."""
        ...

    def list_playlists(self) -> list[Playlist]: ...

    def get_playlist_tracks(self, playlist_id: str) -> list[Track]: ...

    def get_favorites(self) -> list[Track]: ...

    def resolve_stream(self, track_id: str) -> str | None:
        """Return a fresh direct stream URL for `track_id`, or None on failure."""
        ...

    def get_track_metadata(self, track_id: str) -> TrackMetadata | None: ...

    def search(self, query: str, limit: int = 25) -> list[Track]: ...

    def get_radio(self, track_id: str, limit: int = 25) -> list[Track]: ...

    def like(self, track_id: str) -> bool: ...

    def dislike(self, track_id: str) -> bool: ...

    def unlike(self, track_id: str) -> bool: ...

    def get_like_state(self, track_id: str) -> str:
        """Return one of 'LIKED', 'DISLIKED', 'NEUTRAL'."""
        ...

    def report_play(self, track_id: str, duration_seconds: int) -> bool: ...
```

Key constraints:

- All dataclasses use `@dataclass(frozen=True)`.
- `from __future__ import annotations` at top so type hints stay strings (lets us use `Track | None` style anywhere without import-order pain).
- Type hints use Python 3.11 idioms: `str | None`, `list[Track]`, `tuple[bool, str]`. NO `Optional[X]`. NO `List[X]`. NO `Tuple[...]`.
- The Protocol method signatures match exactly what subsequent phases will implement. Don't change argument names or default values without updating the brief.
- `name` is declared as a class-level `str` attribute, NOT a method. Concrete providers set it as a class variable (Phase 2: `class YTMusicProvider: name = "yt"`).

### 2. `xmpd/providers/__init__.py` -- registry skeleton

Create `xmpd/providers/__init__.py` with the exact contents below.

```python
"""Provider registry.

Phase 1 ships only the skeleton functions. The concrete provider classes
(`YTMusicProvider`, `TidalProvider`) do not exist yet, so `build_registry`
returns an empty dict. Subsequent phases fill in the branches:

- Phase 2 wires the YT branch (imports `YTMusicProvider` from
  `xmpd/providers/ytmusic.py` and constructs it from `config["yt"]`).
- Phase 9 wires the Tidal branch (imports `TidalProvider` from
  `xmpd/providers/tidal.py` and constructs it from `config["tidal"]`).
"""

from __future__ import annotations

import logging

from xmpd.providers.base import Playlist, Provider, Track, TrackMetadata

logger = logging.getLogger(__name__)

# Public re-exports so callers can `from xmpd.providers import Provider, Track, ...`
__all__ = [
    "Playlist",
    "Provider",
    "Track",
    "TrackMetadata",
    "build_registry",
    "get_enabled_provider_names",
]


def get_enabled_provider_names(config: dict) -> list[str]:
    """Return canonical names of enabled providers, sorted alphabetically.

    Reads `config[name]["enabled"]` for each known provider name. Missing
    sections or missing `enabled` keys are treated as disabled.

    Examples:
        >>> get_enabled_provider_names({"yt": {"enabled": True}})
        ['yt']
        >>> get_enabled_provider_names({"yt": {"enabled": True}, "tidal": {"enabled": True}})
        ['tidal', 'yt']
        >>> get_enabled_provider_names({})
        []
    """
    known = ("yt", "tidal")
    enabled = [n for n in known if bool(config.get(n, {}).get("enabled", False))]
    return sorted(enabled)


def build_registry(config: dict) -> dict[str, Provider]:
    """Build the provider registry from config.

    Returns an empty dict in Phase 1 -- the concrete provider classes do not
    exist yet. The conductor's batching plan fills this in later:

      TODO(Phase 2): if "yt" in get_enabled_provider_names(config):
          from xmpd.providers.ytmusic import YTMusicProvider
          registry["yt"] = YTMusicProvider(config["yt"])
      TODO(Phase 9): if "tidal" in get_enabled_provider_names(config):
          from xmpd.providers.tidal import TidalProvider
          registry["tidal"] = TidalProvider(config["tidal"])

    Phase 8 wires this into `XMPDaemon.__init__` and adds the warn-and-continue
    behavior on per-provider auth failure.
    """
    registry: dict[str, Provider] = {}
    enabled = get_enabled_provider_names(config)
    if enabled:
        logger.debug(
            "build_registry: %d enabled provider(s) configured (%s) -- "
            "Phase 1 returns empty; concrete classes ship in Phase 2/9",
            len(enabled),
            ", ".join(enabled),
        )
    return registry
```

Key constraints:

- The `__all__` list is mandatory so `from xmpd.providers import Provider, Track, ...` works without surprise.
- Use `logger = logging.getLogger(__name__)` per the existing pattern.
- The TODO comments in `build_registry` MUST reference Phase 2 and Phase 9 by number so the next planners can grep for them.
- Do NOT import `YTMusicProvider` or `TidalProvider` here -- those modules don't exist yet, and importing them would crash on `python -c "import xmpd.providers"`.

### 3. `xmpd/auth/__init__.py` -- empty package marker

Create `xmpd/auth/__init__.py` containing exactly:

```python
"""Per-provider auth helpers."""
```

Nothing else. Phase 2 will move `xmpd/cookie_extract.py` to `xmpd/auth/ytmusic_cookie.py`. Phase 9 will create `xmpd/auth/tidal_oauth.py`. Phase 1 only places the package marker so the directory is importable.

### 4. `tests/test_providers_base.py` -- dataclass and Protocol tests

Create `tests/test_providers_base.py` with these 4 tests. Use plain functions, no test classes (matches existing convention in `tests/`). Each test must be self-contained.

```python
"""Tests for xmpd/providers/base.py: dataclasses and runtime-checkable Protocol."""

from __future__ import annotations

from xmpd.providers.base import Playlist, Provider, Track, TrackMetadata


def test_track_metadata_construction() -> None:
    """TrackMetadata holds title + nullable artist/album/duration/art."""
    md = TrackMetadata(
        title="Song",
        artist="Artist",
        album="Album",
        duration_seconds=200,
        art_url="https://example.com/art.jpg",
    )
    assert md.title == "Song"
    assert md.artist == "Artist"
    assert md.album == "Album"
    assert md.duration_seconds == 200
    assert md.art_url == "https://example.com/art.jpg"

    # All except title are nullable.
    md2 = TrackMetadata(title="Bare", artist=None, album=None, duration_seconds=None, art_url=None)
    assert md2.title == "Bare"
    assert md2.artist is None


def test_track_construction_with_provider() -> None:
    """Track carries (provider, track_id, metadata) + optional liked state."""
    md = TrackMetadata(
        title="Song", artist="Artist", album=None, duration_seconds=180, art_url=None
    )
    t = Track(provider="yt", track_id="abc12345_-9", metadata=md, liked=True)
    assert t.provider == "yt"
    assert t.track_id == "abc12345_-9"
    assert t.metadata.title == "Song"
    assert t.liked is True
    # liked_signature defaults to None (reserved for future cross-provider sync).
    assert t.liked_signature is None


def test_playlist_construction() -> None:
    """Playlist carries (provider, playlist_id, name) + flags."""
    p = Playlist(
        provider="tidal",
        playlist_id="123abc",
        name="Favorites",
        track_count=42,
        is_owned=True,
        is_favorites=True,
    )
    assert p.provider == "tidal"
    assert p.playlist_id == "123abc"
    assert p.name == "Favorites"
    assert p.track_count == 42
    assert p.is_owned is True
    assert p.is_favorites is True


def test_stub_satisfies_provider_protocol() -> None:
    """A class implementing all 14 Protocol methods passes isinstance(stub, Provider)."""

    class _StubProvider:
        name = "stub"

        def is_enabled(self) -> bool:
            return True

        def is_authenticated(self) -> tuple[bool, str]:
            return (True, "")

        def list_playlists(self) -> list[Playlist]:
            return []

        def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
            return []

        def get_favorites(self) -> list[Track]:
            return []

        def resolve_stream(self, track_id: str) -> str | None:
            return None

        def get_track_metadata(self, track_id: str) -> TrackMetadata | None:
            return None

        def search(self, query: str, limit: int = 25) -> list[Track]:
            return []

        def get_radio(self, track_id: str, limit: int = 25) -> list[Track]:
            return []

        def like(self, track_id: str) -> bool:
            return True

        def dislike(self, track_id: str) -> bool:
            return True

        def unlike(self, track_id: str) -> bool:
            return True

        def get_like_state(self, track_id: str) -> str:
            return "NEUTRAL"

        def report_play(self, track_id: str, duration_seconds: int) -> bool:
            return True

    stub = _StubProvider()
    assert isinstance(stub, Provider)
```

Key constraints:

- `test_stub_satisfies_provider_protocol` MUST exercise all 14 Protocol methods -- if you forget one, `runtime_checkable`'s isinstance would still pass (Protocol checks attribute *existence*, not signature shape), but downstream phases need the full template to copy from. The stub doubles as a reference implementation for Phases 3/10.
- Use `from xmpd.providers.base import ...` (NOT `from xmpd.providers import ...`) to make these tests independent of the registry module.
- Test names follow existing tests/ convention (`test_*` snake_case).

### 5. `tests/test_providers_registry.py` -- registry skeleton tests

Create `tests/test_providers_registry.py` with these 4 tests:

```python
"""Tests for xmpd/providers/__init__.py: registry skeleton."""

from __future__ import annotations

from xmpd.providers import build_registry, get_enabled_provider_names


def test_get_enabled_provider_names_empty() -> None:
    """No providers enabled -> empty list."""
    config = {"yt": {"enabled": False}, "tidal": {"enabled": False}}
    assert get_enabled_provider_names(config) == []
    # Also handles entirely missing sections.
    assert get_enabled_provider_names({}) == []


def test_get_enabled_provider_names_yt_only() -> None:
    """yt enabled, tidal disabled -> ['yt']."""
    config = {"yt": {"enabled": True}, "tidal": {"enabled": False}}
    assert get_enabled_provider_names(config) == ["yt"]
    # Missing tidal section behaves identically.
    assert get_enabled_provider_names({"yt": {"enabled": True}}) == ["yt"]


def test_get_enabled_provider_names_both() -> None:
    """Both enabled -> sorted(['tidal', 'yt'])."""
    config = {"yt": {"enabled": True}, "tidal": {"enabled": True}}
    assert get_enabled_provider_names(config) == ["tidal", "yt"]


def test_build_registry_phase1_returns_empty() -> None:
    """Phase 1: build_registry always returns {}; concrete classes ship in Phase 2/9."""
    config = {"yt": {"enabled": True}, "tidal": {"enabled": True}}
    registry = build_registry(config)
    assert registry == {}
    # And with neither enabled.
    assert build_registry({}) == {}
```

### 6. Logging infrastructure verification

Run, exactly:

```bash
grep -rn "getLogger" /home/tunc/Sync/Programs/xmpd/xmpd/
```

Expected (matches Phase 0 audit -- 13 hits):

- `xmpd/__main__.py:33: root_logger = logging.getLogger()` -- root logger config call, NOT a deviation
- `xmpd/__main__.py:51: logger = logging.getLogger(__name__)` -- correct
- 11 other modules: `logger = logging.getLogger(__name__)` -- all correct

If any line shows a hardcoded name like `getLogger("ytmpd")` or `getLogger("xmpd.something")`, fix it to `getLogger(__name__)` and document the fix in the phase summary's "Discoveries" section. The Phase 0 snapshot says everything is clean, so the expected branch is "no fixes needed".

Confirm `xmpd/daemon.py`'s log-handler config block is intact: open it, locate the `setup_logging` (or equivalent) function that wires `log_file` from config into a `logging.FileHandler` for `~/.config/xmpd/xmpd.log`. Confirm it still references `xmpd.log` (not `ytmpd.log`). Don't modify -- just confirm.

Document the audit result in the phase summary's "Notes" section as either:

> Logging infrastructure clean: all 13 grep hits are `getLogger(__name__)` or the root-logger call at `xmpd/__main__.py:33`. Daemon log handler in `xmpd/daemon.py` references `xmpd.log` correctly.

or, if anything was off:

> Logging infrastructure deviation: `<file>:<line>` had `<bad>`. Fixed to `getLogger(__name__)`.

---

## Step-by-Step Implementation Order

1. `mkdir xmpd/providers/ xmpd/auth/`.
2. Write `xmpd/providers/base.py` (dataclasses + Protocol).
3. Write `xmpd/providers/__init__.py` (registry skeleton).
4. Write `xmpd/auth/__init__.py` (one-line docstring).
5. Write `tests/test_providers_base.py`.
6. Write `tests/test_providers_registry.py`.
7. Run `pytest -q tests/test_providers_base.py tests/test_providers_registry.py` -- expect 8 pass.
8. Run `pytest -q` -- expect existing tests still green plus the 8 new tests.
9. Run `python -c "from xmpd.providers.base import Track, Playlist, TrackMetadata, Provider"` -- expect no output, exit 0.
10. Run `python -c "from xmpd.providers import get_enabled_provider_names, build_registry; assert get_enabled_provider_names({'yt': {'enabled': True}}) == ['yt']; assert build_registry({'yt': {'enabled': True}}) == {}"` -- expect no output, exit 0.
11. Run `python -c "from xmpd.auth import *"` -- expect no output, exit 0 (confirms package is importable).
12. Run logging audit grep; document result in summary.
13. Run `mypy xmpd/providers/` -- expect clean.
14. Run `ruff check xmpd/providers/ tests/test_providers_base.py tests/test_providers_registry.py` -- expect clean.

---

## Dependencies

**Requires**: None. Phase 1 is the foundation.

**Enables**:

- Phase 2 (YT module relocation + YTMusicProvider scaffold) -- imports `Provider` Protocol and shared dataclasses from `xmpd/providers/base.py`; fills the yt branch of `build_registry`.
- Phase 5 (Track store schema migration) -- needs the `(provider, track_id)` shape implied by `Track`.
- Phase 7 (History reporter + rating) -- imports `Provider` Protocol for type hints in registry-aware dispatch.
- Phase 9 (Tidal foundation) -- imports `Provider` Protocol and shared dataclasses; fills the tidal branch of `build_registry`.
- All other phases transitively.

---

## Completion Criteria

- [ ] `xmpd/providers/__init__.py`, `xmpd/providers/base.py`, `xmpd/auth/__init__.py` exist.
- [ ] `tests/test_providers_base.py` and `tests/test_providers_registry.py` exist.
- [ ] `pytest -q` exits 0; the existing test count grows by exactly 8 (4 in each new test file).
- [ ] `python -c "from xmpd.providers.base import Track, Playlist, TrackMetadata, Provider"` exits 0 with no output.
- [ ] `python -c "from xmpd.providers import get_enabled_provider_names, build_registry; assert get_enabled_provider_names({'yt': {'enabled': True}}) == ['yt']; assert build_registry({'yt': {'enabled': True}}) == {}"` exits 0 with no output.
- [ ] `python -c "import xmpd.auth"` exits 0 with no output.
- [ ] Logging audit (`grep -rn "getLogger" xmpd/`) result documented in phase summary; any deviation from the expected pattern is fixed and noted.
- [ ] `mypy xmpd/providers/` exits 0 with no errors.
- [ ] `ruff check xmpd/providers/ tests/test_providers_base.py tests/test_providers_registry.py` exits 0.
- [ ] Phase summary written to `docs/agent/tidal-init/summaries/PHASE_01_SUMMARY.md` per `PHASE_SUMMARY_TEMPLATE.md`, including the logging-audit result and any deviations.

---

## Testing Requirements

Exact test commands the coder MUST run before marking the phase complete:

```bash
cd /home/tunc/Sync/Programs/xmpd
source .venv/bin/activate  # or `uv venv && source .venv/bin/activate && uv pip install -e '.[dev]'` if missing

# Run only the new tests (fast feedback loop):
pytest -q tests/test_providers_base.py tests/test_providers_registry.py

# Then run the full suite to confirm nothing else broke:
pytest -q

# Type-check the new code:
mypy xmpd/providers/

# Lint the new code:
ruff check xmpd/providers/ tests/test_providers_base.py tests/test_providers_registry.py

# Smoke-import:
python -c "from xmpd.providers.base import Track, Playlist, TrackMetadata, Provider"
python -c "from xmpd.providers import get_enabled_provider_names, build_registry; assert get_enabled_provider_names({'yt': {'enabled': True}}) == ['yt']; assert build_registry({'yt': {'enabled': True}}) == {}"
python -c "import xmpd.auth"

# Logging audit:
grep -rn "getLogger" xmpd/
```

Expected results:

- `pytest -q tests/test_providers_*` -- 8 passed.
- `pytest -q` -- previous test count + 8, no failures.
- `mypy xmpd/providers/` -- "Success: no issues found".
- `ruff check ...` -- "All checks passed!".
- All `python -c ...` calls exit 0 with no output.
- `grep -rn "getLogger" xmpd/` -- 13 hits, all `getLogger(__name__)` or the single root-logger call at `xmpd/__main__.py:33`.

Edge cases the tests already cover:

- `get_enabled_provider_names` with empty dict (returns `[]`).
- `get_enabled_provider_names` with missing provider sections (treated as disabled).
- `get_enabled_provider_names` with both enabled (returns sorted list).
- `build_registry` with both enabled returns `{}` in Phase 1 (NOT a partial population).
- `Track` constructed without `liked_signature` defaults the field to `None`.
- `TrackMetadata` allows `None` for everything except `title`.
- `_StubProvider` with all 14 methods passes `isinstance(stub, Provider)` (proves the Protocol is correctly `runtime_checkable`).

---

## External Interfaces Consumed

This phase consumes no external interfaces (no HTTP responses, library return shapes, DB row shapes, third-party message payloads). It is pure scaffolding: dataclasses, a Protocol declaration, a registry skeleton, and a package marker. The coder does NOT need to capture any external samples.

---

## Notes

- **Foundation phase**: every other phase in this feature depends on `xmpd/providers/base.py` (Protocol + dataclasses) and the `xmpd/auth/` package marker. Get the dataclass field names and the Protocol method signatures exactly right -- subsequent planners derived their plans from these shapes verbatim.
- **`name` is an attribute, not a method**: in the Protocol, `name: str` is declared as a class-level attribute. Concrete providers set it via `class YTMusicProvider: name = "yt"` (class variable), not a property or method. `runtime_checkable` Protocol's isinstance check looks for attribute presence; the stub test sets `name = "stub"` to satisfy this.
- **Protocol method bodies are `...`**: per Python convention. Do NOT write `pass` or `raise NotImplementedError`; both are wrong for Protocol declarations.
- **Phase 1 deliberately ships an empty registry**: the concrete provider classes do not exist yet. `build_registry` returns `{}` regardless of config flags, with TODO comments referencing Phase 2 (yt branch) and Phase 9 (tidal branch). Do NOT try to import `YTMusicProvider` or `TidalProvider` -- those modules do not exist.
- **`from __future__ import annotations`**: use this in every new file so type hints are strings at runtime; avoids circular-import surprises and lets Python 3.11 idioms (`X | Y`, `list[X]`) coexist cleanly with the Protocol.
- **No `Optional[X]`**: use `X | None` everywhere. The project's ruff `UP` rule (`pyupgrade`) will flag `Optional[X]`.
- **No `List[X]` or `Tuple[...]`**: use `list[X]` / `tuple[X, Y]` lowercase parameterised builtins.
- **Do not modify any existing code**: this phase only creates new files. The `grep` audit is read-only -- if it reveals a deviation, fix it (one-line edit) and note it in the summary, but do not refactor anything else.
- **Logging deliverable scope**: this phase does NOT rebuild logging from scratch. Verify the existing infrastructure survived the rename. The infrastructure includes (a) every module's `getLogger(__name__)` call, (b) the root logger config in `xmpd/__main__.py`, and (c) `xmpd/daemon.py`'s log-handler setup that wires `log_file` into a `logging.FileHandler` for `~/.config/xmpd/xmpd.log`. All three must be intact post-rename. If any reference still says `ytmpd.log`, fix it.
- **Cleanup notes from Phase 0**: `xmpd/cookie_extract.py:67` uses `prefix="ytmpd_cookies_"` and `tests/test_xmpd_status_cli.py` has internal var names `_ytmpd_status_code`/`ytmpd_status`. These are NOT Phase 1's concern -- Phase 2 fixes the cookie-extract one when it moves the file; the test-file leftover is cosmetic and can be cleaned up any time. Mention them in the Phase 1 summary as "noted, deferred to Phase 2 / cosmetic" if you want, but do not touch them here.
- **Coverage baseline**: at the very end, run `pytest --cov=xmpd` once and record the resulting coverage percentage in the phase summary. PROJECT_PLAN.md's "Testing Strategy" calls this out as Phase 1's responsibility (set the baseline; subsequent phases keep at or above).
