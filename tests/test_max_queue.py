# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for the MAX! send queue, groups, week profile sending and multiple sticks."""

from __future__ import annotations

import asyncio

import pytest

from pycul import Cul, NakError, SendCancelledError, SendExpiredError
from pycul.protocols.max import (
    DayProfile,
    DeviceType,
    MaxDevice,
    MaxMessage,
    MaxProtocol,
    MessageType,
    WeekProfile,
    WeekProfileError,
)
from pycul.protocols.max import queue as queue_module
from tests.conftest import FakeStick
from tests.test_max_protocol import HT, ME, ack_responder, frame, sent_messages, settle

HT2 = "0d1e2f"
SC = "5c5c5c"


@pytest.fixture
async def proto(cul: Cul):
    proto = MaxProtocol(ME, time_sync_interval=None)
    cul.add_protocol(proto)
    await cul.connect()
    proto.add_device(MaxDevice(HT, DeviceType.HEATING_THERMOSTAT))
    yield proto
    await cul.close()


# ------------------------------------------------------------------- queue


async def test_waits_for_credit(
    proto: MaxProtocol, stick: FakeStick, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(queue_module, "MAX_CREDIT_WAIT", 0.05)
    ack_responder(stick)
    stick.credits = 20
    task = asyncio.create_task(proto.set_auto(HT))
    await settle()
    assert not sent_messages(stick)
    assert proto.queue.waiting_for_credit is not None
    stick.credits = 900  # duty cycle budget recovered
    await asyncio.wait_for(task, 1)
    assert len(sent_messages(stick)) == 1
    assert proto.queue.waiting_for_credit is None


async def test_unsent_set_points_are_coalesced(
    proto: MaxProtocol, stick: FakeStick, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(queue_module, "MAX_CREDIT_WAIT", 0.05)
    ack_responder(stick)
    stick.credits = 0
    tasks = [asyncio.create_task(proto.set_temperature(HT, t)) for t in (19, 20, 21.5)]
    await settle()
    assert len(proto.queue) == 1
    stick.credits = 900
    await asyncio.wait_for(asyncio.gather(*tasks), 1)
    [sent] = sent_messages(stick)
    assert sent.payload == bytes([43 | 0x40])  # only 21.5 went on air


async def test_shutter_contact_frames_wait_until_it_transmits(
    proto: MaxProtocol, stick: FakeStick
) -> None:
    proto.add_device(MaxDevice(SC, DeviceType.SHUTTER_CONTACT))
    ack_responder(stick, state="")
    task = asyncio.create_task(proto.set_group(SC, 3))
    await settle()
    assert not sent_messages(stick)
    stick.emit(frame(MessageType.SHUTTER_CONTACT_STATE, SC, ME, "12"))
    await asyncio.wait_for(task, 1)
    [sent] = sent_messages(stick)
    assert sent.type is MessageType.SET_GROUP_ID
    assert proto.devices[SC].group == 3


async def test_expiry_and_clear(proto: MaxProtocol, stick: FakeStick) -> None:
    proto.add_device(MaxDevice(SC, DeviceType.SHUTTER_CONTACT))
    held = proto.enqueue(SC, MessageType.SET_GROUP_ID, b"\x01", expires=0.05)
    with pytest.raises(SendExpiredError):
        await asyncio.wait_for(held.future, 1)
    held = proto.enqueue(SC, MessageType.SET_GROUP_ID, b"\x01")
    assert proto.queue.clear(SC) == 1
    with pytest.raises(SendCancelledError):
        await held.future


async def test_nak_is_not_retried(proto: MaxProtocol, stick: FakeStick) -> None:
    ack_responder(stick, status="81", state="")
    with pytest.raises(NakError):
        await proto.set_auto(HT)
    assert len(sent_messages(stick)) == 1


async def test_queue_waits_for_connection(stick: FakeStick) -> None:
    cul = Cul("fake", stream_factory=stick.factory, reconnect=False)  # type: ignore[arg-type]
    proto = MaxProtocol(ME, time_sync_interval=None)
    cul.add_protocol(proto)
    ack_responder(stick)
    task = asyncio.create_task(proto.set_auto(HT))
    await settle()
    assert len(proto.queue) == 1
    await cul.connect()
    await asyncio.wait_for(task, 1)
    assert len(sent_messages(stick)) == 1
    await cul.close()


async def test_fast_send_after_wake_up(cul: Cul, stick: FakeStick) -> None:
    proto = MaxProtocol(ME, time_sync_interval=None, fast_send_after_wake_up=True)
    cul.add_protocol(proto)
    await cul.connect()
    proto.add_device(MaxDevice(HT, DeviceType.HEATING_THERMOSTAT))
    ack_responder(stick, state="")
    profile = WeekProfile.from_days({d: DayProfile.constant(19) for d in range(7)})
    await proto.set_week_profile(HT, profile)
    commands = stick.transmitted
    assert MaxMessage.from_line("Z" + commands[0][2:]).type is MessageType.WAKE_UP
    assert commands[0].startswith("Zs")
    assert all(c.startswith("Zf") for c in commands[1:])
    await cul.close()


# ------------------------------------------------------------------ groups


async def test_group_commands(proto: MaxProtocol, stick: FakeStick) -> None:
    proto.add_device(MaxDevice(HT2, DeviceType.HEATING_THERMOSTAT))
    ack_responder(stick, state="")
    await proto.set_group(HT, 2)
    await proto.set_group(HT2, 2)
    assert {d.address for d in proto.devices_in_group(2)} == {HT, HT2}
    group_frames = [m for m in sent_messages(stick) if m.type is MessageType.SET_GROUP_ID]
    assert all(m.group == 0 and m.flags == 0 for m in group_frames)
    await proto.set_group_temperature(2, 20.5)
    # One frame with the group id reaches both thermostats.
    [temp] = [m for m in sent_messages(stick) if m.type is MessageType.SET_TEMPERATURE]
    assert temp.group == 2 and temp.flags == 0x04
    assert proto.devices[HT].state.target_temperature == 20.5
    assert proto.devices[HT2].state.target_temperature == 20.5
    await proto.set_group(HT, 0)
    assert sent_messages(stick)[-1].type is MessageType.REMOVE_GROUP_ID
    assert proto.devices[HT].group == 0


# ------------------------------------------------------------ week profile


async def test_week_profile_sends_only_changed_days(proto: MaxProtocol, stick: FakeStick) -> None:
    ack_responder(stick, state="")
    proto.devices[HT].week_profile = WeekProfile.factory_default()
    new = WeekProfile.factory_default().replace(
        2, DayProfile.from_periods([("06:00", "22:00", 20)], 16)
    )
    assert await proto.set_week_profile(HT, new) == [2]
    [sent] = sent_messages(stick)
    assert sent.type is MessageType.CONFIG_WEEK_PROFILE
    assert sent.payload[0] == max_day(2)
    assert proto.devices[HT].week_profile == new


async def test_week_profile_partial_failure(
    proto: MaxProtocol, stick: FakeStick, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pycul.protocols.max.protocol.ACK_TIMEOUT", 0.02)
    base = WeekProfile.factory_default()
    proto.devices[HT].week_profile = base

    def respond(command: str) -> list[str]:
        sent = MaxMessage.from_line("Z" + command[2:])
        if sent.payload[0] == max_day(1):  # Tuesday never gets an Ack
            return []
        return [frame(MessageType.ACK, sent.dst, sent.src, "01", sent.counter)]

    stick.on_send = respond
    new = base.replace(0, DayProfile.constant(18)).replace(1, DayProfile.constant(18))
    with pytest.raises(WeekProfileError) as err:
        await proto.set_week_profile(HT, new)
    assert list(err.value.failed) == [1]
    assert proto.devices[HT].week_profile == base.replace(0, DayProfile.constant(18))


def max_day(weekday: int) -> int:
    return (weekday + 2) % 7


# --------------------------------------------------------- multiple sticks


@pytest.fixture
async def two_sticks():
    first, second = FakeStick(), FakeStick()
    cul_a = Cul("a", name="a", stream_factory=first.factory, reconnect=False)  # type: ignore[arg-type]
    cul_b = Cul("b", name="b", stream_factory=second.factory, reconnect=False)  # type: ignore[arg-type]
    proto = MaxProtocol(ME, time_sync_interval=None)
    cul_a.add_protocol(proto)
    cul_b.add_protocol(proto)
    await cul_a.connect()
    await cul_b.connect()
    yield proto, cul_a, cul_b, first, second
    await cul_a.close()
    await cul_b.close()


async def test_frames_heard_by_two_sticks_are_processed_once(two_sticks) -> None:
    proto, _a, _b, first, second = two_sticks
    seen: list[MaxMessage] = []
    proto.add_message_listener(seen.append)
    line = frame(MessageType.THERMOSTAT_STATE, HT, ME, "19202800D700")
    first.emit(line)
    second.emit(line[:-2] + "10")  # same frame, weaker RSSI at stick b
    await settle()
    assert len(seen) == 1
    assert set(proto.devices[HT].rssi_by_cul) == {"a", "b"}


async def test_sends_via_stick_with_best_rssi_and_fails_over(two_sticks) -> None:
    proto, cul_a, _b, first, second = two_sticks
    proto.add_device(MaxDevice(HT, DeviceType.HEATING_THERMOSTAT))
    proto.devices[HT].rssi_by_cul.update({"a": -90.0, "b": -60.0})
    ack_responder(first)
    ack_responder(second)
    await proto.set_auto(HT)
    assert not sent_messages(first)
    assert len(sent_messages(second)) == 1

    proto.devices[HT].preferred_cul = "a"
    await proto.set_boost(HT)
    assert len(sent_messages(first)) == 1

    await cul_a.close()
    await proto.set_auto(HT)
    assert len(sent_messages(second)) == 2


async def test_unexpected_error_fails_the_frame(proto: MaxProtocol, stick: FakeStick) -> None:
    ack_responder(stick)

    def broken() -> bytes:
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        await proto.send(HT, MessageType.TIME_INFORMATION, refresh=broken)
    # The worker keeps running.
    await proto.set_auto(HT)
