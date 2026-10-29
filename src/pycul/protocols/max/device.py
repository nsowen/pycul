# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""In-memory model of a MAX! device and its last known state."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pycul.protocols.max.const import ButtonState, DeviceType, Mode, ShutterState
from pycul.protocols.max.weekprofile import WeekProfile

_LOGGER = logging.getLogger(__name__)

type DeviceListener = Callable[[MaxDevice], None]


@dataclass(slots=True)
class MaxDeviceState:
    """Last known values reported by (or set on) a device. None means unknown."""

    mode: Mode | None = None
    target_temperature: float | None = None
    current_temperature: float | None = None
    valve_position: int | None = None
    until: datetime | None = None
    battery_low: bool | None = None
    rf_error: bool | None = None
    panel_locked: bool | None = None
    #: The device switches to daylight saving time on its own.
    dst_active: bool | None = None
    #: The device is paired with a controller ("langateway" in FHEM).
    gateway_known: bool | None = None
    display_actual_temperature: bool | None = None
    window: ShutterState | None = None
    #: Eco switch: key pressed last, and a counter that changes with every press.
    button: ButtonState | None = None
    button_presses: int = 0
    rssi: float | None = None
    last_seen: datetime | None = None  # UTC


@dataclass(slots=True, eq=False)
class MaxDevice:
    """A MAX! device known to the protocol handler."""

    address: str
    device_type: DeviceType
    serial: str | None = None
    firmware: str | None = None
    group: int = 0
    #: Last profile successfully written (or the factory profile after pairing);
    #: None if unknown, since profiles cannot be read from the device.
    week_profile: WeekProfile | None = None
    #: Devices this one sends to (its link partners), learned from radio traffic
    #: and from links we created. Devices in a room are usually linked to each other.
    link_partners: set[str] = field(default_factory=set)
    #: Name of the CUL to send through; None picks the stick with the best RSSI.
    preferred_cul: str | None = None
    state: MaxDeviceState = field(default_factory=MaxDeviceState)
    #: Last RSSI per receiving CUL name.
    rssi_by_cul: dict[str, float] = field(default_factory=dict)
    #: time.monotonic() until which the device listens without wake-up preamble.
    awake_until: float = 0.0
    _listeners: list[DeviceListener] = field(default_factory=list, repr=False)

    def add_listener(self, listener: DeviceListener) -> Callable[[], None]:
        """Register a callback invoked after every state update. Returns an unsubscribe function."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def update(self, **values: Any) -> None:
        """Update state fields; None values are ignored. Notifies listeners."""
        for key, value in values.items():
            if value is not None:
                setattr(self.state, key, value)
        self.notify()

    def notify(self) -> None:
        """Call all listeners."""
        for listener in list(self._listeners):
            try:
                listener(self)
            except Exception:
                _LOGGER.exception("Error in listener of MAX! device %s", self.address)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the static device data (for persistence)."""
        return {
            "address": self.address,
            "device_type": int(self.device_type),
            "serial": self.serial,
            "firmware": self.firmware,
            "group": self.group,
            "week_profile": self.week_profile.to_dict() if self.week_profile else None,
            "preferred_cul": self.preferred_cul,
            "link_partners": sorted(self.link_partners),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MaxDevice:
        """Restore a device serialised with to_dict()."""
        return cls(
            address=data["address"].lower(),
            device_type=DeviceType(data["device_type"]),
            serial=data.get("serial"),
            firmware=data.get("firmware"),
            group=data.get("group", 0),
            week_profile=WeekProfile.from_dict(data["week_profile"])
            if data.get("week_profile")
            else None,
            preferred_cul=data.get("preferred_cul"),
            link_partners=set(data.get("link_partners", ())),
        )
