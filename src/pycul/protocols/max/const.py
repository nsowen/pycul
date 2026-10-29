# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Constants of the eQ-3 MAX! radio protocol.

Values follow FHEM's 10_MAX.pm / 14_CUL_MAX.pm
((c) 2012 Matthias Gehre, (c) 2019 Wzut, GPL-2.0-or-later), see NOTICE.md.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum

BROADCAST = "000000"
#: Default controller address used by FHEM's CUL_MAX.
DEFAULT_ADDRESS = "123456"
#: Addresses of the emulated wall thermostat / shutter contact (FHEM: fakeWTaddr/fakeSCaddr).
DEFAULT_FAKE_WT_ADDRESS = "111111"
DEFAULT_FAKE_SC_ADDRESS = "222222"

MIN_TEMPERATURE = 4.5  # shown as "off" by the devices
MAX_TEMPERATURE = 30.5  # shown as "on" by the devices

#: Seconds to wait for an Ack before retrying (FHEM: $ackTimeout).
ACK_TIMEOUT = 3.0
#: Number of retries after the first send (FHEM: $maxRetryCnt).
MAX_RETRIES = 2

#: Flags of a message sent to a single device / to a group.
FLAGS_DEVICE = 0x00
FLAGS_GROUP = 0x04


class DeviceType(IntEnum):
    """MAX! device types as reported in PairPing."""

    CUBE = 0
    HEATING_THERMOSTAT = 1
    HEATING_THERMOSTAT_PLUS = 2
    WALL_THERMOSTAT = 3
    SHUTTER_CONTACT = 4
    PUSH_BUTTON = 5
    VIRTUAL_SHUTTER_CONTACT = 6
    VIRTUAL_THERMOSTAT = 7
    PLUG_ADAPTER = 8
    UNKNOWN = 255

    @classmethod
    def _missing_(cls, value: object) -> DeviceType:
        return cls.UNKNOWN

    @property
    def is_thermostat(self) -> bool:
        """Return True for radiator and wall thermostats."""
        return self in (
            DeviceType.HEATING_THERMOSTAT,
            DeviceType.HEATING_THERMOSTAT_PLUS,
            DeviceType.WALL_THERMOSTAT,
        )


class MessageType(IntEnum):
    """MAX! message types (FHEM: %msgId2Cmd)."""

    PAIR_PING = 0x00
    PAIR_PONG = 0x01
    ACK = 0x02
    TIME_INFORMATION = 0x03
    CONFIG_WEEK_PROFILE = 0x10
    CONFIG_TEMPERATURES = 0x11
    CONFIG_VALVE = 0x12
    ADD_LINK_PARTNER = 0x20
    REMOVE_LINK_PARTNER = 0x21
    SET_GROUP_ID = 0x22
    REMOVE_GROUP_ID = 0x23
    SHUTTER_CONTACT_STATE = 0x30
    SET_TEMPERATURE = 0x40
    WALL_THERMOSTAT_CONTROL = 0x42
    SET_COMFORT_TEMPERATURE = 0x43
    SET_ECO_TEMPERATURE = 0x44
    PUSH_BUTTON_STATE = 0x50
    THERMOSTAT_STATE = 0x60
    WALL_THERMOSTAT_STATE = 0x70
    SET_DISPLAY_ACTUAL_TEMPERATURE = 0x82
    RESET = 0xF0
    WAKE_UP = 0xF1
    UNKNOWN = 0xFFFF

    @classmethod
    def _missing_(cls, value: object) -> MessageType:
        return cls.UNKNOWN


class Mode(IntEnum):
    """Thermostat control mode (2 lowest bits of the state byte)."""

    AUTO = 0
    MANUAL = 1
    TEMPORARY = 2
    BOOST = 3


class ShutterState(StrEnum):
    """Window state reported by a shutter contact."""

    CLOSED = "closed"
    OPEN = "open"


class ButtonState(StrEnum):
    """Key last pressed on an eco switch."""

    AUTO = "auto"
    ECO = "eco"


#: Boost duration index -> minutes (FHEM: %boost_durations).
BOOST_DURATIONS: dict[int, int] = {0: 0, 1: 5, 2: 10, 3: 15, 4: 20, 5: 25, 6: 30, 7: 60}

#: Decalcification weekday index as used by the devices (Saturday first).
WEEKDAYS = ("Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri")
