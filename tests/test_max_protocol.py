# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for the MAX! protocol handler against a fake stick."""

from __future__ import annotations

import asyncio

import pytest

from pycul import Cul, NakError, NoAckError
from pycul.protocols.max import (
    DeviceType,
    MaxDevice,
    MaxMessage,
    MaxProtocol,
    MessageType,
    Mode,
)
from pycul.protocols.max import protocol as protocol_module
from pycul.protocols.max import queue as queue_module
from tests.conftest import FakeStick

ME = "123456"
HT = "0a1b2c"


def frame(msg_type: MessageType, src: str, dst: str, payload: str, counter: int = 1) -> str:
    message = MaxMessage(counter, 0, msg_type, src, dst, 0, bytes.fromhex(payload))
    return "Z" + message.to_hex() + "40"  # with RSSI byte


def sent_messages(stick: FakeStick) -> list[MaxMessage]:
    return [MaxMessage.from_line("Z" + c[2:]) for c in stick.transmitted]


def ack_responder(stick: FakeStick, status: str = "01", state: str = "1900A8") -> None:
    """Make every sent frame get acknowledged by its destination."""

    def respond(command: str) -> list[str]:
        sent = MaxMessage.from_line("Z" + command[2:])
        return [frame(MessageType.ACK, sent.dst, sent.src, status + state, sent.counter)]

    stick.on_send = respond


@pytest.fixture
async def max_proto(cul: Cul):
    proto = MaxProtocol(ME, time_sync_interval=None)
    cul.add_protocol(proto)
    await cul.connect()
    yield proto
    await cul.close()


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0.02)


async def test_init_commands(max_proto: MaxProtocol, stick: FakeStick) -> None:
    assert stick.commands[:5] == ["", "V", "X21", "Zr", f"Za{ME}"]


async def test_adopts_device_paired_to_us(max_proto: MaxProtocol, stick: FakeStick) -> None:
    added: list[MaxDevice] = []
    max_proto.add_device_added_listener(added.append)
    stick.emit(frame(MessageType.THERMOSTAT_STATE, HT, ME, "19202800D700"))
    await settle()
    assert [d.address for d in added] == [HT]
    device = max_proto.devices[HT]
    assert device.device_type is DeviceType.HEATING_THERMOSTAT
    assert device.state.mode is Mode.MANUAL
    assert device.state.valve_position == 0x20
    assert device.state.target_temperature == 20.0
    assert device.state.current_temperature == pytest.approx(21.5)
    assert device.state.rssi == 0x40 / 2 - 74


async def test_ignores_foreign_devices(max_proto: MaxProtocol, stick: FakeStick) -> None:
    stick.emit(frame(MessageType.THERMOSTAT_STATE, HT, "abcdef", "19202800D700"))
    await settle()
    assert HT not in max_proto.devices


async def test_set_temperature_with_ack(max_proto: MaxProtocol, stick: FakeStick) -> None:
    max_proto.add_device(MaxDevice(HT, DeviceType.HEATING_THERMOSTAT))
    ack_responder(stick, state="1900AA")  # manual, valve 0, 21.0 (high bit set)
    await max_proto.set_temperature(HT, 21.0)
    [sent] = sent_messages(stick)
    assert sent.type is MessageType.SET_TEMPERATURE
    assert sent.dst == HT
    assert sent.src == ME
    assert sent.payload == bytes([42 | 0x40])
    assert max_proto.devices[HT].state.target_temperature == 21.0
    assert max_proto.devices[HT].state.mode is Mode.MANUAL


async def test_group_flags(max_proto: MaxProtocol, stick: FakeStick) -> None:
    max_proto.add_device(MaxDevice(HT, DeviceType.HEATING_THERMOSTAT, group=3))
    ack_responder(stick)
    await max_proto.set_boost(HT)
    [sent] = sent_messages(stick)
    assert sent.group == 3
    assert sent.flags == 0x04


async def test_nak_raises(max_proto: MaxProtocol, stick: FakeStick) -> None:
    ack_responder(stick, status="81", state="")
    with pytest.raises(NakError):
        await max_proto.set_auto(HT)


async def test_no_ack_retries(
    max_proto: MaxProtocol, stick: FakeStick, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protocol_module, "ACK_TIMEOUT", 0.05)
    with pytest.raises(NoAckError):
        await max_proto.set_auto(HT)
    assert len(sent_messages(stick)) == queue_module.MAX_RETRIES + 1


async def test_pairing(max_proto: MaxProtocol, stick: FakeStick) -> None:
    ack_responder(stick, state="")
    ping = "100100" + b"KEQ0123456".hex()
    stick.emit(frame(MessageType.PAIR_PING, HT, "000000", ping))
    await settle()
    assert HT not in max_proto.devices  # pairing not enabled

    max_proto.start_pairing(5)
    stick.emit(frame(MessageType.PAIR_PING, HT, "000000", ping))
    await settle()
    assert max_proto.devices[HT].serial == "KEQ0123456"
    assert [m.type for m in sent_messages(stick)] == [
        MessageType.PAIR_PONG,
        MessageType.TIME_INFORMATION,
    ]


async def test_pair_ping_for_other_controller_is_ignored(
    max_proto: MaxProtocol, stick: FakeStick
) -> None:
    max_proto.start_pairing(5)
    stick.emit(frame(MessageType.PAIR_PING, HT, "abcdef", "100100" + b"KEQ0123456".hex()))
    await settle()
    assert HT not in max_proto.devices


async def test_time_request_is_answered(max_proto: MaxProtocol, stick: FakeStick) -> None:
    max_proto.add_device(MaxDevice(HT, DeviceType.HEATING_THERMOSTAT))
    ack_responder(stick, state="")
    stick.emit(frame(MessageType.TIME_INFORMATION, HT, ME, ""))
    await settle()
    [sent] = sent_messages(stick)
    assert sent.type is MessageType.TIME_INFORMATION
    assert len(sent.payload) == 5


async def test_own_auto_ack_is_ignored(max_proto: MaxProtocol, stick: FakeStick) -> None:
    stick.emit(frame(MessageType.ACK, ME, HT, "00"))
    await settle()
    assert ME not in max_proto.devices


async def test_init_sends_fake_wt_address(max_proto: MaxProtocol, stick: FakeStick) -> None:
    assert "Zw111111" in stick.commands


async def test_old_firmware_gets_no_fake_wt(cul: Cul, stick: FakeStick) -> None:
    stick.version = "V 1.50 CUL868"
    cul.add_protocol(MaxProtocol(ME, time_sync_interval=None))
    await cul.connect()
    assert not any(c.startswith("Zw") for c in stick.commands)
    await cul.close()


def test_fake_addresses_must_be_distinct() -> None:
    with pytest.raises(ValueError):
        MaxProtocol(ME, fake_wt_address=ME)
    with pytest.raises(ValueError):
        MaxProtocol(ME, fake_sc_address="000000")


async def test_associate_fake_wall_thermostat(max_proto: MaxProtocol, stick: FakeStick) -> None:
    ack_responder(stick)
    await max_proto.associate_fake_wall_thermostat(HT)
    await max_proto.associate_fake_shutter_contact(HT, associate=False)
    first, second = sent_messages(stick)
    assert first.type is MessageType.ADD_LINK_PARTNER
    assert first.payload.hex() == "11111103"
    assert second.type is MessageType.REMOVE_LINK_PARTNER
    assert second.payload.hex() == "22222204"


async def test_fake_wall_thermostat_is_acked_to_fake_address(
    max_proto: MaxProtocol, stick: FakeStick
) -> None:
    # The thermostat answers the fake WT (dst 111111), not the controller.
    ack_responder(stick)
    await max_proto.send_fake_wall_thermostat(HT, 21.0, 19.5)
    [sent] = sent_messages(stick)
    assert sent.type is MessageType.WALL_THERMOSTAT_CONTROL
    assert sent.src == "111111"
    assert sent.flags == 0x00
    assert sent.payload == bytes([42, 195])


async def test_fake_shutter_contact(max_proto: MaxProtocol, stick: FakeStick) -> None:
    ack_responder(stick)
    await max_proto.send_fake_shutter_contact(HT, window_open=True)
    [sent] = sent_messages(stick)
    assert sent.src == "222222"
    assert sent.flags == 0x06
    assert sent.payload == b"\x12"


async def test_culfw_auto_ack_of_fake_is_ignored(max_proto: MaxProtocol, stick: FakeStick) -> None:
    stick.emit(frame(MessageType.ACK, "111111", HT, "00"))
    await settle()
    assert "111111" not in max_proto.devices


async def test_local_set_temperature_updates_device(
    max_proto: MaxProtocol, stick: FakeStick
) -> None:
    max_proto.add_device(MaxDevice(HT, DeviceType.HEATING_THERMOSTAT))
    # Knob turned to 23.0 manual, thermostat tells its partner (fake WT).
    stick.emit(frame(MessageType.SET_TEMPERATURE, HT, "111111", "6e"))
    await settle()
    assert max_proto.devices[HT].state.target_temperature == 23.0
    assert max_proto.devices[HT].state.mode is Mode.MANUAL
