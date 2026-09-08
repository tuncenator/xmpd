"""Tests for mpd-owntone-watchdog, the AirPlay route auto-reheal loop.

The bug it exists for: a Wi-Fi blip makes OwnTone reset its outputs, the FLUSH
to an AP2 receiver times out, and OwnTone deselects the speaker and pauses.
OwnTone 29.3 only honours `reconnect` in device_streaming_cb, so nothing
re-arms the speaker and recovery meant re-running `speaker` by hand.

Every test drives the real bash loop (bounded with --ticks) against the fake
OwnTone/MPD in tests/airplay_bridge_stubs.py.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.airplay_bridge_stubs import BRIDGE_DISABLED, JBL_ID, KITCHEN_ID, Bridge

WATCHDOG = "mpd-owntone-watchdog"


@pytest.fixture
def bridge(tmp_path: Path) -> Bridge:
    return Bridge(tmp_path)


@pytest.fixture
def drifted(bridge: Bridge) -> Bridge:
    """AirPlay intent recorded, MPD playing into the bridge, JBL not selected."""
    bridge.set_intent("airplay", JBL_ID)
    bridge.set_selected()
    return bridge


def _notifies(bridge: Bridge) -> list[str]:
    return [c for c in bridge.calls() if c.startswith("notify-send")]


def _bursts(result: subprocess.CompletedProcess) -> int:
    return result.stdout.count("re-arm burst")


# --- the gates: when the watchdog must keep its hands off ---


def test_healthy_airplay_route_is_left_alone(bridge: Bridge) -> None:
    """Intent matches reality: no writes to OwnTone at all."""
    bridge.set_intent("airplay", JBL_ID)
    bridge.set_selected(JBL_ID)

    result = bridge.run(WATCHDOG, "--once")

    assert result.returncode == 0, result.stderr
    assert bridge.selects() == []
    assert [c for c in bridge.api_calls() if c.startswith("PUT")] == []


def test_laptop_route_is_never_rearmed(bridge: Bridge) -> None:
    """A deliberate laptop route means deselected AirPlay outputs are correct."""
    bridge.set_intent("laptop")
    bridge.set_selected()

    result = bridge.run(WATCHDOG, "--once")

    assert result.returncode == 0, result.stderr
    assert bridge.selects() == []


def test_missing_intent_record_is_never_rearmed(bridge: Bridge) -> None:
    """No route recorded yet (fresh install): the watchdog does nothing."""
    bridge.write_state({})

    result = bridge.run(WATCHDOG, "--once")

    assert result.returncode == 0, result.stderr
    assert bridge.api_calls() == []


def test_unparseable_state_file_is_never_rearmed(bridge: Bridge) -> None:
    """A torn or corrupt state.json is skipped, not read as an airplay intent."""
    bridge.state.write_text('{"route": "airp')

    result = bridge.run(WATCHDOG, "--once")

    assert result.returncode == 0, result.stderr
    assert bridge.api_calls() == []


def test_paused_mpd_is_never_rearmed(drifted: Bridge) -> None:
    """Nothing is playing, so a dropped speaker is not worth waking."""
    drifted.env["FAKE_MPD_STATE"] = "[paused]"

    result = drifted.run(WATCHDOG, "--once")

    assert result.returncode == 0, result.stderr
    assert drifted.selects() == []


def test_disabled_bridge_output_is_never_rearmed(drifted: Bridge) -> None:
    """MPD is not feeding the bridge, so OwnTone is not meant to be playing."""
    drifted.env["FAKE_MPD_OUTPUTS"] = BRIDGE_DISABLED

    result = drifted.run(WATCHDOG, "--once")

    assert result.returncode == 0, result.stderr
    assert drifted.selects() == []


def test_unreachable_owntone_api_is_not_treated_as_drift(drifted: Bridge) -> None:
    """A down API cannot tell us anything; do not re-select blind."""
    drifted.env["FAKE_DOWN"] = "1"

    result = drifted.run(WATCHDOG, "--once")

    assert result.returncode == 0, result.stderr
    assert drifted.selects() == []


# --- the reheal ---


def test_deselected_output_is_reselected(drifted: Bridge) -> None:
    """The actual fix: put the intended output back to selected=true."""
    result = drifted.run(WATCHDOG, "--once")

    assert result.returncode == 0, result.stderr
    assert drifted.selects() == [JBL_ID]


def test_reheal_resumes_playback_after_reselecting(drifted: Bridge) -> None:
    """OwnTone paused when it dropped the speaker; play again, in that order."""
    result = drifted.run(WATCHDOG, "--once")
    assert result.returncode == 0, result.stderr

    calls = drifted.calls()
    select_at = next(i for i, c in enumerate(calls) if c.startswith(f"PUT /outputs/{JBL_ID}"))
    play_at = next(i for i, c in enumerate(calls) if c.startswith("PUT /player/play"))
    assert select_at < play_at


def test_reheal_reemits_metadata_and_pokes_waybar(drifted: Bridge) -> None:
    """The fresh RTSP session needs the track metadata re-sent; waybar too."""
    drifted.run(WATCHDOG, "--once")

    calls = drifted.calls()
    assert any(
        c.startswith("systemctl") and "USR1" in c and "mpd-owntone-metadata" in c for c in calls
    )
    assert any(c.startswith("pkill") and "RTMIN+9" in c and "waybar" in c for c in calls)


def test_reheal_never_writes_a_volume(drifted: Bridge) -> None:
    """Volumes belong to vol-wrap/speaker; a reheal must not touch loudness."""
    drifted.run(WATCHDOG, "--once")

    assert drifted.selects() == [JBL_ID]
    assert [c for c in drifted.calls() if "volume" in c] == []


def test_only_the_drifted_output_of_a_multi_room_set_is_reselected(bridge: Bridge) -> None:
    """Kitchen is still up; re-select the JBL alone rather than resetting both."""
    bridge.set_intent("airplay", JBL_ID, KITCHEN_ID)
    bridge.set_selected(KITCHEN_ID)

    result = bridge.run(WATCHDOG, "--once")

    assert result.returncode == 0, result.stderr
    assert bridge.selects() == [JBL_ID]


def test_reheal_retries_a_receiver_that_rejects_the_first_handshake(drifted: Bridge) -> None:
    """Cold receivers drop the first RTSP handshake; re-select until it sticks."""
    accept_on_second_try = textwrap.dedent(f"""\
        #!/usr/bin/env bash
        # Second in-burst backoff sleep: the receiver finishes waking up.
        [[ "$1" == "2" ]] || exit 0
        printf '%s\\n' "{JBL_ID}" >> "{drifted.selected}"
    """)
    drifted.env["FAKE_ACCEPT"] = ""
    drifted.install_sleep_hook(accept_on_second_try)

    result = drifted.run(WATCHDOG, "--once")

    assert result.returncode == 0, result.stderr
    assert len(drifted.selects()) >= 2
    assert any(c.startswith("PUT /player/play") for c in drifted.calls())


def test_a_receiver_off_the_network_does_not_spam_the_journal(drifted: Bridge) -> None:
    """OwnTone answers 400/HTML for a vanished output id; jq must not shout."""
    drifted.env["FAKE_OUTPUTS"] = f"{KITCHEN_ID}|Kitchen|AirPlay"

    result = drifted.run(WATCHDOG, "--once")

    assert result.returncode == 0
    assert "jq:" not in result.stderr
    assert "parse error" not in result.stderr


def test_a_receiver_that_never_comes_back_is_not_played_into(drifted: Bridge) -> None:
    """Nothing got selected, so do not order OwnTone to play into a void."""
    drifted.env["FAKE_ACCEPT"] = ""

    result = drifted.run(WATCHDOG, "--once")

    assert result.returncode == 0, result.stderr
    assert not any(c.startswith("PUT /player/play") for c in drifted.calls())


# --- the backoff: never fight a speaker that is off on purpose ---


def test_gives_up_after_three_bursts_and_notifies_once(drifted: Bridge) -> None:
    """A powered-off speaker gets 3 bursts, one notification, then silence."""
    drifted.env["FAKE_ACCEPT"] = ""

    result = drifted.run(WATCHDOG, "--ticks", "12")

    assert result.returncode == 0, result.stderr
    assert _bursts(result) == 3
    assert len(_notifies(drifted)) == 1


def test_a_fresh_route_intent_rearms_after_giving_up(drifted: Bridge) -> None:
    """Re-running `speaker` is the re-arm signal: the burst budget resets."""
    drifted.env["FAKE_ACCEPT"] = ""
    ticks = drifted.tmp_path / "tick.count"
    rearm_at_tick_6 = textwrap.dedent(f"""\
        #!/usr/bin/env bash
        [[ "$1" == "0" ]] || exit 0
        n=1
        [[ -f "{ticks}" ]] && n=$(( $(cat "{ticks}") + 1 ))
        printf '%s\\n' "$n" > "{ticks}"
        (( n == 6 )) || exit 0
        printf '%s' \\
          '{{"route":"airplay","route_ids":["{JBL_ID}"],"route_changed_at":1770009999}}' \\
          > "{drifted.state}.new"
        mv "{drifted.state}.new" "{drifted.state}"
    """)
    drifted.install_sleep_hook(rearm_at_tick_6)

    result = drifted.run(WATCHDOG, "--ticks", "14")

    assert result.returncode == 0, result.stderr
    assert _bursts(result) == 6
    assert len(_notifies(drifted)) == 2


def test_a_recovered_route_restores_the_burst_budget(drifted: Bridge) -> None:
    """Speaker back on its own: stop bursting, no notification, budget resets."""
    drifted.env["FAKE_ACCEPT"] = ""
    ticks = drifted.tmp_path / "tick.count"
    recover_at_tick_2 = textwrap.dedent(f"""\
        #!/usr/bin/env bash
        [[ "$1" == "0" ]] || exit 0
        n=1
        [[ -f "{ticks}" ]] && n=$(( $(cat "{ticks}") + 1 ))
        printf '%s\\n' "$n" > "{ticks}"
        (( n == 2 )) || exit 0
        printf '%s\\n' "{JBL_ID}" > "{drifted.selected}"
    """)
    drifted.install_sleep_hook(recover_at_tick_2)

    result = drifted.run(WATCHDOG, "--ticks", "8")

    assert result.returncode == 0, result.stderr
    assert _bursts(result) == 2
    assert _notifies(drifted) == []
    assert "healthy again" in result.stdout


# --- plumbing ---


def test_missing_config_fails_loudly(bridge: Bridge) -> None:
    """Without config.env there is no API or MPD to talk to; exit non-zero."""
    bridge.config.unlink()

    result = bridge.run(WATCHDOG, "--once")

    assert result.returncode != 0
    assert "config" in result.stderr


def test_unknown_argument_is_rejected(bridge: Bridge) -> None:
    result = bridge.run(WATCHDOG, "--bogus")

    assert result.returncode == 2
    assert "usage" in result.stderr
