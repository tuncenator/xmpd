"""Audio flow probe: source quality through MPD to physical sink.

Reports the audio quality chain for the currently playing track. Pure data
collection in ``probe_flow``; formatters render the resulting ``FlowReport``.
Tools used (all optional with graceful fallback):

- ``ffprobe`` for DASH manifest stream enumeration (already an xmpd dep)
- ``pactl`` for PulseAudio / PipeWire-as-Pulse sink details
- ``pw-dump`` (PipeWire) for Bluetooth codec; falls back to pactl card profile

Sink probing branches on the MPD output ``plugin`` field:
``pulse``/``pipewire`` go through pactl; ``alsa``/``jack``/``fifo`` are reported
with a note that detail extraction is not implemented (v1 scope: STORMTREE,
VICAR -- both PipeWire/Pulse).
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mpd import ConnectionError as MPDConnectionError
from mpd import MPDClient as MPDClientBase

# AirPlay bridge constants. The MPD FIFO output named here feeds OwnTone,
# which fans out to AirPlay/Chromecast receivers. When this FIFO is the
# active sink, we treat OwnTone as the real downstream and surface the
# selected receivers as the sink in the flow report.
_OWNTONE_API_URL = "http://localhost:3689/api/outputs"
_BRIDGE_FIFO_NAME = "Owntone Bridge"

# Bluetooth A2DP codec ceiling table.
# Maps lowercase codec key -> (display name, bitrate description, lossy flag).
_BT_CODECS: dict[str, tuple[str, str, bool]] = {
    "aac": ("AAC", "~256 kbps", True),
    "sbc": ("SBC", "~328 kbps", True),
    "sbc_xq": ("SBC-XQ", "~552 kbps", True),
    "aptx": ("aptX", "~352 kbps", True),
    "aptx_hd": ("aptX HD", "~576 kbps", True),
    "aptx_ll": ("aptX LL", "~352 kbps", True),
    "ldac": ("LDAC", "up to 990 kbps", True),
    "lc3": ("LC3", "up to 345 kbps", True),
    "opus": ("Opus", "up to 510 kbps", True),
}

_LOSSLESS_CODECS = {
    "flac", "alac", "wav", "wavpack", "ape", "tta", "monkey",
    "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le",
}

_LOSSY_CODECS = {
    "aac", "mp3", "opus", "vorbis", "ac3", "eac3", "wma", "wmav2", "wmapro",
    "mp2", "amr_nb", "amr_wb",
}

# Per-provider source-codec fallback when manifest probe fails.
# Tidal always serves FLAC (lossless tier or HiRes); YT always serves lossy.
_PROVIDER_CODEC: dict[str, tuple[str, bool]] = {
    "tidal": ("flac", True),
    "yt": ("aac", False),
}

_PROXY_URL_RE = re.compile(
    r"^https?://[^/]+/proxy/(?P<provider>[^/]+)/(?P<id>[^/?#]+)"
)


@dataclass
class StreamCandidate:
    """One audio adaptation in a DASH manifest."""
    index: int
    codec: str
    sample_fmt: str
    sample_rate: int
    channels: int
    bits_per_raw_sample: int | None
    bitrate: int | None
    tag_id: str | None  # "FLAC,44100,16" or "FLAC_HIRES,44100,24"


@dataclass
class SourceInfo:
    """Per-track source-side audio info."""
    provider: str
    track_id: str
    manifest_url: str | None
    candidates: list[StreamCandidate]
    selected: StreamCandidate | None
    error: str | None = None
    inferred: bool = False  # selected built from MPD audio + provider hint


@dataclass
class MPDInfo:
    """Snapshot of MPD state and the format it ingested."""
    state: str
    file: str
    artist: str | None
    title: str | None
    sample_rate: int | None
    bits: int | None
    channels: int | None
    elapsed: float | None
    duration: float | None
    active_outputs: list[dict[str, Any]]


@dataclass
class SinkInfo:
    """Output sink details, with Bluetooth-specific fields when applicable."""
    # "pulse" | "pipewire" | "alsa" | "jack" | "fifo" | "airplay" | "chromecast" | "unknown"
    backend: str
    name: str | None
    description: str | None
    state: str | None
    sample_fmt: str | None
    sample_rate: int | None
    channels: int | None
    is_bluetooth: bool
    bt_codec: str | None
    bt_codec_display: str | None
    bt_bitrate: str | None
    bt_lossy: bool | None
    error: str | None = None
    bits: int | None = None
    # Per-receiver detail when the sink is the OwnTone AirPlay/Chromecast fan-out.
    # Each entry is a dict with at least: name, type, volume, selected.
    airplay_outputs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Verdict:
    """End-to-end quality assessment."""
    label: str  # "BIT-PERFECT" | "LOSSLESS (resampled)" | "LOSSY" | "LOSSY (source)" | "UNKNOWN"
    detail: str
    bottleneck: str | None = None
    hints: list[str] = field(default_factory=list)


@dataclass
class FlowReport:
    """Aggregate report rendered by formatters."""
    mpd: MPDInfo | None
    source: SourceInfo | None
    sink: SinkInfo | None
    verdict: Verdict | None
    note: str | None = None


# ---------- public API ----------


def probe_flow(config: dict[str, Any]) -> FlowReport:
    """Probe MPD state, source manifest, and output sink. No I/O in formatters."""
    mpd = _probe_mpd(config)
    if mpd is None:
        return FlowReport(
            mpd=None, source=None, sink=None, verdict=None,
            note="MPD unreachable",
        )
    if mpd.state not in ("play", "pause"):
        return FlowReport(
            mpd=mpd, source=None, sink=None, verdict=None,
            note=f"no track playing (MPD state: {mpd.state})",
        )
    source = _probe_source(mpd, config)
    sink = _probe_sink(mpd.active_outputs)
    verdict = _compute_verdict(mpd, source, sink)
    return FlowReport(mpd=mpd, source=source, sink=sink, verdict=verdict)


# ---------- MPD probe ----------


def _probe_mpd(config: dict[str, Any]) -> MPDInfo | None:
    socket_path = config.get(
        "mpd_socket_path", str(Path.home() / ".config" / "mpd" / "socket")
    )
    client = MPDClientBase()
    client.timeout = 5
    try:
        if ":" in socket_path:
            host, port_str = socket_path.split(":", 1)
            client.connect(host, int(port_str))
        else:
            client.connect(socket_path, 0)
        status = client.status()
        current = client.currentsong()
        outputs = client.outputs()
    except (MPDConnectionError, ConnectionRefusedError, OSError, ValueError):
        return None
    finally:
        try:
            client.close()
            client.disconnect()
        except Exception:
            pass

    sample_rate, bits, channels = _parse_mpd_audio_field(status.get("audio", ""))
    elapsed = _safe_float(status.get("elapsed"))
    duration = _safe_float(status.get("duration"))

    active = [
        o for o in outputs
        if str(o.get("outputenabled", "0")) in ("1", "True", "true")
    ]

    return MPDInfo(
        state=status.get("state", "stop"),
        file=current.get("file", ""),
        artist=current.get("artist"),
        title=current.get("title"),
        sample_rate=sample_rate,
        bits=bits,
        channels=channels,
        elapsed=elapsed,
        duration=duration,
        active_outputs=active,
    )


def _parse_mpd_audio_field(audio: str) -> tuple[int | None, int | None, int | None]:
    """MPD ``audio`` is ``rate:bits:channels``; bits can be ``f`` (float) or ``dsd<n>``."""
    if not audio:
        return None, None, None
    parts = audio.split(":")
    if len(parts) != 3:
        return None, None, None
    rate = _safe_int(parts[0])
    if parts[1] == "f":
        bits = 32
    elif parts[1].startswith("dsd"):
        bits = 1
    else:
        bits = _safe_int(parts[1])
    channels = _safe_int(parts[2])
    return rate, bits, channels


# ---------- source probe ----------


def _probe_source(mpd: MPDInfo, config: dict[str, Any]) -> SourceInfo | None:
    if not mpd.file:
        return None
    m = _PROXY_URL_RE.match(mpd.file)
    if m:
        return _probe_proxy_source(mpd, config, m.group("provider"), m.group("id"))
    if "://" in mpd.file:
        return None
    return _probe_local_source(mpd, config)


def _probe_local_source(
    mpd: MPDInfo, config: dict[str, Any]
) -> SourceInfo:
    """Probe codec/format of a local file via ffprobe.

    Resolves ``mpd.file`` against ``mpd_music_directory`` when relative.
    """
    music_dir = config.get("mpd_music_directory") or str(Path.home() / "Music")
    path = Path(mpd.file)
    if not path.is_absolute():
        path = Path(music_dir).expanduser() / path
    track_id = path.name

    if not path.exists():
        return SourceInfo(
            provider="local", track_id=track_id, manifest_url=str(path),
            candidates=[], selected=None,
            error=f"file not found: {path}",
        )
    if shutil.which("ffprobe") is None:
        return SourceInfo(
            provider="local", track_id=track_id, manifest_url=str(path),
            candidates=[], selected=None,
            error="ffprobe not on PATH",
        )

    candidates: list[StreamCandidate] = []
    selected: StreamCandidate | None = None
    error: str | None = None
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", "-select_streams", "a", str(path),
            ],
            capture_output=True, text=True, timeout=15, check=False,
        )
        data = json.loads(proc.stdout) if proc.stdout else {}
        candidates = parse_dash_streams(data)
        selected = select_best_stream(candidates)
        if selected is None:
            error = "ffprobe returned no audio streams"
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as e:
        error = f"ffprobe failed: {e}"

    return SourceInfo(
        provider="local", track_id=track_id, manifest_url=str(path),
        candidates=candidates, selected=selected, error=error,
    )


def _probe_proxy_source(
    mpd: MPDInfo, config: dict[str, Any], provider: str, track_id: str
) -> SourceInfo:
    db_path = config.get("proxy_track_mapping_db") or str(
        Path.home() / ".config" / "xmpd" / "track_mapping.db"
    )
    stream_url = _lookup_cached_stream_url(db_path, provider, track_id)

    error: str | None = None
    candidates: list[StreamCandidate] = []
    selected: StreamCandidate | None = None

    if not stream_url:
        error = "no cached stream URL (daemon hasn't resolved this track yet)"
    elif shutil.which("ffprobe") is None:
        error = "ffprobe not on PATH"
    else:
        try:
            proc = subprocess.run(
                [
                    "ffprobe", "-v", "quiet", "-print_format", "json",
                    "-show_streams", "-select_streams", "a", stream_url,
                ],
                capture_output=True, text=True, timeout=15, check=False,
            )
            data = json.loads(proc.stdout) if proc.stdout else {}
            candidates = parse_dash_streams(data)
            selected = select_best_stream(candidates)
            if selected is None:
                error = (
                    "manifest probe returned no audio streams "
                    "(URL likely expired; daemon refreshes on next play)"
                )
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as e:
            error = f"ffprobe failed: {e}"

    inferred = False
    if selected is None and provider in _PROVIDER_CODEC:
        codec, _ = _PROVIDER_CODEC[provider]
        if mpd.sample_rate:
            selected = StreamCandidate(
                index=-1, codec=codec, sample_fmt="?",
                sample_rate=mpd.sample_rate,
                channels=mpd.channels or 2,
                bits_per_raw_sample=mpd.bits,
                bitrate=None, tag_id=None,
            )
            inferred = True

    return SourceInfo(
        provider=provider, track_id=track_id, manifest_url=stream_url,
        candidates=candidates, selected=selected,
        error=error, inferred=inferred,
    )


def _lookup_cached_stream_url(
    db_path: str, provider: str, track_id: str
) -> str | None:
    if not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT stream_url FROM tracks WHERE provider=? AND track_id=?",
            (provider, track_id),
        )
        row = cur.fetchone()
        return str(row[0]) if row and row[0] else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def parse_dash_streams(ffprobe_data: dict[str, Any]) -> list[StreamCandidate]:
    """Extract ``StreamCandidate``s from ffprobe ``-show_streams`` JSON."""
    out: list[StreamCandidate] = []
    for s in ffprobe_data.get("streams", []):
        out.append(
            StreamCandidate(
                index=_safe_int(s.get("index", 0)) or 0,
                codec=str(s.get("codec_name", "?")),
                sample_fmt=str(s.get("sample_fmt", "?")),
                sample_rate=_safe_int(s.get("sample_rate")) or 0,
                channels=_safe_int(s.get("channels")) or 0,
                bits_per_raw_sample=_safe_int(s.get("bits_per_raw_sample")),
                bitrate=_safe_int(s.get("bit_rate")),
                tag_id=(s.get("tags") or {}).get("id"),
            )
        )
    return out


def select_best_stream(
    candidates: list[StreamCandidate],
) -> StreamCandidate | None:
    """Mirror ``stream_proxy._probe_best_audio_stream``: highest bitrate."""
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.bitrate or 0)


# ---------- sink probe ----------


def _probe_sink(active_outputs: list[dict[str, Any]]) -> SinkInfo | None:
    if not active_outputs:
        return SinkInfo(
            backend="unknown", name=None, description=None, state=None,
            sample_fmt=None, sample_rate=None, channels=None,
            is_bluetooth=False, bt_codec=None, bt_codec_display=None,
            bt_bitrate=None, bt_lossy=None, error="no enabled MPD outputs",
        )

    # Preference order: a hardware-ish backend first, then the OwnTone bridge
    # FIFO (since it has a known downstream), then any FIFO, then whatever.
    # Multiple FIFOs are common (e.g. a visualizer tap alongside the bridge);
    # the previous code grabbed active_outputs[0] which was arbitrary.
    chosen: dict[str, Any] | None = None
    for o in active_outputs:
        if o.get("plugin") in ("pulse", "pipewire", "alsa", "jack"):
            chosen = o
            break
    if chosen is None:
        for o in active_outputs:
            if o.get("plugin") == "fifo" and o.get("outputname") == _BRIDGE_FIFO_NAME:
                chosen = o
                break
    if chosen is None:
        chosen = active_outputs[0]

    plugin = str(chosen.get("plugin", "unknown"))

    if plugin in ("pulse", "pipewire"):
        return _probe_pulse_sink()
    if plugin == "alsa":
        return SinkInfo(
            backend="alsa", name=str(chosen.get("outputname") or "alsa"),
            description=str(chosen.get("device") or chosen.get("outputname") or ""),
            state=None, sample_fmt=None, sample_rate=None, channels=None,
            is_bluetooth=False, bt_codec=None, bt_codec_display=None,
            bt_bitrate=None, bt_lossy=None,
            error="ALSA hw_params probe not implemented in v1",
        )
    if plugin == "jack":
        return SinkInfo(
            backend="jack", name=str(chosen.get("outputname") or "jack"),
            description="JACK", state=None,
            sample_fmt=None, sample_rate=None, channels=None,
            is_bluetooth=False, bt_codec=None, bt_codec_display=None,
            bt_bitrate=None, bt_lossy=None,
            error="JACK probe not implemented in v1",
        )
    if plugin == "fifo":
        if chosen.get("outputname") == _BRIDGE_FIFO_NAME:
            return _probe_owntone_sink(chosen)
        return SinkInfo(
            backend="fifo", name=str(chosen.get("outputname") or "fifo"),
            description="FIFO bridge",
            state=None, sample_fmt=None, sample_rate=None, channels=None,
            is_bluetooth=False, bt_codec=None, bt_codec_display=None,
            bt_bitrate=None, bt_lossy=None,
            error="FIFO output, downstream sink not introspected",
        )
    return SinkInfo(
        backend=plugin, name=str(chosen.get("outputname") or plugin),
        description=None, state=None,
        sample_fmt=None, sample_rate=None, channels=None,
        is_bluetooth=False, bt_codec=None, bt_codec_display=None,
        bt_bitrate=None, bt_lossy=None,
        error=f"unsupported MPD plugin: {plugin}",
    )


# ---------- OwnTone (AirPlay / Chromecast bridge) probe ----------


def _probe_owntone_sink(fifo_output: dict[str, Any]) -> SinkInfo:
    """Resolve the real downstream when MPD's active output is the bridge FIFO.

    Calls the OwnTone REST API at localhost:3689 and surfaces the selected
    AirPlay/Chromecast receivers as the sink. Sample format reflects the FIFO
    parameters from mpd.conf (typically 44100:16:2), which is what OwnTone
    actually sees -- not MPD's input format.
    """
    fifo_name = str(fifo_output.get("outputname") or _BRIDGE_FIFO_NAME)
    fifo_rate, fifo_bits, fifo_ch = _read_fifo_format(fifo_name)

    req = urllib.request.Request(_OWNTONE_API_URL)
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return SinkInfo(
            backend="airplay", name=fifo_name,
            description="OwnTone bridge (API unreachable)",
            state=None, sample_fmt=None,
            sample_rate=fifo_rate, channels=fifo_ch, bits=fifo_bits,
            is_bluetooth=False, bt_codec=None, bt_codec_display=None,
            bt_bitrate=None, bt_lossy=None,
            error=f"OwnTone API error: {e}",
        )

    outputs = data.get("outputs", []) if isinstance(data, dict) else []
    selected = [
        o for o in outputs
        if o.get("selected")
        and isinstance(o.get("type"), str)
        and o["type"].startswith(("AirPlay", "Chromecast"))
    ]
    if not selected:
        return SinkInfo(
            backend="airplay", name=fifo_name,
            description="OwnTone bridge (no receiver selected)",
            state="idle", sample_fmt=None,
            sample_rate=fifo_rate, channels=fifo_ch, bits=fifo_bits,
            is_bluetooth=False, bt_codec=None, bt_codec_display=None,
            bt_bitrate=None, bt_lossy=None,
            error="FIFO drains to OwnTone but no AirPlay/Chromecast output is selected",
        )

    types = sorted({str(o.get("type", "")) for o in selected})
    backend = "chromecast" if all("Chromecast" in t for t in types) else "airplay"
    receiver_names = [str(o.get("name", "?")) for o in selected]
    name = (
        receiver_names[0] if len(selected) == 1
        else f"{len(selected)} receivers"
    )
    description = (
        f"OwnTone {types[0]} bridge" if len(types) == 1
        else "OwnTone bridge (" + ", ".join(types) + ")"
    )
    sample_fmt = None
    if fifo_bits is not None:
        codec = "ALAC" if backend == "airplay" else "PCM"
        sample_fmt = f"{codec} {fifo_bits}-bit"

    return SinkInfo(
        backend=backend, name=name, description=description,
        state="RUNNING", sample_fmt=sample_fmt,
        sample_rate=fifo_rate, channels=fifo_ch, bits=fifo_bits,
        is_bluetooth=False, bt_codec=None, bt_codec_display=None,
        bt_bitrate=None, bt_lossy=None,
        airplay_outputs=selected,
    )


_MPD_AUDIO_OUTPUT_BLOCK_RE = re.compile(
    r"audio_output\s*\{([^}]*)\}", re.DOTALL,
)
_MPD_KV_RE = re.compile(r'(\w+)\s+"([^"]*)"')


def _read_fifo_format(
    output_name: str, mpd_conf: Path | None = None,
) -> tuple[int | None, int | None, int | None]:
    """Parse mpd.conf, return (rate, bits, channels) for the named FIFO block.

    Returns (None, None, None) if the file or block is missing, or if the block
    has no ``format`` directive. The bridge installer writes 44100:16:2; users
    may have overridden it.
    """
    if mpd_conf is None:
        mpd_conf = Path.home() / ".mpd" / "mpd.conf"
    if not mpd_conf.exists():
        return None, None, None
    try:
        text = mpd_conf.read_text()
    except OSError:
        return None, None, None

    for block in _MPD_AUDIO_OUTPUT_BLOCK_RE.finditer(text):
        body = block.group(1)
        kvs = dict(_MPD_KV_RE.findall(body))
        if kvs.get("name") != output_name:
            continue
        fmt = kvs.get("format")
        if not fmt:
            return None, None, None
        return _parse_mpd_audio_field(fmt)
    return None, None, None


def _probe_pulse_sink() -> SinkInfo:
    """Probe the sink MPD is actually streaming to, codec via ``pw-dump``.

    Resolution order:
    1. ``pactl list sink-inputs`` -> find MPD's stream, read its target sink id.
    2. Fall back to ``pactl get-default-sink`` if MPD isn't found in sink-inputs.
    """
    if shutil.which("pactl") is None:
        return SinkInfo(
            backend="pulse", name=None, description=None, state=None,
            sample_fmt=None, sample_rate=None, channels=None,
            is_bluetooth=False, bt_codec=None, bt_codec_display=None,
            bt_bitrate=None, bt_lossy=None, error="pactl not on PATH",
        )

    try:
        default = subprocess.run(
            ["pactl", "get-default-sink"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()
        sinks_blob = subprocess.run(
            ["pactl", "list", "sinks"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
        cards_blob = subprocess.run(
            ["pactl", "list", "cards"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
        sink_inputs_blob = subprocess.run(
            ["pactl", "list", "sink-inputs"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (subprocess.SubprocessError, OSError) as e:
        return SinkInfo(
            backend="pulse", name=None, description=None, state=None,
            sample_fmt=None, sample_rate=None, channels=None,
            is_bluetooth=False, bt_codec=None, bt_codec_display=None,
            bt_bitrate=None, bt_lossy=None, error=f"pactl failed: {e}",
        )

    sink_name = mpd_sink_from_inputs(sink_inputs_blob, sinks_blob) or default
    props = parse_pactl_sink_block(sinks_blob, sink_name)
    bt_addr = props.get("api.bluez5.address")
    is_bt = bool(bt_addr) or props.get("device.api") == "bluez5" \
        or "bluez" in default.lower()

    bt_codec = bt_codec_display = bt_bitrate = None
    bt_lossy: bool | None = None
    if is_bt:
        codec_raw = _bt_codec_from_pwdump(bt_addr)
        if not codec_raw:
            codec_raw = codec_from_pactl_cards(cards_blob, bt_addr)
        if codec_raw:
            entry = _BT_CODECS.get(codec_raw.lower())
            if entry:
                bt_codec_display, bt_bitrate, bt_lossy = entry
            else:
                bt_codec_display = codec_raw.upper()
                bt_bitrate = "unknown"
                bt_lossy = True
            bt_codec = codec_raw

    return SinkInfo(
        backend="pulse",
        name=default or None,
        description=props.get("description"),
        state=props.get("state"),
        sample_fmt=props.get("sample_fmt"),
        sample_rate=props.get("sample_rate"),
        channels=props.get("channels"),
        is_bluetooth=is_bt,
        bt_codec=bt_codec,
        bt_codec_display=bt_codec_display,
        bt_bitrate=bt_bitrate,
        bt_lossy=bt_lossy,
    )


_PACTL_SAMPLE_RE = re.compile(
    r"^\s*Sample Specification:\s*(\S+)\s+(\d+)ch\s+(\d+)Hz", re.M
)
_PACTL_DESC_RE = re.compile(r"^\s*Description:\s*(.+)$", re.M)
_PACTL_NAME_RE = re.compile(r"^\s*Name:\s*(\S+)$", re.M)
_PACTL_STATE_RE = re.compile(r"^\s*State:\s*(\S+)$", re.M)
_PACTL_CARD_PROFILE_RE = re.compile(r"^\s*Active Profile:\s*(\S+)$", re.M)
_A2DP_PROFILE_SUFFIX_RE = re.compile(r"^a2dp-sink-(\S+)$")


def mpd_sink_from_inputs(
    sink_inputs_blob: str, sinks_blob: str
) -> str | None:
    """Find the sink MPD is streaming to via ``pactl list sink-inputs``.

    Returns the target sink's ``Name:`` (matching ``pactl list sinks``), or
    ``None`` if no MPD sink-input is found.
    """
    blocks = re.split(r"^Sink Input #\d+\s*$", sink_inputs_blob, flags=re.M)
    target_id: str | None = None
    for blk in blocks:
        if not blk.strip():
            continue
        is_mpd = (
            'application.process.binary = "mpd"' in blk
            or 'application.name = "Music Player Daemon"' in blk
            or 'media.role = "Music"' in blk and "mpd" in blk.lower()
        )
        if not is_mpd:
            continue
        m = re.search(r"^\s*Sink:\s*(\d+)\s*$", blk, re.M)
        if m:
            target_id = m.group(1)
            break
    if target_id is None:
        return None
    sink_blocks = re.split(r"^Sink #(\d+)\s*$", sinks_blob, flags=re.M)
    for i in range(1, len(sink_blocks), 2):
        if sink_blocks[i] == target_id:
            body = sink_blocks[i + 1]
            nm = _PACTL_NAME_RE.search(body)
            if nm:
                return nm.group(1)
    return None


def parse_pactl_sink_block(sinks_blob: str, sink_name: str) -> dict[str, Any]:
    """Find the sink block by Name and parse format/state/select properties."""
    if not sink_name:
        return {}
    blocks = re.split(r"^Sink #\d+\s*$", sinks_blob, flags=re.M)
    target: str | None = None
    for blk in blocks:
        if not blk.strip():
            continue
        m = _PACTL_NAME_RE.search(blk)
        if m and m.group(1) == sink_name:
            target = blk
            break
    if target is None:
        return {}
    props: dict[str, Any] = {"name": sink_name}
    md = _PACTL_DESC_RE.search(target)
    if md:
        props["description"] = md.group(1).strip()
    ms = _PACTL_STATE_RE.search(target)
    if ms:
        props["state"] = ms.group(1)
    msa = _PACTL_SAMPLE_RE.search(target)
    if msa:
        props["sample_fmt"] = msa.group(1)
        props["channels"] = int(msa.group(2))
        props["sample_rate"] = int(msa.group(3))
    for key in ("device.api", "api.bluez5.address"):
        m = re.search(
            rf'^\s*{re.escape(key)}\s*=\s*"([^"]*)"$', target, re.M
        )
        if m:
            props[key] = m.group(1)
    return props


def _bt_codec_from_pwdump(bt_addr: str | None) -> str | None:
    if not bt_addr or shutil.which("pw-dump") is None:
        return None
    try:
        out = subprocess.run(
            ["pw-dump"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
        data = json.loads(out)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, list):
        return None
    for item in data:
        if not isinstance(item, dict):
            continue
        info = item.get("info") or {}
        props = info.get("props") or {}
        if props.get("api.bluez5.address") != bt_addr:
            continue
        codec = props.get("api.bluez5.codec") or props.get("bluez5.codec")
        if codec:
            return _normalize_codec(str(codec))
    return None


def codec_from_pactl_cards(
    cards_blob: str, bt_addr: str | None
) -> str | None:
    """Extract active A2DP codec from ``pactl list cards`` output."""
    if not bt_addr:
        return None
    blocks = re.split(r"^Card #\d+\s*$", cards_blob, flags=re.M)
    sanitized = bt_addr.replace(":", "_")
    for blk in blocks:
        if sanitized not in blk and bt_addr not in blk:
            continue
        m = _PACTL_CARD_PROFILE_RE.search(blk)
        if not m:
            continue
        active = m.group(1)
        prof_line = re.search(
            rf"^\s*{re.escape(active)}:\s*([^\n]+)$", blk, re.M
        )
        if prof_line:
            cm = re.search(r"codec\s+([A-Za-z0-9_-]+)", prof_line.group(1))
            if cm:
                return _normalize_codec(cm.group(1))
        sm = _A2DP_PROFILE_SUFFIX_RE.match(active)
        if sm:
            return _normalize_codec(sm.group(1))
    return None


def _normalize_codec(raw: str) -> str:
    return raw.lower().replace("-", "_")


# ---------- verdict ----------


def _compute_verdict(
    mpd: MPDInfo, source: SourceInfo | None, sink: SinkInfo | None
) -> Verdict:
    src_codec = source.selected.codec.lower() if source and source.selected else None

    if src_codec is not None:
        if src_codec in _LOSSY_CODECS:
            src_lossless: bool | None = False
        elif src_codec in _LOSSLESS_CODECS:
            src_lossless = True
        else:
            src_lossless = None
    else:
        src_lossless = None

    src_rate = source.selected.sample_rate if source and source.selected else None
    src_bits = source.selected.bits_per_raw_sample if source and source.selected else None
    sink_rate = sink.sample_rate if sink else None
    sink_bits = sink.bits if sink else None
    sink_lossy = bool(sink and sink.is_bluetooth and sink.bt_lossy)
    rate_mismatch = bool(src_rate and sink_rate and src_rate != sink_rate)
    bits_truncated = bool(src_bits and sink_bits and src_bits > sink_bits)
    mpd_rate = mpd.sample_rate
    if not src_rate and mpd_rate and sink_rate and mpd_rate != sink_rate:
        rate_mismatch = True

    if src_lossless is False:
        return Verdict(
            label="LOSSY (source)",
            detail=f"source codec {src_codec} is itself lossy",
            bottleneck="source",
        )

    if sink_lossy and sink is not None:
        codec_str = sink.bt_codec_display or "lossy codec"
        bitrate = sink.bt_bitrate or "unknown bitrate"
        hints = ["switch to a wired ALSA sink for bit-perfect playback"]
        if sink.bt_codec != "ldac":
            hints.append(
                "or pair an LDAC source/sink for higher-bitrate (still lossy) Bluetooth"
            )
        return Verdict(
            label="LOSSY",
            detail=f"sink re-encodes via {codec_str} ({bitrate})",
            bottleneck="Bluetooth A2DP encoder",
            hints=hints,
        )

    if src_lossless and not sink_lossy:
        if rate_mismatch and src_rate and sink_rate:
            return Verdict(
                label="LOSSLESS (resampled)",
                detail=f"source {src_rate} Hz resampled to {sink_rate} Hz at the sink",
                bottleneck="resampler",
                hints=[
                    f"set the sink sample rate to {src_rate} Hz for bit-perfect playback",
                ],
            )
        if bits_truncated:
            bottleneck = (
                "bridge FIFO bit-depth" if sink and sink.backend in ("airplay", "chromecast")
                else "sink bit-depth"
            )
            hint = (
                f'set the "{_BRIDGE_FIFO_NAME}" audio_output format in mpd.conf to '
                f'"{src_rate}:{src_bits}:2" if the receiver supports it'
                if sink and sink.backend in ("airplay", "chromecast")
                else f"configure the sink for {src_bits}-bit playback"
            )
            return Verdict(
                label="LOSSLESS (bit-depth truncated)",
                detail=(
                    f"source {src_bits}-bit truncated to {sink_bits}-bit "
                    "before reaching the sink"
                ),
                bottleneck=bottleneck,
                hints=[hint],
            )
        return Verdict(
            label="BIT-PERFECT",
            detail="source rate matches sink rate, no lossy encode",
        )

    return Verdict(
        label="UNKNOWN",
        detail="source codec or sink details unavailable",
    )


# ---------- formatters ----------


def format_default(report: FlowReport, color: Any) -> str:
    """Sectioned format matching ``xmpctl status`` aesthetic. ``color(text, name)``."""
    lines: list[str] = []
    lines.append(color("=== xmpd Audio Flow ===", "bold"))
    lines.append("")

    if report.mpd is None:
        lines.append(report.note or "MPD unreachable")
        return "\n".join(lines)

    mpd = report.mpd
    title = f"{mpd.artist or '?'} - {mpd.title or '?'}"
    lines.append(f"Track:        {title}")
    if mpd.elapsed is not None and mpd.duration is not None:
        lines.append(
            f"Position:     {_fmt_time(mpd.elapsed)} / {_fmt_time(mpd.duration)}"
        )
    if report.source and report.source.provider and report.source.provider != "local":
        lines.append(
            f"Provider:     {report.source.provider}/{report.source.track_id}"
        )
    elif mpd.file:
        lines.append(f"File:         {mpd.file}")
    lines.append("")

    if report.note:
        lines.append(report.note)
        return "\n".join(lines)

    src = report.source
    lines.append(color("=== Source ===", "bold"))
    if src is None:
        lines.append("(local file or non-proxy URL -- no manifest probe)")
    elif src.selected:
        sel = src.selected
        codec_label = sel.codec.upper()
        if sel.codec.lower() in _LOSSLESS_CODECS:
            codec_label += " (lossless)"
        elif sel.codec.lower() in _LOSSY_CODECS:
            codec_label += " (lossy)"
        if src.inferred:
            codec_label += "  [inferred]"
        lines.append(f"Codec:        {codec_label}")
        lines.append(
            "Format:       "
            + format_audio_spec(
                sel.sample_rate, sel.bits_per_raw_sample, sel.channels
            )
        )
        if sel.bitrate:
            lines.append(f"Bitrate:      {sel.bitrate // 1000} kbps")
        if sel.tag_id:
            lines.append(f"Tier:         {sel.tag_id}")
        if len(src.candidates) > 1:
            lines.append(
                f"Adaptations:  {len(src.candidates)} (selected highest-bitrate)"
            )
        if src.inferred and src.error:
            lines.append(f"Note:         {src.error}")
    elif src.error:
        lines.append(f"Error:        {src.error}")
    else:
        lines.append("(no source data)")
    lines.append("")

    if src is not None and src.selected is not None and src.provider != "local":
        lines.append(color("=== Proxy ===", "bold"))
        lines.append("Path:         ffmpeg -c copy -f flac (rewrap, no re-encode)")
        lines.append("")

    lines.append(color("=== MPD ===", "bold"))
    lines.append(f"State:        {mpd.state}")
    if mpd.sample_rate:
        lines.append(
            "Audio:        "
            + format_audio_spec(mpd.sample_rate, mpd.bits, mpd.channels)
        )
    lines.append("")

    lines.append(color("=== Output ===", "bold"))
    sink = report.sink
    if sink is None:
        lines.append("(no sink info)")
    else:
        if sink.name:
            state_str = f"  [{sink.state}]" if sink.state else ""
            lines.append(f"Sink:         {sink.name}{state_str}")
        if sink.description:
            lines.append(f"Device:       {sink.description}")
        if sink.sample_rate:
            spec = format_audio_spec(sink.sample_rate, sink.bits, sink.channels)
            fmt = f" ({sink.sample_fmt})" if sink.sample_fmt else ""
            resample = ""
            mpd_r = mpd.sample_rate
            if mpd_r and sink.sample_rate and mpd_r != sink.sample_rate:
                resample = f"   resample {mpd_r} -> {sink.sample_rate}"
            lines.append(f"Mix format:   {spec}{fmt}{resample}")
        if sink.airplay_outputs:
            label = "Receivers" if len(sink.airplay_outputs) > 1 else "Receiver"
            for o in sink.airplay_outputs:
                rname = o.get("name", "?")
                rtype = o.get("type", "?")
                rvol = o.get("volume", "?")
                lines.append(f"{label}:    {rname}  [{rtype}, vol={rvol}]")
                label = " " * len(label)
        if sink.is_bluetooth:
            codec_str = sink.bt_codec_display or "unknown"
            br = sink.bt_bitrate or ""
            lossy = "lossy" if sink.bt_lossy else "lossless"
            extra = f"  {br}" if br else ""
            lines.append(f"BT codec:     {codec_str}{extra}  ({lossy})")
        if sink.error:
            lines.append(f"Note:         {sink.error}")
    lines.append("")

    lines.append(color("=== Verdict ===", "bold"))
    v = report.verdict
    if v is None:
        lines.append("(unavailable)")
    else:
        verdict_color = (
            "green" if v.label == "BIT-PERFECT"
            else "yellow" if v.label.startswith("LOSSLESS")
            else "red" if v.label.startswith("LOSSY")
            else "yellow"
        )
        lines.append(f"End-to-end:   {color(v.label, verdict_color)}")
        if v.detail:
            lines.append(f"Detail:       {v.detail}")
        if v.bottleneck:
            lines.append(f"Bottleneck:   {v.bottleneck}")
        for h in v.hints:
            lines.append(f"Hint:         {h}")

    return "\n".join(lines)


def format_short(report: FlowReport) -> str:
    """One-liner for status bars or scripts."""
    if report.mpd is None:
        return report.note or "MPD unreachable"
    if report.note and report.verdict is None:
        return report.note

    parts: list[str] = []
    src = report.source
    if src and src.selected:
        sel = src.selected
        codec = sel.codec.upper()
        bps = sel.bits_per_raw_sample
        rate_khz = sel.sample_rate / 1000 if sel.sample_rate else 0
        prov = src.track_id if src.provider == "local" else f"{src.provider}/{src.track_id}"
        bps_part = f"{bps}/" if bps else ""
        br_part = f" ({sel.bitrate // 1000}k)" if sel.bitrate else ""
        parts.append(f"{prov}  {codec} {bps_part}{rate_khz:.1f}{br_part}")
    elif src:
        prov = src.track_id if src.provider == "local" else f"{src.provider}/{src.track_id}"
        parts.append(f"{prov}  source ?")
    else:
        mpd_file = report.mpd.file
        parts.append(mpd_file.split("/")[-1] if mpd_file else "?")

    mpd = report.mpd
    if mpd.sample_rate:
        bits_str = f"{mpd.bits}/" if mpd.bits else ""
        parts.append(f"-> MPD {bits_str}{mpd.sample_rate / 1000:.1f}")

    sink = report.sink
    if sink:
        if sink.is_bluetooth:
            tail = f" {sink.sample_rate // 1000}k" if sink.sample_rate else ""
            parts.append(f"-> {sink.bt_codec_display or 'BT?'}{tail}")
        elif sink.sample_rate:
            parts.append(f"-> {sink.backend} {sink.sample_rate / 1000:.1f}k")
        else:
            parts.append(f"-> {sink.backend}")

    if report.verdict:
        parts.append(f"[{report.verdict.label.lower()}]")

    return "  ".join(p for p in parts if p)


def format_brief(report: FlowReport) -> str:
    """One- or two-sentence verdict for scripts."""
    if report.mpd is None:
        return report.note or "MPD unreachable"
    if report.verdict is None:
        return report.note or "no playback"
    v = report.verdict
    body = [v.label, v.detail]
    if v.hints:
        body.append("Hint: " + v.hints[0])
    return ". ".join(p for p in body if p) + "."


def format_audio_spec(
    rate: int | None, bits: int | None, channels: int | None
) -> str:
    """Render ``rate/bits/channels`` as ``24-bit / 44.1 kHz / 2 ch``."""
    parts: list[str] = []
    if bits is not None:
        parts.append(f"{bits}-bit")
    if rate:
        if rate >= 1000:
            parts.append(f"{rate / 1000:.1f} kHz")
        else:
            parts.append(f"{rate} Hz")
    if channels:
        parts.append(f"{channels} ch")
    return " / ".join(parts) if parts else "?"


def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _safe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
