"""Tests for xmpd.audio_flow probes, parsers, verdict logic, and formatters."""

from xmpd.audio_flow import (
    FlowReport,
    MPDInfo,
    SinkInfo,
    SourceInfo,
    StreamCandidate,
    Verdict,
    _compute_verdict,
    _parse_mpd_audio_field,
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
