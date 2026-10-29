# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for MAX! frame and payload coding."""

from __future__ import annotations

from datetime import datetime

import pytest

from pycul.protocols.max import DeviceType, MaxMessage, MessageType, Mode, ShutterState
from pycul.protocols.max.message import InvalidFrameError
from pycul.protocols.max.payloads import (
    PairPing,
    PayloadError,
    SetTemperature,
    ShutterContactState,
    TemperatureConfig,
    ThermostatState,
    ValveConfig,
    WallThermostatControl,
    WallThermostatState,
    decode_time_information,
    decode_until,
    encode_time_information,
    encode_until,
)


def test_parse_frame_with_rssi() -> None:
    msg = MaxMessage.from_line("Z0F0A0460ABCDEF12345600190020011A44")
    assert msg.counter == 0x0A
    assert msg.flags == 0x04
    assert msg.type is MessageType.THERMOSTAT_STATE
    assert msg.src == "abcdef"
    assert msg.dst == "123456"
    assert msg.group == 0
    assert msg.payload == bytes.fromhex("190020011A")
    assert msg.rssi == 0x44 / 2 - 74


def test_parse_frame_without_rssi_and_roundtrip() -> None:
    line = "Z0B01000212345600ABCD0001"
    msg = MaxMessage.from_line(line)
    assert msg.rssi is None
    assert msg.type is MessageType.ACK
    assert "Z" + msg.to_hex() == line.upper()


@pytest.mark.parametrize(
    "line", ["Z", "Zxx", "Z05010002", "Z0F0004601234560000000019001F", "A0B010002"]
)
def test_invalid_frames(line: str) -> None:
    with pytest.raises(InvalidFrameError):
        MaxMessage.from_line(line)


def test_unknown_message_type() -> None:
    msg = MaxMessage.from_line("Z0A0100991234560000000000")
    assert msg.type is MessageType.UNKNOWN
    assert msg.type_id == 0x99


def test_thermostat_state_manual_with_temperature() -> None:
    # manual, battery ok, valve 32%, target 20.0, measured 28.2 (9th bit set)
    state = ThermostatState.decode(bytes([0x19, 32, 40, 0x01, 0x1A, 0x00]))
    assert state.status.mode is Mode.MANUAL
    assert state.status.dst_active
    assert state.status.gateway_known
    assert not state.status.battery_low
    assert state.valve_position == 32
    assert state.target_temperature == 20.0
    assert state.current_temperature == pytest.approx(28.2)
    assert state.until is None


def test_thermostat_state_temporary_and_flags() -> None:
    until = encode_until(datetime(2026, 11, 3, 18, 30))
    state = ThermostatState.decode(bytes([0xC2, 0, 43, *until]))
    assert state.status.mode is Mode.TEMPORARY
    assert state.status.battery_low
    assert state.status.rf_error
    assert state.current_temperature is None
    assert state.until == datetime(2026, 11, 3, 18, 30)


def test_thermostat_state_from_ack_is_short() -> None:
    state = ThermostatState.decode(bytes([0x18, 0, 42]))
    assert state.status.mode is Mode.AUTO
    assert state.target_temperature == 21.0
    assert state.current_temperature is None


def test_thermostat_state_ignores_bogus_low_temperature() -> None:
    assert ThermostatState.decode(bytes([0x19, 0, 40, 0, 3, 0])).current_temperature is None


def test_until_matches_fhem_formula() -> None:
    # FHEM 10_MAX.pm: sprintf("%06x", (($month&0xE) << 20) | ($day << 16) | (($month&1) << 15)
    #                 | (($year-2000) << 8) | ($hour*2 + int($min/30)))
    for when in (datetime(2026, 10, 28, 18, 30), datetime(2027, 1, 31, 0, 0)):
        expected = (
            ((when.month & 0xE) << 20)
            | (when.day << 16)
            | ((when.month & 1) << 15)
            | ((when.year - 2000) << 8)
            | (when.hour * 2 + when.minute // 30)
        )
        encoded = encode_until(when)
        assert encoded == expected.to_bytes(3, "big")
        assert decode_until(*encoded) == when


def test_set_temperature_encoding() -> None:
    assert SetTemperature(Mode.MANUAL, 21.5).encode() == bytes([43 | 0x40])
    assert SetTemperature(Mode.AUTO, 0).encode() == b"\x00"
    assert SetTemperature(Mode.BOOST, 0).encode() == b"\xc0"
    assert SetTemperature(Mode.MANUAL, 4.5).encode() == bytes([9 | 0x40])
    temporary = SetTemperature(Mode.TEMPORARY, 19, datetime(2026, 10, 28, 22, 0)).encode()
    assert temporary[0] == 38 | 0x80
    assert SetTemperature.decode(temporary).until == datetime(2026, 10, 28, 22, 0)
    with pytest.raises(PayloadError):
        SetTemperature(Mode.MANUAL, 31).encode()
    with pytest.raises(PayloadError):
        SetTemperature(Mode.TEMPORARY, 20).encode()


def test_wall_thermostat_state_and_control() -> None:
    state = WallThermostatState.decode(bytes([0x19, 0x00, 0x2A | 0x80, 0, 0, 0, 0x04]))
    assert state.target_temperature == 21.0
    assert state.current_temperature == pytest.approx(26.0)
    assert state.until is None
    control = WallThermostatControl.decode(bytes([0x28, 0xD2]))
    assert control.target_temperature == 20.0
    assert control.current_temperature == pytest.approx(21.0)


def test_shutter_contact_state() -> None:
    assert ShutterContactState.decode(b"\x12").state is ShutterState.OPEN
    closed = ShutterContactState.decode(b"\x90")
    assert closed.state is ShutterState.CLOSED
    assert closed.battery_low


def test_pair_ping() -> None:
    ping = PairPing.decode(bytes([0x10, 1, 0]) + b"KEQ0123456")
    assert ping.firmware == "1.0"
    assert ping.device_type is DeviceType.HEATING_THERMOSTAT
    assert ping.serial == "KEQ0123456"


def test_time_information_roundtrip() -> None:
    now = datetime(2026, 10, 28, 21, 45, 17)
    assert decode_time_information(encode_time_information(now)) == now
    assert decode_time_information(b"") is None


def test_config_payloads_match_fhem_defaults() -> None:
    # Factory defaults as in FHEM: comfort 21, eco 17, max on, min off, offset 0, window 12/15min
    assert TemperatureConfig().encode().hex() == "2a223d09071803"
    # boost 5 min/80 %, decalc Sat 12:00, max valve 100 %, offset 0 %
    assert ValveConfig().encode().hex() == "300cff00"
    with pytest.raises(PayloadError):
        ValveConfig(boost_duration=7).encode()


def test_wall_thermostat_control_encoding_matches_fhem() -> None:
    # FHEM CUL_MAX_Set fakeWT: arg2 = int(10*measured);
    # arg1 = ((arg2 & 0x100) >> 1) | (int(2*desired) & 0x7F); arg2 &= 0xFF
    for desired, measured in ((21.0, 19.7), (4.5, 26.3), (30.5, 51.1), (20.0, 0.0)):
        m10 = round(10 * measured)
        expected = bytes([((m10 & 0x100) >> 1) | (int(2 * desired) & 0x7F), m10 & 0xFF])
        encoded = WallThermostatControl(desired, measured).encode()
        assert encoded == expected
        decoded = WallThermostatControl.decode(encoded)
        assert decoded.target_temperature == desired
        assert decoded.current_temperature == pytest.approx(measured)
    with pytest.raises(PayloadError):
        WallThermostatControl(21, 52).encode()


def test_shutter_contact_encoding() -> None:
    assert ShutterContactState(ShutterState.OPEN, False, False).encode() == b"\x12"
    assert ShutterContactState(ShutterState.CLOSED, False, False).encode() == b"\x10"


def test_link_partner_payload() -> None:
    from pycul.protocols.max.payloads import encode_link_partner

    assert encode_link_partner("111111", DeviceType.WALL_THERMOSTAT).hex() == "11111103"
    assert encode_link_partner("222222", DeviceType.VIRTUAL_SHUTTER_CONTACT).hex() == "22222204"


def test_thermostat_state_broadcast_with_five_bytes() -> None:
    # Seen on air: 15f682 -> 000000, flags 04, payload 193f2c00d9
    state = ThermostatState.decode(bytes.fromhex("193f2c00d9"))
    assert state.status.mode is Mode.MANUAL
    assert state.valve_position == 63
    assert state.target_temperature == 22.0
    assert state.current_temperature == pytest.approx(21.7)
