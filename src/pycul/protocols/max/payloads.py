# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Decoding and encoding of MAX! message payloads.

The bit layouts are taken from FHEM's 10_MAX.pm (MAX_Parse, MAX_Set,
MAX_ParseDateTime) and 14_CUL_MAX.pm (CUL_MAX_Parse,
CUL_MAX_GetTimeInformationPayload), (c) 2012 Matthias Gehre,
(c) 2019 Wzut, GPL-2.0-or-later. See NOTICE.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pycul.protocols.max.const import (
    BOOST_DURATIONS,
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
    WEEKDAYS,
    ButtonState,
    DeviceType,
    Mode,
    ShutterState,
)


class PayloadError(ValueError):
    """A payload is too short or contains invalid values."""


# ------------------------------------------------------------------ helpers


def decode_until(b1: int, b2: int, b3: int) -> datetime | None:
    """Decode the 3 byte end date of a temporary mode (FHEM: MAX_ParseDateTime)."""
    day = b1 & 0x1F
    month = ((b1 & 0xE0) >> 4) | (b2 >> 7)
    year = 2000 + (b2 & 0x3F)
    half_hours = b3 & 0x3F
    try:
        return datetime(year, month, day, half_hours // 2, 30 * (half_hours % 2))
    except ValueError:
        return None


def encode_until(until: datetime) -> bytes:
    """Encode an end date for a temporary mode. Minutes are rounded down to 0 or 30."""
    if not 2000 <= until.year <= 2063:
        raise PayloadError(f"Year out of range: {until.year}")
    value = (
        ((until.month & 0x0E) << 20)
        | (until.day << 16)
        | ((until.month & 0x01) << 15)
        | ((until.year - 2000) << 8)
        | (until.hour * 2 + until.minute // 30)
    )
    return value.to_bytes(3, "big")


def encode_temperature(temperature: float) -> int:
    """Encode a set point in 0.5 degree steps (4.5 = off, 30.5 = on)."""
    if not MIN_TEMPERATURE <= temperature <= MAX_TEMPERATURE:
        raise PayloadError(
            f"Temperature {temperature} outside {MIN_TEMPERATURE}..{MAX_TEMPERATURE}"
        )
    return round(temperature * 2)


def _require(payload: bytes, length: int, what: str) -> None:
    if len(payload) < length:
        raise PayloadError(f"{what} payload too short: {payload.hex()}")


# ------------------------------------------------------------- state types


@dataclass(slots=True, frozen=True)
class StatusBits:
    """Flags in the first byte of thermostat state messages."""

    mode: Mode
    dst_active: bool
    gateway_known: bool
    panel_locked: bool
    rf_error: bool
    battery_low: bool

    @classmethod
    def decode(cls, value: int) -> StatusBits:
        """Decode the status byte (bit 0-1 mode, bit 3 DST, ... bit 7 battery)."""
        return cls(
            mode=Mode(value & 0x03),
            dst_active=bool(value & 0x08),
            gateway_known=bool(value & 0x10),
            panel_locked=bool(value & 0x20),
            rf_error=bool(value & 0x40),
            battery_low=bool(value & 0x80),
        )


@dataclass(slots=True, frozen=True)
class ThermostatState:
    """State of a radiator thermostat (ThermostatState, also embedded in Acks)."""

    status: StatusBits
    valve_position: int
    target_temperature: float
    current_temperature: float | None
    until: datetime | None

    @classmethod
    def decode(cls, payload: bytes) -> ThermostatState:
        """Decode a ThermostatState payload (3 bytes from Acks, 5 or 6 bytes otherwise).

        Broadcast state reports can be 5 bytes long: enough for the measured
        temperature, not for the end of a temporary mode (FHEM handles both).
        """
        _require(payload, 3, "ThermostatState")
        status = StatusBits.decode(payload[0])
        current: float | None = None
        until: datetime | None = None
        if status.mode is Mode.TEMPORARY:
            if len(payload) >= 6:
                until = decode_until(payload[3], payload[4], payload[5])
        elif len(payload) >= 5:
            current = (((payload[3] & 0x01) << 8) | payload[4]) / 10
            # The device sometimes reports 0.x degree - FHEM ignores values < 1.
            if current < 1:
                current = None
        return cls(
            status=status,
            valve_position=payload[1],
            target_temperature=(payload[2] & 0x7F) / 2,
            current_temperature=current,
            until=until,
        )


@dataclass(slots=True, frozen=True)
class WallThermostatState:
    """State of a wall thermostat (WallThermostatState, also embedded in Acks)."""

    status: StatusBits
    display_actual_temperature: bool
    target_temperature: float
    current_temperature: float | None
    until: datetime | None

    @classmethod
    def decode(cls, payload: bytes) -> WallThermostatState:
        """Decode a WallThermostatState payload (3..7 bytes)."""
        _require(payload, 3, "WallThermostatState")
        status = StatusBits.decode(payload[0])
        until: datetime | None = None
        if len(payload) >= 6 and (payload[3] or payload[5]):
            until = decode_until(payload[3], payload[4], payload[5])
        current: float | None = None
        if len(payload) >= 7:
            current = (((payload[2] & 0x80) << 1) | payload[6]) / 10
        return cls(
            status=status,
            display_actual_temperature=bool(payload[1]),
            target_temperature=(payload[2] & 0x7F) / 2,
            current_temperature=current,
            until=until,
        )


@dataclass(slots=True, frozen=True)
class WallThermostatControl:
    """Set point and measured temperature a wall thermostat sends to its radiator thermostats."""

    target_temperature: float
    current_temperature: float

    @classmethod
    def decode(cls, payload: bytes) -> WallThermostatControl:
        """Decode the 2 byte payload."""
        _require(payload, 2, "WallThermostatControl")
        return cls(
            target_temperature=(payload[0] & 0x7F) / 2,
            current_temperature=(((payload[0] & 0x80) << 1) | payload[1]) / 10,
        )

    def encode(self) -> bytes:
        """Encode the payload (FHEM: CUL_MAX_Set fakeWT).

        The measured temperature (0..51.1 degree) is a 9 bit value in 0.1
        degree; its highest bit is stored in bit 7 of the set point byte.
        """
        measured = round(max(0.0, self.current_temperature) * 10)
        if measured > 0x1FF:
            raise PayloadError("current_temperature must be within 0..51.1")
        return bytes(
            [
                ((measured & 0x100) >> 1) | (encode_temperature(self.target_temperature) & 0x7F),
                measured & 0xFF,
            ]
        )


@dataclass(slots=True, frozen=True)
class SetTemperature:
    """A SetTemperature command (sent by us, a cube or a wall thermostat)."""

    mode: Mode
    temperature: float
    until: datetime | None = None

    @classmethod
    def decode(cls, payload: bytes) -> SetTemperature:
        """Decode a SetTemperature payload."""
        _require(payload, 1, "SetTemperature")
        until = decode_until(*payload[1:4]) if len(payload) >= 4 else None
        return cls(mode=Mode(payload[0] >> 6), temperature=(payload[0] & 0x3F) / 2, until=until)

    def encode(self) -> bytes:
        """Encode the payload (FHEM: MAX_Set desiredTemperature).

        A temperature of 0 in AUTO mode means "use the week profile", in BOOST
        mode the temperature is ignored.
        """
        if self.mode is Mode.TEMPORARY and self.until is None:
            raise PayloadError("Temporary mode requires an end date")
        temperature = 0 if self.temperature == 0 else encode_temperature(self.temperature)
        payload = bytes([(temperature & 0x3F) | (self.mode << 6)])
        if self.mode is Mode.TEMPORARY and self.until is not None:
            payload += encode_until(self.until)
        return payload


@dataclass(slots=True, frozen=True)
class ShutterContactState:
    """State of a window contact."""

    state: ShutterState
    rf_error: bool
    battery_low: bool

    @classmethod
    def decode(cls, payload: bytes) -> ShutterContactState:
        """Decode the 1 byte payload."""
        _require(payload, 1, "ShutterContactState")
        return cls(
            state=ShutterState.OPEN if payload[0] & 0x03 else ShutterState.CLOSED,
            rf_error=bool(payload[0] & 0x40),
            battery_low=bool(payload[0] & 0x80),
        )

    def encode(self) -> bytes:
        """Encode the payload as sent by a (fake) shutter contact (FHEM: 0x12 open, 0x10 closed)."""
        value = 0x12 if self.state is ShutterState.OPEN else 0x10
        value |= (0x40 if self.rf_error else 0) | (0x80 if self.battery_low else 0)
        return bytes([value])


@dataclass(slots=True, frozen=True)
class PushButtonState:
    """State of an eco switch (PushButtonState).

    FHEM only guesses the meaning of the status bits (10_MAX.pm, "TODO:
    confirm"); the second byte is 1 for the auto/comfort key, 0 for eco.
    """

    button: ButtonState | None
    rf_error: bool
    battery_low: bool

    @classmethod
    def decode(cls, payload: bytes) -> PushButtonState:
        """Decode the 1..2 byte payload."""
        _require(payload, 1, "PushButtonState")
        button: ButtonState | None = None
        if len(payload) >= 2:
            button = ButtonState.AUTO if payload[1] else ButtonState.ECO
        return cls(
            button=button,
            rf_error=bool(payload[0] & 0x40),
            battery_low=bool(payload[0] & 0x80),
        )


def encode_link_partner(partner: str, partner_type: DeviceType) -> bytes:
    """Encode the AddLinkPartner/RemoveLinkPartner payload: address + device type.

    Virtual devices are announced as their real counterparts (FHEM: MAX_Set associate).
    """
    if partner_type is DeviceType.VIRTUAL_SHUTTER_CONTACT:
        partner_type = DeviceType.SHUTTER_CONTACT
    elif partner_type is DeviceType.VIRTUAL_THERMOSTAT:
        partner_type = DeviceType.WALL_THERMOSTAT
    return bytes.fromhex(partner) + bytes([partner_type])


@dataclass(slots=True, frozen=True)
class PairPing:
    """Pairing request of a device."""

    firmware: str
    device_type: DeviceType
    test_result: int
    serial: str

    @classmethod
    def decode(cls, payload: bytes) -> PairPing:
        """Decode the PairPing payload (firmware, type, test result, serial)."""
        _require(payload, 3, "PairPing")
        return cls(
            firmware=f"{payload[0] >> 4}.{payload[0] & 0x0F}",
            device_type=DeviceType(payload[1]),
            test_result=payload[2],
            serial=payload[3:].decode("ascii", errors="replace").strip("\x00 "),
        )


# --------------------------------------------------------------- encoders


def encode_time_information(now: datetime) -> bytes:
    """Encode the current local time (FHEM: CUL_MAX_GetTimeInformationPayload).

    The month is split over the upper bits of the minute and second bytes.
    """
    return bytes(
        [
            now.year - 2000,
            now.day,
            now.hour,
            now.minute | ((now.month & 0x0C) << 4),
            now.second | ((now.month & 0x03) << 6),
        ]
    )


def decode_time_information(payload: bytes) -> datetime | None:
    """Decode a TimeInformation payload; returns None for a time request without payload."""
    if len(payload) < 5:
        return None
    month = ((payload[3] >> 6) << 2) | (payload[4] >> 6)
    try:
        return datetime(
            2000 + payload[0],
            month,
            payload[1],
            payload[2] & 0x1F,
            payload[3] & 0x3F,
            payload[4] & 0x3F,
        )
    except ValueError:
        return None


@dataclass(slots=True)
class TemperatureConfig:
    """Temperature configuration of a thermostat (ConfigTemperatures).

    Defaults are the factory values (FHEM: %readingDef).
    """

    comfort: float = 21.0
    eco: float = 17.0
    maximum: float = MAX_TEMPERATURE
    minimum: float = MIN_TEMPERATURE
    measurement_offset: float = 0.0
    window_open_temperature: float = 12.0
    window_open_duration: int = 15  # minutes, 0..60 in steps of 5

    def encode(self) -> bytes:
        """Encode the 7 byte ConfigTemperatures payload."""
        if not -3.5 <= self.measurement_offset <= 3.5:
            raise PayloadError("measurement_offset must be within -3.5..3.5")
        if not 0 <= self.window_open_duration <= 60:
            raise PayloadError("window_open_duration must be within 0..60 minutes")
        return bytes(
            [
                encode_temperature(self.comfort),
                encode_temperature(self.eco),
                encode_temperature(self.maximum),
                encode_temperature(self.minimum),
                round((self.measurement_offset + 3.5) * 2),
                encode_temperature(self.window_open_temperature),
                self.window_open_duration // 5,
            ]
        )


@dataclass(slots=True)
class ValveConfig:
    """Valve configuration of a radiator thermostat (ConfigValve).

    Defaults are the factory values (FHEM: %readingDef).
    """

    boost_duration: int = 5  # minutes, one of BOOST_DURATIONS
    boost_valve_position: int = 80  # percent, steps of 5
    decalcification_day: str = "Sat"
    decalcification_hour: int = 12
    max_valve_setting: int = 100  # percent
    valve_offset: int = 0  # percent

    def encode(self) -> bytes:
        """Encode the 4 byte ConfigValve payload."""
        durations = {minutes: index for index, minutes in BOOST_DURATIONS.items()}
        if self.boost_duration not in durations:
            raise PayloadError(f"boost_duration must be one of {sorted(durations)}")
        if self.decalcification_day not in WEEKDAYS or not 0 <= self.decalcification_hour <= 23:
            raise PayloadError("Invalid decalcification day/hour")
        for value in (self.boost_valve_position, self.max_valve_setting, self.valve_offset):
            if not 0 <= value <= 100:
                raise PayloadError("Valve percentages must be within 0..100")
        return bytes(
            [
                (durations[self.boost_duration] << 5) | (self.boost_valve_position // 5),
                (WEEKDAYS.index(self.decalcification_day) << 5) | self.decalcification_hour,
                # FHEM truncates (int()); rounding keeps e.g. 50% at 128 instead of 127.
                round(self.max_valve_setting * 255 / 100),
                round(self.valve_offset * 255 / 100),
            ]
        )
