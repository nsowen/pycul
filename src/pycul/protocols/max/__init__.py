# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""eQ-3 MAX! support (culfw "Moritz" mode)."""

from pycul.protocols.max.const import (
    BROADCAST,
    DEFAULT_ADDRESS,
    DEFAULT_FAKE_SC_ADDRESS,
    DEFAULT_FAKE_WT_ADDRESS,
    ButtonState,
    DeviceType,
    MessageType,
    Mode,
    ShutterState,
)
from pycul.protocols.max.device import MaxDevice, MaxDeviceState
from pycul.protocols.max.message import InvalidFrameError, MaxMessage
from pycul.protocols.max.payloads import PayloadError, TemperatureConfig, ValveConfig
from pycul.protocols.max.protocol import MaxProtocol, WeekProfileError, normalize_address
from pycul.protocols.max.queue import OutgoingMessage, SendQueue
from pycul.protocols.max.weekprofile import DayProfile, SwitchPoint, WeekProfile

__all__ = [
    "BROADCAST",
    "DEFAULT_ADDRESS",
    "DEFAULT_FAKE_SC_ADDRESS",
    "DEFAULT_FAKE_WT_ADDRESS",
    "ButtonState",
    "DayProfile",
    "DeviceType",
    "InvalidFrameError",
    "MaxDevice",
    "MaxDeviceState",
    "MaxMessage",
    "MaxProtocol",
    "MessageType",
    "Mode",
    "OutgoingMessage",
    "PayloadError",
    "SendQueue",
    "ShutterState",
    "SwitchPoint",
    "TemperatureConfig",
    "ValveConfig",
    "WeekProfile",
    "WeekProfileError",
    "normalize_address",
]
