"""Tests for `speaker`'s routing-intent record in state.json.

`speaker` is the single writer of the route intent that the auto-reheal
watchdog (mpd-owntone-watchdog) reads. Harness in tests/airplay_bridge_stubs.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.airplay_bridge_stubs import JBL_ID, KITCHEN_ID, Bridge


@pytest.fixture
def bridge(tmp_path: Path) -> Bridge:
    return Bridge(tmp_path)


def test_denon_records_airplay_route_and_target_id(bridge: Bridge) -> None:
    """`speaker denon` writes the AirPlay intent the watchdog reads."""
    result = bridge.run("speaker", "denon")
    assert result.returncode == 0, result.stderr

    state = bridge.read_state()
    assert state["route"] == "airplay"
    assert state["route_ids"] == [JBL_ID]


def test_route_intent_carries_a_change_timestamp(bridge: Bridge) -> None:
    """route_changed_at is an epoch integer, so the watchdog can spot re-arms."""
    bridge.run("speaker", "denon")
    changed_at = bridge.read_state()["route_changed_at"]
    assert isinstance(changed_at, int)
    assert changed_at > 1_700_000_000


def test_multi_records_every_target_id_alongside_the_baseline(bridge: Bridge) -> None:
    """Intent and multi_baseline coexist: neither write clobbers the other."""
    result = bridge.run("speaker", "multi", "denon", "kitchen")
    assert result.returncode == 0, result.stderr

    state = bridge.read_state()
    assert state["route"] == "airplay"
    assert sorted(state["route_ids"]) == sorted([JBL_ID, KITCHEN_ID])
    assert state["multi_baseline"] == {JBL_ID: 40, KITCHEN_ID: 40}


def test_laptop_records_laptop_route_with_no_target_ids(bridge: Bridge) -> None:
    """`speaker laptop` disarms the watchdog by recording a non-airplay route."""
    result = bridge.run("speaker", "laptop")
    assert result.returncode == 0, result.stderr

    state = bridge.read_state()
    assert state["route"] == "laptop"
    assert state["route_ids"] == []


def test_route_write_preserves_unrelated_state_keys(bridge: Bridge) -> None:
    """A pre-existing multi_baseline survives a route change to laptop."""
    bridge.write_state({"multi_baseline": {JBL_ID: 37}})

    bridge.run("speaker", "laptop")

    state = bridge.read_state()
    assert state["route"] == "laptop"
    assert state["multi_baseline"] == {JBL_ID: 37}


def test_state_file_is_never_written_torn(bridge: Bridge) -> None:
    """state.json is replaced atomically; the watchdog is a concurrent reader."""
    bridge.run("speaker", "denon")
    assert json.loads(bridge.state.read_text())["route"] == "airplay"
    leftovers = list(bridge.cfg_dir.glob("state.json.tmp*"))
    assert leftovers == []


def test_status_reports_the_recorded_route(bridge: Bridge) -> None:
    """`speaker status` surfaces the intent so the watchdog's view is visible."""
    bridge.run("speaker", "denon")
    result = bridge.run("speaker", "status")
    assert result.returncode == 0, result.stderr
    assert "airplay" in result.stdout
    assert JBL_ID in result.stdout
