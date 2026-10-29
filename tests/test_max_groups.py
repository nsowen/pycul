# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for Ack state, learning groups and links, rooms and eco switches."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from pycul import Cul, NoAckError
from pycul.protocols.max import (
    ButtonState,
    DeviceType,
    MaxDevice,
    MaxMessage,
    MaxProtocol,
    MessageType,
    Mode,
    ShutterState,
    WeekProfile,
)
from pycul.protocols.max import protocol as protocol_module
from pycul.protocols.max.payloads import encode_until
from tests.conftest import FakeStick
from tests.test_max_protocol import HT, ME, ack_responder, frame, sent_messages, settle

HT2 = "0d1e2f"
WT = "0e0e0e"
SC = "5c5c5c"
SC2 = "5d5d5d"
PB = "7b7b7b"


def group_frame(
    msg_type: MessageType, src: str, dst: str, payload: str, group: int, counter: int = 1
) -> str:
    message = MaxMessage(counter, 0x04, msg_type, src, dst, group, bytes.fromhex(payload))
    return "Z" + message.to_hex() + "40"


@pytest.fixture
async def proto(cul: Cul):
    proto = MaxProtocol(ME, time_sync_interval=None)
    cul.add_protocol(proto)
    await cul.connect()
    proto.add_device(MaxDevice(HT, DeviceType.HEATING_THERMOSTAT))
    yield proto
    await cul.close()


# ------------------------------------------------------------------- Acks


async def test_ack_of_time_information_updates_state(proto: MaxProtocol, stick: FakeStick) -> None:
    # Ack: ok, status 0x19 (manual, DST, gateway), valve 35 %, target 20.0
    ack_responder(stick, state="192328")
    await proto.send_time_information(HT)
    state = proto.devices[HT].state
    assert state.mode is Mode.MANUAL
    assert state.valve_position == 35
    assert state.target_temperature == 20.0
    assert state.dst_active and state.gateway_known


async def test_short_ack_keeps_end_of_temporary_mode(proto: MaxProtocol, stick: FakeStick) -> None:
    until = datetime(2026, 11, 3, 18, 30)
    stick.emit(frame(MessageType.THERMOSTAT_STATE, HT, ME, "1A002B" + encode_until(until).hex()))
    await settle()
    ack_responder(stick, state="1A002B")  # temporary, no end date in the Ack
    await proto.send_time_information(HT)
    assert proto.devices[HT].state.until == until


async def test_shutter_contact_ack_updates_window(proto: MaxProtocol, stick: FakeStick) -> None:
    proto.add_device(MaxDevice(SC, DeviceType.SHUTTER_CONTACT))
    stick.emit(frame(MessageType.ACK, SC, ME, "0192"))  # open, battery low
    await settle()
    state = proto.devices[SC].state
    assert state.window is ShutterState.OPEN
    assert state.battery_low


# ------------------------------------------------------------------ groups


async def test_commands_other_than_set_point_ignore_the_group(
    proto: MaxProtocol, stick: FakeStick
) -> None:
    proto.devices[HT].group = 4
    ack_responder(stick, state="")
    await proto.factory_reset(HT)
    await proto.add_link_partner(HT, WT, DeviceType.WALL_THERMOSTAT)
    await proto.set_day_profile(HT, 0, WeekProfile.factory_default().days[0])
    assert all(m.flags == 0 and m.group == 0 for m in sent_messages(stick))


async def test_set_point_reaches_the_group(proto: MaxProtocol, stick: FakeStick) -> None:
    proto.add_device(MaxDevice(HT2, DeviceType.HEATING_THERMOSTAT, group=4))
    proto.devices[HT].group = 4
    ack_responder(stick, state="")
    await proto.set_temperature(HT, 22.5)
    [sent] = sent_messages(stick)
    assert (sent.group, sent.flags) == (4, 0x04)
    assert proto.devices[HT2].state.target_temperature == 22.5
    assert proto.devices[HT2].state.mode is Mode.MANUAL


async def test_set_points_for_one_group_are_coalesced(proto: MaxProtocol, stick: FakeStick) -> None:
    proto.add_device(MaxDevice(HT2, DeviceType.HEATING_THERMOSTAT, group=4))
    proto.devices[HT].group = 4
    stick.credits = 0  # keep everything queued
    first = asyncio.create_task(proto.set_temperature(HT, 21.0))
    second = asyncio.create_task(proto.set_temperature(HT2, 23.0))
    await settle()
    assert len(proto.queue) == 1
    stick.credits = 900
    ack_responder(stick, state="")
    proto.queue.poke()
    await asyncio.gather(first, second)
    [sent] = sent_messages(stick)
    assert sent.payload == bytes([46 | 0x40])  # 23.0, manual
    assert proto.devices[HT].state.target_temperature == 23.0


async def test_group_representative_has_best_reception(proto: MaxProtocol) -> None:
    proto.add_device(MaxDevice(HT2, DeviceType.HEATING_THERMOSTAT, group=4))
    proto.add_device(MaxDevice(SC, DeviceType.SHUTTER_CONTACT, group=4))
    proto.devices[HT].group = 4
    proto.devices[HT].rssi_by_cul = {"a": -90.0}
    proto.devices[HT2].rssi_by_cul = {"a": -60.0}
    proto.devices[SC].rssi_by_cul = {"a": -40.0}
    assert proto.group_representative(4).address == HT2
    with pytest.raises(ValueError):
        proto.group_representative(9)


async def test_learns_group_and_links_from_overheard_frames(
    proto: MaxProtocol, stick: FakeStick
) -> None:
    proto.add_device(MaxDevice(WT, DeviceType.WALL_THERMOSTAT))
    proto.add_device(MaxDevice(SC, DeviceType.SHUTTER_CONTACT))
    # Wall thermostat tells the radiator thermostat its set point and temperature.
    stick.emit(group_frame(MessageType.WALL_THERMOSTAT_CONTROL, WT, HT, "2AD2", group=3))
    # Window contact without group (flags 0x06) reports to the radiator thermostat.
    stick.emit(frame(MessageType.SHUTTER_CONTACT_STATE, SC, HT, "12", counter=2))
    # Frames to us or broadcasts reveal no link.
    stick.emit(frame(MessageType.THERMOSTAT_STATE, HT, ME, "19202800D700", counter=3))
    await settle()
    assert proto.devices[WT].group == 3
    assert proto.devices[WT].link_partners == {HT}
    assert proto.devices[SC].link_partners == {HT}
    assert proto.devices[SC].group == 0
    assert proto.devices[HT].link_partners == set()


async def test_group_devices_sets_group_and_links(proto: MaxProtocol, stick: FakeStick) -> None:
    proto.add_device(MaxDevice(WT, DeviceType.WALL_THERMOSTAT, link_partners={HT}))
    proto.add_device(MaxDevice(SC, DeviceType.SHUTTER_CONTACT))
    proto.add_device(MaxDevice(SC2, DeviceType.SHUTTER_CONTACT))
    ack_responder(stick, state="")
    stick.credits = 5000  # 14 frames are more than one duty cycle budget
    task = asyncio.create_task(proto.group_devices([HT, WT, SC, SC2], 5))
    await settle()
    assert {m.dst for m in proto.queue.pending} == {SC, SC2}  # waiting for the contacts
    # Shutter contacts only listen right after they transmitted.
    stick.emit(frame(MessageType.SHUTTER_CONTACT_STATE, SC, ME, "10"))
    stick.emit(frame(MessageType.SHUTTER_CONTACT_STATE, SC2, ME, "10"))
    results = await task
    assert results == dict.fromkeys((HT, WT, SC, SC2))
    sent = sent_messages(stick)
    groups = {m.dst for m in sent if m.type is MessageType.SET_GROUP_ID}
    assert groups == {HT, WT, SC, SC2}
    links = {(m.dst, m.payload[:3].hex()) for m in sent if m.type is MessageType.ADD_LINK_PARTNER}
    assert (WT, HT) not in links  # already known
    assert (SC, SC2) not in links and (SC2, SC) not in links
    assert {(HT, WT), (HT, SC), (HT, SC2), (WT, SC), (SC, WT), (SC2, HT)} <= links
    assert all(d.group == 5 for d in proto.devices.values())
    assert WT in proto.devices[HT].link_partners


# ---------------------------------------------------------------- eco switch


async def test_eco_switch_press_counted_once(proto: MaxProtocol, stick: FakeStick) -> None:
    proto.add_device(MaxDevice(PB, DeviceType.PUSH_BUTTON))
    # One press is sent to each link partner with the same counter.
    stick.emit(frame(MessageType.PUSH_BUTTON_STATE, PB, HT, "0000", counter=7))
    stick.emit(frame(MessageType.PUSH_BUTTON_STATE, PB, HT2, "0000", counter=7))
    await settle()
    assert proto.devices[PB].state.button is ButtonState.ECO
    assert proto.devices[PB].state.button_presses == 1
    stick.emit(frame(MessageType.PUSH_BUTTON_STATE, PB, HT, "0001", counter=8))
    await settle()
    assert proto.devices[PB].state.button is ButtonState.AUTO
    assert proto.devices[PB].state.button_presses == 2


def test_link_partners_are_persisted() -> None:
    device = MaxDevice(WT, DeviceType.WALL_THERMOSTAT, link_partners={HT, HT2})
    assert MaxDevice.from_dict(device.to_dict()).link_partners == {HT, HT2}


async def test_link_clusters_ignore_own_fake_devices(proto: MaxProtocol) -> None:
    proto.add_device(MaxDevice(HT2, DeviceType.HEATING_THERMOSTAT, link_partners={"111111"}))
    proto.devices[HT].link_partners = {"111111", WT}  # WT not heard yet
    assert proto.link_clusters() == [{HT, WT}]


async def test_zone_with_shared_window_contact(
    proto: MaxProtocol, stick: FakeStick, caplog: pytest.LogCaptureFixture
) -> None:
    # A loft with two zones; the door contact SC acts on both.
    proto.add_device(MaxDevice(WT, DeviceType.WALL_THERMOSTAT))
    proto.add_device(MaxDevice(SC, DeviceType.SHUTTER_CONTACT))
    ack_responder(stick, state="")
    stick.credits = 5000
    task = asyncio.create_task(proto.group_devices([HT, WT], 5, shared_window_contacts=[SC]))
    await settle()
    stick.emit(frame(MessageType.SHUTTER_CONTACT_STATE, SC, ME, "10"))
    results = await task
    assert results == dict.fromkeys((HT, WT, SC))
    sent = sent_messages(stick)
    assert {m.dst for m in sent if m.type is MessageType.SET_GROUP_ID} == {HT, WT}
    links = {(m.dst, m.payload[:3].hex()) for m in sent if m.type is MessageType.ADD_LINK_PARTNER}
    assert {(HT, SC), (WT, SC), (SC, HT), (SC, WT)} <= links
    assert proto.devices[SC].group == 0  # keeps its own group
    assert "wall thermostats" not in caplog.text


async def test_two_wall_thermostats_in_one_group_are_logged(
    proto: MaxProtocol, stick: FakeStick, caplog: pytest.LogCaptureFixture
) -> None:
    proto.add_device(MaxDevice(WT, DeviceType.WALL_THERMOSTAT))
    proto.add_device(MaxDevice("0f0f0f", DeviceType.WALL_THERMOSTAT))
    ack_responder(stick, state="")
    stick.credits = 5000
    await proto.group_devices([HT, WT, "0f0f0f"], 6, link=False)
    assert "gets 2 wall thermostats" in caplog.text
    assert not [m for m in sent_messages(stick) if m.type is MessageType.ADD_LINK_PARTNER]


async def test_paired_devices_heard_between_each_other_are_probed(
    proto: MaxProtocol, stick: FakeStick, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protocol_module, "PROBE_DELAY", 0.01)
    # Our thermostats answer, the neighbour's wall thermostat does not.
    neighbour = "0b0b0b"

    def respond(command: str) -> list[str]:
        sent = MaxMessage.from_line("Z" + command[2:])
        if sent.dst == neighbour:
            return []
        return [frame(MessageType.ACK, sent.dst, sent.src, "01192028", sent.counter)]

    stick.on_send = respond
    stick.credits = 5000
    # Wall thermostat -> radiator thermostat, nothing addressed to us.
    stick.emit(frame(MessageType.WALL_THERMOSTAT_CONTROL, WT, HT2, "2AD2"))
    stick.emit(frame(MessageType.WALL_THERMOSTAT_CONTROL, neighbour, "0c0c0c", "2AD2"))
    for _ in range(60):
        await asyncio.sleep(0.05)
        if WT in proto.devices and HT2 in proto.devices:
            break
    assert proto.devices[WT].device_type is DeviceType.WALL_THERMOSTAT
    assert proto.devices[HT2].device_type is DeviceType.HEATING_THERMOSTAT
    # The Ack's state was applied right away.
    assert proto.devices[HT2].state.target_temperature == 20.0
    assert neighbour not in proto.devices
    # Each of our devices is asked once.
    probes = [m.dst for m in sent_messages(stick) if m.type is MessageType.TIME_INFORMATION]
    assert probes.count(WT) == probes.count(HT2) == 1
    assert neighbour in probes


async def test_probe_is_repeated_later_after_no_answer(
    proto: MaxProtocol, stick: FakeStick, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protocol_module, "PROBE_DELAY", 0.01)
    monkeypatch.setattr(protocol_module, "ACK_TIMEOUT", 0.05)
    stick.credits = 5000

    def probes() -> int:
        return sum(m.type is MessageType.TIME_INFORMATION for m in sent_messages(stick))

    async def heard() -> None:
        stick.emit(frame(MessageType.WALL_THERMOSTAT_STATE, WT, "000000", "19042b00d9"))
        for _ in range(40):
            await asyncio.sleep(0.02)

    await heard()  # no answer (e.g. a collision): the first probe with its retries
    first = probes()
    assert first >= 1
    await heard()  # heard again right away: not asked again yet
    assert probes() == first
    count, last = proto._probes[WT]
    proto._probes[WT] = (count, last - protocol_module.PROBE_RETRY_AFTER - 1)
    ack_responder(stick, state="192b00")
    await heard()  # later: asked again, answers, adopted
    assert WT in proto.devices


# ------------------------------------------------- fixes from the first field test


async def test_repeated_pair_pings_get_one_pong_first_in_line(
    proto: MaxProtocol, stick: FakeStick
) -> None:
    stick.credits = 0  # nothing can be sent: everything stays queued
    proto.enqueue(HT, MessageType.TIME_INFORMATION, b"\x00", key="other")
    waiting = proto.enqueue(HT, MessageType.WAKE_UP, b"\x3f", key="waiting")
    ping = "1101a14e455130383934333932"  # thermostat, serial NEQ0894392
    for counter in range(3):  # the device repeats its ping every few seconds
        stick.emit(frame(MessageType.PAIR_PING, HT2, ME, ping, counter=counter))
    await settle()
    pending = proto.queue.pending
    pongs = [m for m in pending if m.msg_type is MessageType.PAIR_PONG]
    assert len(pongs) == 1
    # Only a frame already on its way can stay in front of the answer.
    assert pending.index(pongs[0]) < pending.index(waiting)
    proto.queue.clear()


async def test_links_are_learned_from_acks_between_devices(
    proto: MaxProtocol, stick: FakeStick
) -> None:
    proto.add_device(MaxDevice(WT, DeviceType.WALL_THERMOSTAT))
    # The radiator thermostat acknowledges the wall thermostat's set point.
    stick.emit(frame(MessageType.ACK, HT, WT, "0119002b"))
    await settle()
    assert proto.devices[HT].link_partners == {WT}


async def test_zone_setup_gives_up_on_a_device_that_does_not_answer(
    proto: MaxProtocol, stick: FakeStick, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protocol_module, "ACK_TIMEOUT", 0.05)
    proto.add_device(MaxDevice(WT, DeviceType.WALL_THERMOSTAT))
    proto.add_device(MaxDevice(HT2, DeviceType.HEATING_THERMOSTAT))
    stick.credits = 50000

    def respond(command: str) -> list[str]:
        sent = MaxMessage.from_line("Z" + command[2:])
        if sent.dst == HT:  # out of range
            return []
        return [frame(MessageType.ACK, sent.dst, sent.src, "01", sent.counter)]

    stick.on_send = respond
    results = await proto.group_devices([HT, HT2, WT], 2)
    assert isinstance(results[HT], NoAckError)
    assert results[HT2] is None and results[WT] is None
    to_ht = [m for m in sent_messages(stick) if m.dst == HT]
    # Only the group id was tried (with its retries); its links were dropped.
    assert {m.type for m in to_ht} == {MessageType.SET_GROUP_ID}


async def test_repeated_zone_setup_does_not_duplicate_links(
    proto: MaxProtocol, stick: FakeStick
) -> None:
    proto.add_device(MaxDevice(WT, DeviceType.WALL_THERMOSTAT))
    stick.credits = 0
    first = asyncio.create_task(proto.group_devices([HT, WT], 2))
    await settle()
    second = asyncio.create_task(proto.group_devices([HT, WT], 2))
    await settle()
    links = [m for m in proto.queue.pending if m.msg_type is MessageType.ADD_LINK_PARTNER]
    assert len(links) == 2  # HT -> WT and WT -> HT, once each
    proto.queue.clear()
    await asyncio.gather(first, second, return_exceptions=True)
