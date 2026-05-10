"""Tests for xmpd.audio_flow probes, parsers, verdict logic, and formatters."""

import json

from xmpd import audio_flow
from xmpd.audio_flow import (
    FlowReport,
    MPDInfo,
    SinkInfo,
    SourceInfo,
    StreamCandidate,
    Verdict,
    _compute_verdict,
    _parse_mpd_audio_field,
    _probe_local_source,
    _probe_source,
    codec_from_pactl_cards,
    format_audio_spec,
    format_brief,
    format_default,
    format_short,
    parse_dash_streams,
    parse_pactl_sink_block,
    select_best_stream,
)

# ffprobe -show_streams output captured from a real Tidal HiRes manifest.
_DASH_FFPROBE_FIXTURE = {
    "streams": [
        {
            "index": 0,
            "codec_name": "flac",
            "sample_fmt": "s16",
            "sample_rate": "44100",
            "channels": 2,
            "bits_per_raw_sample": "16",
            "bit_rate": "933319",
            "tags": {"id": "FLAC,44100,16"},
        },
        {
            "index": 1,
            "codec_name": "flac",
            "sample_fmt": "s32",
            "sample_rate": "44100",
            "channels": 2,
            "bits_per_raw_sample": "24",
            "bit_rate": "972431",
            "tags": {"id": "FLAC_HIRES,44100,24"},
        },
    ]
}


_PACTL_SINKS_FIXTURE = """Sink #67
\tState: SUSPENDED
\tName: alsa_output.pci-0000_04_00.6.analog-stereo
\tDescription: Ryzen HD Audio Controller Analog Stereo
\tSample Specification: s32le 2ch 48000Hz
\tChannel Map: front-left,front-right
Sink #869
\tState: RUNNING
\tName: bluez_output.18:4E:16:39:FD:01
\tDescription: [SELF] Galaxy Buds+ (FD01)
\tDriver: PipeWire
\tSample Specification: float32le 2ch 48000Hz
\tChannel Map: front-left,front-right
\tProperties:
\t\tdevice.api = "bluez5"
\t\tapi.bluez5.address = "18:4E:16:39:FD:01"
\t\tdevice.description = "[SELF] Galaxy Buds+ (FD01)"
"""


_PACTL_CARDS_FIXTURE = """Card #58
\tName: bluez_card.18_4E_16_39_FD_01
\tDriver: bluez5
\tProfiles:
\t\ta2dp-sink-sbc: High Fidelity Playback (A2DP Sink, codec SBC) (available: yes)
\t\ta2dp-sink-sbc_xq: High Fidelity Playback (A2DP Sink, codec SBC-XQ) (available: yes)
\t\ta2dp-sink: High Fidelity Playback (A2DP Sink, codec AAC) (available: yes)
\t\theadset-head-unit: Headset Head Unit (HSP/HFP, codec MSBC) (available: yes)
\tActive Profile: a2dp-sink
"""


_PACTL_CARDS_PULSEAUDIO_FIXTURE = """Card #58
\tName: bluez_card.AA_BB_CC_DD_EE_FF
\tProfiles:
\t\ta2dp-sink-aac: High Fidelity Playback (codec AAC)
\t\ta2dp-sink-sbc_xq: High Fidelity Playback (codec SBC-XQ)
\tActive Profile: a2dp-sink-sbc_xq
"""


class TestParseDashStreams:
    def test_extracts_two_candidates_from_real_fixture(self):
        candidates = parse_dash_streams(_DASH_FFPROBE_FIXTURE)
        assert len(candidates) == 2
        assert candidates[0].codec == "flac"
        assert candidates[0].bits_per_raw_sample == 16
        assert candidates[0].bitrate == 933319
        assert candidates[1].bits_per_raw_sample == 24
        assert candidates[1].tag_id == "FLAC_HIRES,44100,24"

    def test_returns_empty_for_no_streams(self):
        assert parse_dash_streams({}) == []
        assert parse_dash_streams({"streams": []}) == []

    def test_handles_missing_optional_fields(self):
        data = {"streams": [{"index": 0, "codec_name": "flac"}]}
        c = parse_dash_streams(data)[0]
        assert c.codec == "flac"
        assert c.bitrate is None
        assert c.bits_per_raw_sample is None
        assert c.tag_id is None


class TestSelectBestStream:
    def test_picks_highest_bitrate(self):
        candidates = parse_dash_streams(_DASH_FFPROBE_FIXTURE)
        best = select_best_stream(candidates)
        assert best is not None
        assert best.index == 1
        assert best.bits_per_raw_sample == 24

    def test_returns_none_for_empty_list(self):
        assert select_best_stream([]) is None

    def test_handles_missing_bitrate_as_zero(self):
        a = StreamCandidate(0, "flac", "s16", 44100, 2, 16, None, None)
        b = StreamCandidate(1, "flac", "s24", 44100, 2, 24, 1000, None)
        assert select_best_stream([a, b]) is b


class TestParsePactlSinkBlock:
    def test_parses_sample_spec_and_state(self):
        props = parse_pactl_sink_block(
            _PACTL_SINKS_FIXTURE, "bluez_output.18:4E:16:39:FD:01"
        )
        assert props["sample_fmt"] == "float32le"
        assert props["sample_rate"] == 48000
        assert props["channels"] == 2
        assert props["state"] == "RUNNING"
        assert props["description"] == "[SELF] Galaxy Buds+ (FD01)"
        assert props["api.bluez5.address"] == "18:4E:16:39:FD:01"
        assert props["device.api"] == "bluez5"

    def test_returns_empty_for_unknown_sink(self):
        assert parse_pactl_sink_block(_PACTL_SINKS_FIXTURE, "nope") == {}

    def test_returns_empty_for_blank_name(self):
        assert parse_pactl_sink_block(_PACTL_SINKS_FIXTURE, "") == {}


class TestCodecFromPactlCards:
    def test_pipewire_active_profile_uses_description_codec(self):
        codec = codec_from_pactl_cards(
            _PACTL_CARDS_FIXTURE, "18:4E:16:39:FD:01"
        )
        assert codec == "aac"

    def test_pulseaudio_codec_suffix(self):
        codec = codec_from_pactl_cards(
            _PACTL_CARDS_PULSEAUDIO_FIXTURE, "AA:BB:CC:DD:EE:FF"
        )
        assert codec == "sbc_xq"

    def test_unknown_address_returns_none(self):
        assert codec_from_pactl_cards(_PACTL_CARDS_FIXTURE, "XX:XX:XX:XX:XX:XX") is None

    def test_no_address_returns_none(self):
        assert codec_from_pactl_cards(_PACTL_CARDS_FIXTURE, None) is None


class TestParseMPDAudioField:
    def test_pcm_24_bit(self):
        rate, bits, ch = _parse_mpd_audio_field("44100:24:2")
        assert rate == 44100
        assert bits == 24
        assert ch == 2

    def test_float_format_normalizes_to_32(self):
        rate, bits, ch = _parse_mpd_audio_field("48000:f:2")
        assert bits == 32

    def test_dsd_format_is_one_bit(self):
        rate, bits, ch = _parse_mpd_audio_field("2822400:dsd64:2")
        assert bits == 1

    def test_empty_returns_all_none(self):
        assert _parse_mpd_audio_field("") == (None, None, None)

    def test_malformed_returns_all_none(self):
        assert _parse_mpd_audio_field("44100:bogus") == (None, None, None)


class TestComputeVerdict:
    def _mpd(self, rate=44100, bits=24, ch=2):
        return MPDInfo(
            state="play", file="x", artist=None, title=None,
            sample_rate=rate, bits=bits, channels=ch,
            elapsed=0.0, duration=100.0, active_outputs=[],
        )

    def _src(self, codec="flac", rate=44100, bits=24, bitrate=972000):
        cand = StreamCandidate(
            index=0, codec=codec, sample_fmt="s32",
            sample_rate=rate, channels=2,
            bits_per_raw_sample=bits, bitrate=bitrate, tag_id=None,
        )
        return SourceInfo(
            provider="tidal", track_id="x", manifest_url="u",
            candidates=[cand], selected=cand,
        )

    def _sink(self, *, bt=False, bt_codec="aac", bt_lossy=True, rate=48000):
        codec_display = bt_codec.upper() if bt else None
        return SinkInfo(
            backend="pulse", name="x", description="x", state="RUNNING",
            sample_fmt="float32le", sample_rate=rate, channels=2,
            is_bluetooth=bt, bt_codec=bt_codec if bt else None,
            bt_codec_display=codec_display,
            bt_bitrate="~256 kbps" if bt else None,
            bt_lossy=bt_lossy if bt else None,
        )

    def test_lossless_bluetooth_sink_is_lossy(self):
        v = _compute_verdict(self._mpd(), self._src(), self._sink(bt=True))
        assert v.label == "LOSSY"
        assert "AAC" in v.detail
        assert v.bottleneck and "Bluetooth" in v.bottleneck

    def test_lossless_source_wired_resampled(self):
        v = _compute_verdict(self._mpd(), self._src(), self._sink(rate=48000))
        assert v.label == "LOSSLESS (resampled)"
        assert "44100" in v.detail and "48000" in v.detail

    def test_lossless_source_wired_matched_rate_is_bit_perfect(self):
        v = _compute_verdict(self._mpd(), self._src(), self._sink(rate=44100))
        assert v.label == "BIT-PERFECT"

    def test_lossy_source_short_circuits(self):
        v = _compute_verdict(
            self._mpd(), self._src(codec="aac"), self._sink(rate=44100)
        )
        assert v.label == "LOSSY (source)"

    def test_unknown_when_no_source(self):
        v = _compute_verdict(self._mpd(), None, self._sink(rate=44100))
        assert v.label == "UNKNOWN"

    def test_ldac_bluetooth_omits_ldac_hint(self):
        sink = self._sink(bt=True, bt_codec="ldac")
        v = _compute_verdict(self._mpd(), self._src(), sink)
        assert v.label == "LOSSY"
        assert not any("LDAC" in h for h in v.hints)


class TestFormatters:
    def _full_report(self):
        cand = StreamCandidate(
            index=1, codec="flac", sample_fmt="s32",
            sample_rate=44100, channels=2,
            bits_per_raw_sample=24, bitrate=972000,
            tag_id="FLAC_HIRES,44100,24",
        )
        src = SourceInfo(
            provider="tidal", track_id="397955552",
            manifest_url="https://m/x.mpd",
            candidates=[cand, cand], selected=cand,
        )
        mpd = MPDInfo(
            state="play", file="http://localhost:6602/proxy/tidal/397955552",
            artist="copperplate", title="subtle senses",
            sample_rate=44100, bits=24, channels=2,
            elapsed=120.0, duration=221.0, active_outputs=[],
        )
        sink = SinkInfo(
            backend="pulse", name="bluez_output.x",
            description="Galaxy Buds+", state="RUNNING",
            sample_fmt="float32le", sample_rate=48000, channels=2,
            is_bluetooth=True, bt_codec="aac", bt_codec_display="AAC",
            bt_bitrate="~256 kbps", bt_lossy=True,
        )
        verdict = Verdict(
            label="LOSSY",
            detail="sink re-encodes via AAC (~256 kbps)",
            bottleneck="Bluetooth A2DP encoder",
            hints=["switch to a wired ALSA sink for bit-perfect playback"],
        )
        return FlowReport(mpd=mpd, source=src, sink=sink, verdict=verdict)

    def test_format_audio_spec_full(self):
        assert format_audio_spec(44100, 24, 2) == "24-bit / 44.1 kHz / 2 ch"

    def test_format_audio_spec_partial(self):
        assert format_audio_spec(44100, None, 2) == "44.1 kHz / 2 ch"

    def test_format_audio_spec_empty(self):
        assert format_audio_spec(None, None, None) == "?"

    def test_format_default_includes_sections(self):
        report = self._full_report()
        out = format_default(report, lambda t, _c: t)
        assert "=== xmpd Audio Flow ===" in out
        assert "=== Source ===" in out
        assert "=== Output ===" in out
        assert "=== Verdict ===" in out
        assert "FLAC (lossless)" in out
        assert "24-bit / 44.1 kHz / 2 ch" in out
        assert "AAC" in out
        assert "resample 44100 -> 48000" in out
        assert "LOSSY" in out

    def test_format_short_one_line(self):
        out = format_short(self._full_report())
        assert "tidal/397955552" in out
        assert "FLAC" in out
        assert "AAC" in out
        assert "[lossy]" in out
        assert "\n" not in out

    def test_format_brief_includes_hint(self):
        out = format_brief(self._full_report())
        assert "LOSSY" in out
        assert "Hint" in out

    def test_format_default_handles_missing_mpd(self):
        report = FlowReport(
            mpd=None, source=None, sink=None, verdict=None,
            note="MPD unreachable",
        )
        out = format_default(report, lambda t, _c: t)
        assert "MPD unreachable" in out

    def test_format_default_handles_stopped_state(self):
        mpd = MPDInfo(
            state="stop", file="", artist=None, title=None,
            sample_rate=None, bits=None, channels=None,
            elapsed=None, duration=None, active_outputs=[],
        )
        report = FlowReport(
            mpd=mpd, source=None, sink=None, verdict=None,
            note="no track playing (MPD state: stop)",
        )
        out = format_default(report, lambda t, _c: t)
        assert "no track playing" in out

    def test_format_short_handles_unreachable(self):
        report = FlowReport(
            mpd=None, source=None, sink=None, verdict=None,
            note="MPD unreachable",
        )
        assert format_short(report) == "MPD unreachable"


_FFPROBE_MP3_FIXTURE = {
    "streams": [
        {
            "index": 0,
            "codec_name": "mp3",
            "sample_fmt": "fltp",
            "sample_rate": "44100",
            "channels": 2,
            "bit_rate": "320000",
        }
    ]
}

_FFPROBE_FLAC_FIXTURE = {
    "streams": [
        {
            "index": 0,
            "codec_name": "flac",
            "sample_fmt": "s16",
            "sample_rate": "44100",
            "channels": 2,
            "bits_per_raw_sample": "16",
            "bit_rate": "850000",
        }
    ]
}


class _FakeProc:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.returncode = 0


class TestProbeLocalSource:
    def _mpd(self, file: str) -> MPDInfo:
        return MPDInfo(
            state="play", file=file, artist=None, title=None,
            sample_rate=44100, bits=16, channels=2,
            elapsed=0.0, duration=100.0, active_outputs=[],
        )

    def test_relative_path_resolves_against_music_dir(
        self, monkeypatch, tmp_path
    ):
        track = tmp_path / "a" / "b.mp3"
        track.parent.mkdir(parents=True)
        track.write_bytes(b"")
        captured: dict[str, list[str]] = {}

        def fake_run(cmd, **_kw):
            captured["cmd"] = cmd
            return _FakeProc(json.dumps(_FFPROBE_MP3_FIXTURE))

        monkeypatch.setattr(audio_flow.shutil, "which", lambda _x: "/usr/bin/ffprobe")
        monkeypatch.setattr(audio_flow.subprocess, "run", fake_run)

        info = _probe_local_source(
            self._mpd("a/b.mp3"), {"mpd_music_directory": str(tmp_path)}
        )
        assert info.provider == "local"
        assert info.track_id == "b.mp3"
        assert info.manifest_url == str(track)
        assert captured["cmd"][-1] == str(track)
        assert info.selected is not None
        assert info.selected.codec == "mp3"
        assert info.selected.bitrate == 320000

    def test_absolute_path_used_as_is(self, monkeypatch, tmp_path):
        track = tmp_path / "absolute.flac"
        track.write_bytes(b"")
        monkeypatch.setattr(audio_flow.shutil, "which", lambda _x: "/usr/bin/ffprobe")
        monkeypatch.setattr(
            audio_flow.subprocess, "run",
            lambda *_a, **_kw: _FakeProc(json.dumps(_FFPROBE_FLAC_FIXTURE)),
        )

        info = _probe_local_source(
            self._mpd(str(track)),
            {"mpd_music_directory": "/some/other/dir"},
        )
        assert info.manifest_url == str(track)
        assert info.selected is not None
        assert info.selected.codec == "flac"
        assert info.selected.bits_per_raw_sample == 16

    def test_missing_file_returns_error(self, tmp_path):
        info = _probe_local_source(
            self._mpd("does/not/exist.mp3"),
            {"mpd_music_directory": str(tmp_path)},
        )
        assert info.provider == "local"
        assert info.selected is None
        assert info.error is not None
        assert "not found" in info.error

    def test_ffprobe_missing_returns_error(self, monkeypatch, tmp_path):
        track = tmp_path / "x.mp3"
        track.write_bytes(b"")
        monkeypatch.setattr(audio_flow.shutil, "which", lambda _x: None)

        info = _probe_local_source(
            self._mpd("x.mp3"), {"mpd_music_directory": str(tmp_path)}
        )
        assert info.error == "ffprobe not on PATH"

    def test_ffprobe_returns_no_streams(self, monkeypatch, tmp_path):
        track = tmp_path / "x.mp3"
        track.write_bytes(b"")
        monkeypatch.setattr(audio_flow.shutil, "which", lambda _x: "/usr/bin/ffprobe")
        monkeypatch.setattr(
            audio_flow.subprocess, "run",
            lambda *_a, **_kw: _FakeProc("{}"),
        )

        info = _probe_local_source(
            self._mpd("x.mp3"), {"mpd_music_directory": str(tmp_path)}
        )
        assert info.selected is None
        assert info.error == "ffprobe returned no audio streams"


class TestProbeSourceDispatch:
    def _mpd(self, file: str) -> MPDInfo:
        return MPDInfo(
            state="play", file=file, artist=None, title=None,
            sample_rate=44100, bits=16, channels=2,
            elapsed=0.0, duration=100.0, active_outputs=[],
        )

    def test_empty_file_returns_none(self):
        assert _probe_source(self._mpd(""), {}) is None

    def test_non_proxy_http_url_returns_none(self):
        assert _probe_source(
            self._mpd("http://radio.example.com/stream"), {}
        ) is None

    def test_proxy_url_routes_to_proxy_probe(self, monkeypatch):
        captured: dict[str, str] = {}

        def fake_proxy(mpd, config, provider, track_id):
            captured["provider"] = provider
            captured["track_id"] = track_id
            return SourceInfo(
                provider=provider, track_id=track_id, manifest_url=None,
                candidates=[], selected=None,
            )

        monkeypatch.setattr(audio_flow, "_probe_proxy_source", fake_proxy)
        _probe_source(
            self._mpd("http://localhost:6602/proxy/tidal/abc123"), {}
        )
        assert captured == {"provider": "tidal", "track_id": "abc123"}

    def test_relative_path_routes_to_local_probe(
        self, monkeypatch, tmp_path
    ):
        called: dict[str, bool] = {}

        def fake_local(mpd, config):
            called["yes"] = True
            return SourceInfo(
                provider="local", track_id="x", manifest_url=None,
                candidates=[], selected=None,
            )

        monkeypatch.setattr(audio_flow, "_probe_local_source", fake_local)
        _probe_source(
            self._mpd("Artist/Album/track.mp3"),
            {"mpd_music_directory": str(tmp_path)},
        )
        assert called.get("yes") is True


class TestLocalFormatters:
    def _local_report(self, codec: str = "mp3", bitrate: int = 320000) -> FlowReport:
        cand = StreamCandidate(
            index=0, codec=codec, sample_fmt="fltp",
            sample_rate=44100, channels=2,
            bits_per_raw_sample=None, bitrate=bitrate, tag_id=None,
        )
        src = SourceInfo(
            provider="local", track_id="track.mp3",
            manifest_url="/home/u/Music/track.mp3",
            candidates=[cand], selected=cand,
        )
        mpd = MPDInfo(
            state="play", file="Artist/Album/track.mp3",
            artist="Artist", title="Track",
            sample_rate=44100, bits=16, channels=2,
            elapsed=10.0, duration=200.0, active_outputs=[],
        )
        sink = SinkInfo(
            backend="pulse", name="alsa_output.hdmi", description="HDMI",
            state="RUNNING", sample_fmt="s32le", sample_rate=44100, channels=2,
            is_bluetooth=False, bt_codec=None, bt_codec_display=None,
            bt_bitrate=None, bt_lossy=None,
        )
        verdict = Verdict(
            label="LOSSY (source)",
            detail=f"source codec {codec} is itself lossy",
            bottleneck="source",
        )
        return FlowReport(mpd=mpd, source=src, sink=sink, verdict=verdict)

    def test_format_default_shows_file_not_provider(self):
        out = format_default(self._local_report(), lambda t, _c: t)
        assert "Provider:" not in out
        assert "File:         Artist/Album/track.mp3" in out
        assert "MP3 (lossy)" in out
        assert "320 kbps" in out

    def test_format_default_skips_proxy_section_for_local(self):
        out = format_default(self._local_report(), lambda t, _c: t)
        assert "=== Proxy ===" not in out

    def test_format_short_omits_local_prefix(self):
        out = format_short(self._local_report())
        assert "local/" not in out
        assert out.startswith("track.mp3")
        assert "[lossy (source)]" in out
