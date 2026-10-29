# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for the topology report of the command line tool."""

from __future__ import annotations

import json

import pytest

from pycul.cli import _learn_topology, _topology_report
from pycul.protocols.max import MaxMessage, MaxProtocol, MessageType

WT = "0e0e0e"
HT = "0a1b2c"
SC = "5c5c5c"


def test_topology_from_overheard_frames(capsys: pytest.CaptureFixture[str]) -> None:
    proto = MaxProtocol("123456", time_sync_interval=None)
    _learn_topology(proto)
    for message in (
        MaxMessage(5, 0x04, MessageType.WALL_THERMOSTAT_CONTROL, WT, HT, 3, bytes.fromhex("2AD2")),
        MaxMessage(6, 0x06, MessageType.SHUTTER_CONTACT_STATE, SC, HT, 0, b"\x12"),
    ):
        proto.handle_message(message)
    output = capsys.readouterr().out
    assert f"LEARN {WT} is in group 3" in output
    assert f"LEARN {SC} is linked to {HT}" in output

    _topology_report(proto, as_json=True)
    report = json.loads(capsys.readouterr().out)
    assert report["groups"] == {"3": [WT]}
    assert report["devices"][SC]["link_partners"] == [HT]
    assert report["linked"] == [sorted([HT, SC, WT])]
