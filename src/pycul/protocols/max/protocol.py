# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""eQ-3 MAX! (culfw "Moritz" mode) protocol handler.

The message handling follows FHEM's 14_CUL_MAX.pm (CUL_MAX_Parse,
CUL_MAX_Send, CUL_MAX_SendTimeInformation) and 10_MAX.pm (MAX_Parse,
MAX_Set), (c) 2012 Matthias Gehre, (c) 2019 Wzut, GPL-2.0-or-later, and
culfw's clib/rf_moritz.c (GPL-2.0). See NOTICE.md.

One MaxProtocol represents one MAX! network. It can be attached to several
CULs (FHEM: IOgrp): frames received by more than one stick are processed
once, and each frame is sent through the stick that hears the device best.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Hashable, Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pycul.exceptions import CulError, NakError, NoAckError
from pycul.protocols.base import CulProtocol
from pycul.protocols.max.const import (
    ACK_TIMEOUT,
    BROADCAST,
    DEFAULT_ADDRESS,
    DEFAULT_FAKE_SC_ADDRESS,
    DEFAULT_FAKE_WT_ADDRESS,
    FLAGS_DEVICE,
    FLAGS_GROUP,
    DeviceType,
    MessageType,
    Mode,
    ShutterState,
)
from pycul.protocols.max.device import MaxDevice
from pycul.protocols.max.message import InvalidFrameError, MaxMessage
from pycul.protocols.max.payloads import (
    PairPing,
    PayloadError,
    PushButtonState,
    SetTemperature,
    ShutterContactState,
    TemperatureConfig,
    ThermostatState,
    ValveConfig,
    WallThermostatControl,
    WallThermostatState,
    decode_time_information,
    encode_link_partner,
    encode_time_information,
)
from pycul.protocols.max.queue import OutgoingMessage, SendQueue
from pycul.protocols.max.weekprofile import (
    WEEKDAY_NAMES,
    DayProfile,
    WeekProfile,
    max_day_index,
)

if TYPE_CHECKING:
    from pycul.cul import Cul

_LOGGER = logging.getLogger(__name__)

RE_ADDRESS = re.compile(r"^[0-9a-f]{6}$")
RE_FIRMWARE_VERSION = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)")
#: Resend the time if a device's clock differs more than this (FHEM: broadcastTimeDiff).
TIME_DIFF_TOLERANCE = 10
#: Check every 5 minutes whether a thermostat needs a time update (one per check).
TIME_SYNC_CHECK_INTERVAL = 300.0
#: Frames heard by several sticks arrive within this window.
DUPLICATE_WINDOW = 2.0
#: Seconds a device stays awake after an acknowledged WakeUp (31 s minus margin, FHEM).
WAKE_UP_DURATION = 26.0
#: Queue a WakeUp first when at least this many frames wait for one device.
WAKE_UP_THRESHOLD = 3

#: Device type implied by a message a device sends on its own (FHEM: MAX_Parse autocreate).
TYPE_BY_MESSAGE: dict[MessageType, DeviceType] = {
    MessageType.THERMOSTAT_STATE: DeviceType.HEATING_THERMOSTAT,
    MessageType.WALL_THERMOSTAT_STATE: DeviceType.WALL_THERMOSTAT,
    MessageType.WALL_THERMOSTAT_CONTROL: DeviceType.WALL_THERMOSTAT,
    MessageType.SHUTTER_CONTACT_STATE: DeviceType.SHUTTER_CONTACT,
    MessageType.PUSH_BUTTON_STATE: DeviceType.PUSH_BUTTON,
}

#: Messages devices send to their link partners; they reveal links and groups.
_PEER_MESSAGES = frozenset(
    {
        MessageType.THERMOSTAT_STATE,
        MessageType.WALL_THERMOSTAT_STATE,
        MessageType.WALL_THERMOSTAT_CONTROL,
        MessageType.SHUTTER_CONTACT_STATE,
        MessageType.PUSH_BUTTON_STATE,
        MessageType.SET_TEMPERATURE,
    }
)

#: Device types a MAX! Cube links with each other when they share a room.
_ROOM_TYPES = frozenset(
    {
        DeviceType.HEATING_THERMOSTAT,
        DeviceType.HEATING_THERMOSTAT_PLUS,
        DeviceType.WALL_THERMOSTAT,
        DeviceType.SHUTTER_CONTACT,
    }
)

type DeviceAddedListener = Callable[[MaxDevice], None]
type MessageListener = Callable[[MaxMessage], None]


def normalize_address(address: str) -> str:
    """Return a lower case 6 digit hex address or raise ValueError."""
    address = address.strip().lower()
    if not RE_ADDRESS.match(address):
        raise ValueError(f"Invalid MAX! address {address!r}, expected 6 hex digits")
    return address


class WeekProfileError(CulError):
    """Some days of a week profile could not be written."""

    def __init__(self, failed: dict[int, Exception]) -> None:
        self.failed = failed
        days = ", ".join(f"{WEEKDAY_NAMES[d]} ({e})" for d, e in failed.items())
        super().__init__(f"Week profile not written for: {days}")


class MaxProtocol(CulProtocol):
    """Talks to MAX! devices through one or more CULs in Moritz mode.

    The CULs act as the devices' controller (like a MAX! Cube or FHEM's
    CUL_MAX) with the given `address`. Devices only accept commands from the
    controller they are paired with, so when migrating from FHEM keep its
    CUL_MAX address (default 123456).
    """

    name = "max"
    rf_mode = "moritz"
    line_prefixes = ("Z",)

    def __init__(
        self,
        address: str = DEFAULT_ADDRESS,
        devices: Iterable[MaxDevice] = (),
        *,
        adopt_devices: bool = True,
        probe_devices: bool = True,
        time_sync_interval: timedelta | None = timedelta(days=1),
        fake_wt_address: str | None = DEFAULT_FAKE_WT_ADDRESS,
        fake_sc_address: str | None = DEFAULT_FAKE_SC_ADDRESS,
        fast_send_after_wake_up: bool = False,
    ) -> None:
        """Create the handler.

        adopt_devices: add unknown devices that send messages to our address
            (they are already paired with us, e.g. after migrating from FHEM).
        probe_devices: with adopt_devices, also find paired devices that only
            talk to each other: a device heard sending to another device gets
            one time update; if it acknowledges, it is paired with us and is
            adopted. Devices of other controllers do not answer.
        time_sync_interval: periodically send the time to known thermostats;
            None disables it (time requests of devices are still answered).
        fake_wt_address / fake_sc_address: addresses of the virtual wall
            thermostat and shutter contact the CUL emulates (FHEM attributes
            fakeWTaddr / fakeSCaddr); None disables them.
        fast_send_after_wake_up: experimental. When several frames wait for
            one device, wake it up first and send the rest without the 1 s
            preamble, which saves ~90% of the duty cycle per frame. FHEM had
            this disabled because the first frame after the WakeUp got lost.
        """
        super().__init__()
        self.address = normalize_address(address)
        self.fake_wt_address = normalize_address(fake_wt_address) if fake_wt_address else None
        self.fake_sc_address = normalize_address(fake_sc_address) if fake_sc_address else None
        own = [a for a in (self.address, self.fake_wt_address, self.fake_sc_address) if a]
        if BROADCAST in own or len(set(own)) != len(own):
            raise ValueError("Controller and fake addresses must be distinct and not 000000")
        self._own_addresses = frozenset(own)
        self.devices: dict[str, MaxDevice] = {d.address: d for d in devices}
        self.adopt_devices = adopt_devices
        self.probe_devices = probe_devices
        #: Addresses asked whether they are paired with us (once per run).
        self._probed: set[str] = set()
        self.time_sync_interval = time_sync_interval
        self.fast_send_after_wake_up = fast_send_after_wake_up
        self.queue = SendQueue(self._transmit, self._select_cul, self._is_awake)
        self._counter = int(time.time()) & 0xFF
        self._ack_waiters: dict[tuple[str, int], asyncio.Future[bytes]] = {}
        self._recent_frames: dict[tuple[object, ...], tuple[str, float]] = {}
        self._pairing_until = 0.0
        self._device_added_listeners: list[DeviceAddedListener] = []
        self._message_listeners: list[MessageListener] = []
        self._last_time_sync: dict[str, float] = {}
        self._last_button_counter: dict[str, int] = {}
        self._time_sync_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------ CulProtocol

    def init_commands(self, cul: Cul) -> list[str]:
        """X21: report RSSI, Zr: Moritz mode, Za: auto-ack address, Zw: fake WT address.

        Zw makes culfw acknowledge SetTemperature frames sent to the fake wall
        thermostat. Firmware older than culfw 1.53 leaves Moritz mode on unknown
        Z commands, so it is only sent to newer sticks (FHEM: CUL_MAX_updateConfig).
        """
        commands = ["X21", "Zr", f"Za{self.address}"]
        if self.fake_wt_address and self._firmware_supports_fake_wt(cul):
            commands.append(f"Zw{self.fake_wt_address}")
        return commands

    @staticmethod
    def _firmware_supports_fake_wt(cul: Cul) -> bool:
        version = cul.version
        if version is None or version.is_a_culfw:
            return True
        # Builds carry suffixes, e.g. "1.66_nocredits" (compiled without duty cycle limit).
        match = RE_FIRMWARE_VERSION.match(version.version)
        if match is None:
            _LOGGER.warning(
                "%s: unknown culfw version %r, fake wall thermostat disabled",
                cul.name,
                version.version,
            )
            return False
        if (int(match["major"]), int(match["minor"])) < (1, 53):
            _LOGGER.warning(
                "%s: culfw %s is too old for the fake wall thermostat", cul.name, version.version
            )
            return False
        return True

    async def async_connected(self, cul: Cul) -> None:
        """Start the periodic time sync and resume sending."""
        if self.time_sync_interval and self._time_sync_task is None:
            self._time_sync_task = asyncio.create_task(self._time_sync_loop())
        self.queue.poke()

    async def async_cul_closed(self, cul: Cul) -> None:
        """Shut down once the last stick of this network is closed."""
        if all(c.closing for c in self._culs):
            await self.async_stop()

    async def async_stop(self) -> None:
        """Stop background tasks, cancel queued frames and pending Acks."""
        tasks = [*self._background_tasks]
        if self._time_sync_task:
            tasks.append(self._time_sync_task)
            self._time_sync_task = None
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.queue.stop()
        for future in self._ack_waiters.values():
            future.cancel()

    def handle_line(self, line: str, cul: Cul) -> None:
        """Decode a "Z" line and process the frame once, however many sticks heard it."""
        try:
            message = MaxMessage.from_line(line)
        except InvalidFrameError as err:
            _LOGGER.debug("%s: ignoring line: %s", cul.name, err)
            return
        if not self._is_duplicate(message, cul):
            self.handle_message(message)
        # Record per-stick reception (also for duplicates) to pick the best sender.
        device = self.devices.get(message.src)
        if device is not None and message.rssi is not None:
            device.rssi_by_cul[cul.name] = message.rssi

    def _is_duplicate(self, message: MaxMessage, cul: Cul) -> bool:
        """Return True if another stick delivered the same frame moments ago.

        Repetitions on the same stick are real retransmissions and are kept.
        """
        now = time.monotonic()
        if len(self._recent_frames) > 64:
            self._recent_frames = {
                k: v for k, v in self._recent_frames.items() if now - v[1] < DUPLICATE_WINDOW
            }
        key = (message.src, message.dst, message.counter, message.type_id, message.payload)
        seen = self._recent_frames.get(key)
        if seen is not None and seen[0] != cul.name and now - seen[1] < DUPLICATE_WINDOW:
            return True
        self._recent_frames[key] = (cul.name, now)
        return False

    # -------------------------------------------------------------- listeners

    def add_device_added_listener(self, listener: DeviceAddedListener) -> Callable[[], None]:
        """Call `listener` whenever a new device is paired or adopted."""
        self._device_added_listeners.append(listener)
        return lambda: self._device_added_listeners.remove(listener)

    def add_message_listener(self, listener: MessageListener) -> Callable[[], None]:
        """Call `listener` for every decoded frame (including foreign traffic)."""
        self._message_listeners.append(listener)
        return lambda: self._message_listeners.remove(listener)

    # ---------------------------------------------------------------- pairing

    @property
    def pairing(self) -> bool:
        """Return True while new devices are accepted."""
        return time.monotonic() < self._pairing_until

    def start_pairing(self, duration: float = 60.0) -> None:
        """Accept pairing requests of new devices for `duration` seconds."""
        self._pairing_until = time.monotonic() + duration
        _LOGGER.info("MAX! pairing enabled for %d s", duration)

    def stop_pairing(self) -> None:
        """Stop accepting new devices."""
        self._pairing_until = 0.0

    def add_device(self, device: MaxDevice) -> MaxDevice:
        """Register a device (e.g. restored from storage) and notify listeners."""
        if device.address in self.devices:
            return self.devices[device.address]
        self.devices[device.address] = device
        for listener in list(self._device_added_listeners):
            try:
                listener(device)
            except Exception:
                _LOGGER.exception("Error in device added listener")
        return device

    def remove_device(self, address: str) -> None:
        """Forget a device and drop its queued frames.

        It stays paired; it will be re-adopted if adopt_devices is set.
        """
        address = normalize_address(address)
        self.devices.pop(address, None)
        self.queue.clear(address)

    def link_clusters(self) -> list[set[str]]:
        """Return sets of devices linked with each other (rooms learned from links).

        Partners that were not heard themselves yet are included; the CUL's own
        (fake) addresses are not, as they are linked with devices of every room.
        """
        neighbours: dict[str, set[str]] = {address: set() for address in self.devices}
        for address, device in self.devices.items():
            for partner in device.link_partners - self._own_addresses:
                neighbours[address].add(partner)
                neighbours.setdefault(partner, set()).add(address)
        clusters: list[set[str]] = []
        seen: set[str] = set()
        for start in sorted(neighbours):
            if start in seen or not neighbours[start]:
                continue
            cluster: set[str] = set()
            todo = [start]
            while todo:
                address = todo.pop()
                if address not in cluster:
                    cluster.add(address)
                    todo.extend(neighbours[address] - cluster)
            seen |= cluster
            clusters.append(cluster)
        return clusters

    def devices_in_group(self, group: int) -> list[MaxDevice]:
        """Return the known devices with the given group id (room)."""
        return [d for d in self.devices.values() if group and d.group == group]

    # --------------------------------------------------------------- receiving

    def handle_message(self, message: MaxMessage) -> None:
        """Process a decoded frame."""
        _LOGGER.debug("Received %s", message)
        for listener in list(self._message_listeners):
            try:
                listener(message)
            except Exception:
                _LOGGER.exception("Error in message listener")

        if message.src in self._own_addresses:
            # Echo of culfw's auto-acks or of our other sticks' transmissions.
            return

        to_me = message.dst == self.address
        msg_type = message.type

        if msg_type is MessageType.PAIR_PING:
            self._handle_pair_ping(message, to_me)
            return
        if msg_type is MessageType.ACK and message.dst in self._own_addresses and message.payload:
            # Resolve before the device lookup: the sender may not be registered (yet).
            waiter = self._ack_waiters.get((message.src, message.counter))
            if waiter and not waiter.done():
                waiter.set_result(message.payload)

        # A device that just transmitted listens for a moment: send what waits for it
        # (shutter contacts cannot be woken up otherwise; FHEM: CUL_MAX_SQH).
        self.queue.release(message.src)
        self._probe_unknown_devices(message)

        device = self.devices.get(message.src)
        if device is None:
            device_type = TYPE_BY_MESSAGE.get(msg_type)
            if not (to_me and self.adopt_devices and device_type):
                return
            _LOGGER.info("Adopting MAX! device %s (%s)", message.src, device_type.name)
            device = self.add_device(MaxDevice(address=message.src, device_type=device_type))
            if message.rssi is not None:
                device.state.rssi = message.rssi

        device.state.last_seen = datetime.now(UTC)
        if message.rssi is not None:
            device.state.rssi = message.rssi
        self._learn_topology(device, message)

        try:
            if msg_type is MessageType.ACK:
                self._handle_ack(device, message)
            elif msg_type is MessageType.THERMOSTAT_STATE:
                self._apply_thermostat_state(device, ThermostatState.decode(message.payload))
            elif msg_type is MessageType.WALL_THERMOSTAT_STATE:
                self._apply_wall_thermostat_state(
                    device, WallThermostatState.decode(message.payload)
                )
            elif msg_type is MessageType.WALL_THERMOSTAT_CONTROL:
                control = WallThermostatControl.decode(message.payload)
                device.update(
                    target_temperature=control.target_temperature,
                    current_temperature=control.current_temperature,
                )
            elif msg_type is MessageType.SHUTTER_CONTACT_STATE:
                contact = ShutterContactState.decode(message.payload)
                device.update(
                    window=contact.state,
                    battery_low=contact.battery_low,
                    rf_error=contact.rf_error,
                )
            elif msg_type is MessageType.PUSH_BUTTON_STATE:
                self._apply_push_button_state(device, message)
            elif msg_type is MessageType.SET_TEMPERATURE:
                # A device announces a locally changed set point to its link partners.
                command = SetTemperature.decode(message.payload)
                device.state.until = command.until
                device.update(mode=command.mode, target_temperature=command.temperature or None)
            elif msg_type is MessageType.TIME_INFORMATION and to_me:
                self._handle_time_information(device, message)
            else:
                device.notify()  # at least last_seen / rssi changed
        except PayloadError as err:
            _LOGGER.warning("Invalid %s from %s: %s", msg_type.name, message.src, err)

    def _probe_unknown_devices(self, message: MaxMessage) -> None:
        """Ask unknown devices heard talking to each other whether they are ours.

        Devices paired with us only send to the controller now and then (time
        requests, some state reports); with a wall thermostat, the radiator
        thermostats talk almost only to it. FHEM knew them from its
        configuration; we ask instead.
        """
        if not (self.adopt_devices and self.probe_devices) or message.dst in self._own_addresses:
            return  # frames to us are handled by the regular adoption
        candidates: list[tuple[str, DeviceType]] = []
        if (device_type := TYPE_BY_MESSAGE.get(message.type)) is not None:
            candidates.append((message.src, device_type))
        if message.type is MessageType.WALL_THERMOSTAT_CONTROL and not message.is_broadcast:
            # Wall thermostats send this to their radiator thermostats.
            candidates.append((message.dst, DeviceType.HEATING_THERMOSTAT))
        for address, device_type in candidates:
            if address in self.devices or address in self._probed:
                continue
            self._probed.add(address)
            # A window contact or eco switch only listens right after it sent.
            listening_now = address == message.src and device_type in (
                DeviceType.SHUTTER_CONTACT,
                DeviceType.PUSH_BUTTON,
            )
            self._spawn(self._probe(address, device_type, listening_now))

    async def _probe(self, address: str, device_type: DeviceType, listening_now: bool) -> None:
        queued = self.enqueue(
            address,
            MessageType.TIME_INFORMATION,
            encode_time_information(datetime.now()),
            group=0,
            flags=FLAGS_GROUP,
            key=(address, MessageType.TIME_INFORMATION),
            refresh=lambda: encode_time_information(datetime.now()),
        )
        if listening_now:
            queued.hold_until_released = True
            queued.released = True
        try:
            ack = await queued.future
        except CulError as err:
            _LOGGER.info("%s did not answer, not paired with %s: %s", address, self.address, err)
            return
        if address in self.devices or ack is None:
            return
        _LOGGER.info("Adopting MAX! device %s (%s): paired with us", address, device_type.name)
        device = self.add_device(MaxDevice(address=address, device_type=device_type))
        self._last_time_sync[address] = time.monotonic()
        self._handle_ack(device, MaxMessage(0, 0, MessageType.ACK, address, self.address, 0, ack))

    def _learn_topology(self, device: MaxDevice, message: MaxMessage) -> None:
        """Learn groups and links from frames devices send to each other.

        Frames a device sends to us carry group 0. Only frames to its link
        partners (overheard, or sent to a whole group) show the group id and
        whom the device is linked with - FHEM ignores both.
        """
        if message.type not in _PEER_MESSAGES:
            return
        if message.flags & FLAGS_GROUP and message.group and device.group != message.group:
            _LOGGER.info("Learned: %s is in group %d", device.address, message.group)
            device.group = message.group
        if (
            not message.is_broadcast
            and message.dst not in self._own_addresses
            and message.dst not in device.link_partners
        ):
            _LOGGER.info("Learned: %s is linked to %s", device.address, message.dst)
            device.link_partners.add(message.dst)

    def _handle_ack(self, device: MaxDevice, message: MaxMessage) -> None:
        """Apply the state an Ack carries behind its status byte (FHEM: MAX_Parse "Ack").

        Every Ack does, whatever it acknowledges (set point, time, config...).
        """
        payload = message.payload
        if len(payload) < 2 or payload[0] & 0x80:
            device.notify()
            return
        state = payload[1:]
        device_type = device.device_type
        if device_type in (DeviceType.HEATING_THERMOSTAT, DeviceType.HEATING_THERMOSTAT_PLUS):
            if len(state) >= 3:
                self._apply_thermostat_state(device, ThermostatState.decode(state))
                return
        elif device_type is DeviceType.WALL_THERMOSTAT:
            if len(state) >= 3:
                self._apply_wall_thermostat_state(device, WallThermostatState.decode(state))
                return
        elif device_type is DeviceType.SHUTTER_CONTACT:
            contact = ShutterContactState.decode(state)
            device.update(
                window=contact.state, battery_low=contact.battery_low, rf_error=contact.rf_error
            )
            return
        elif device_type is DeviceType.PUSH_BUTTON:
            # Not a key press: only the status bits are meaningful here.
            button = PushButtonState.decode(state)
            device.update(battery_low=button.battery_low, rf_error=button.rf_error)
            return
        device.notify()

    @staticmethod
    def _apply_until(device: MaxDevice, mode: Mode, until: datetime | None) -> None:
        # Acks are too short to carry the end of a temporary mode; keep the known one.
        if until is not None or mode is not Mode.TEMPORARY:
            device.state.until = until

    def _apply_thermostat_state(self, device: MaxDevice, state: ThermostatState) -> None:
        self._apply_until(device, state.status.mode, state.until)
        device.update(
            mode=state.status.mode,
            target_temperature=state.target_temperature,
            current_temperature=state.current_temperature,
            valve_position=state.valve_position,
            battery_low=state.status.battery_low,
            rf_error=state.status.rf_error,
            panel_locked=state.status.panel_locked,
            dst_active=state.status.dst_active,
            gateway_known=state.status.gateway_known,
        )

    def _apply_wall_thermostat_state(self, device: MaxDevice, state: WallThermostatState) -> None:
        self._apply_until(device, state.status.mode, state.until)
        device.update(
            mode=state.status.mode,
            target_temperature=state.target_temperature,
            current_temperature=state.current_temperature,
            display_actual_temperature=state.display_actual_temperature,
            battery_low=state.status.battery_low,
            rf_error=state.status.rf_error,
            panel_locked=state.status.panel_locked,
            dst_active=state.status.dst_active,
            gateway_known=state.status.gateway_known,
        )

    def _apply_push_button_state(self, device: MaxDevice, message: MaxMessage) -> None:
        state = PushButtonState.decode(message.payload)
        presses = device.state.button_presses
        # One key press is sent to every link partner with the same counter.
        if (
            state.button is not None
            and self._last_button_counter.get(device.address) != message.counter
        ):
            self._last_button_counter[device.address] = message.counter
            presses += 1
        device.update(
            button=state.button,
            button_presses=presses,
            battery_low=state.battery_low,
            rf_error=state.rf_error,
        )

    def _handle_pair_ping(self, message: MaxMessage, to_me: bool) -> None:
        try:
            ping = PairPing.decode(message.payload)
        except PayloadError as err:
            _LOGGER.warning("Invalid PairPing from %s: %s", message.src, err)
            return
        known = message.src in self.devices
        _LOGGER.info(
            "PairPing from %s (%s, serial %s, firmware %s) to %s",
            message.src,
            ping.device_type.name,
            ping.serial,
            ping.firmware,
            message.dst,
        )
        # A PairPing to another controller means the device is paired elsewhere
        # and needs a factory reset first (FHEM: CUL_MAX_Parse "PairPing").
        if not message.is_broadcast and not to_me:
            _LOGGER.info("%s is paired to %s, not to us - ignoring", message.src, message.dst)
            return
        if not (self.pairing or to_me or known):
            _LOGGER.info("Pairing request from %s ignored, pairing is not enabled", message.src)
            return

        device = self.devices.get(message.src) or MaxDevice(
            address=message.src, device_type=ping.device_type
        )
        device.device_type = ping.device_type
        device.serial = ping.serial
        device.firmware = ping.firmware
        if message.is_broadcast and device.device_type.is_thermostat:
            # A broadcast PairPing comes from a factory reset device; re-pairing
            # (PairPing to us) keeps the configuration (FHEM: CUL_MAX_Parse).
            device.week_profile = WeekProfile.factory_default()
            device.group = 0
        if not known:
            self.add_device(device)
        self._spawn(self._send_pair_pong(device))

    async def _send_pair_pong(self, device: MaxDevice) -> None:
        try:
            await self.send(device.address, MessageType.PAIR_PONG, b"\x00", group=0)
            _LOGGER.info("Paired MAX! device %s", device.address)
            if device.device_type.is_thermostat:
                await self.send_time_information(device.address)
        except CulError as err:
            _LOGGER.warning("Pairing %s failed: %s", device.address, err)

    def _handle_time_information(self, device: MaxDevice, message: MaxMessage) -> None:
        device_time = decode_time_information(message.payload)
        if device_time is not None:
            diff = abs((datetime.now() - device_time).total_seconds())
            if diff <= TIME_DIFF_TOLERANCE:
                return
        _LOGGER.debug("%s requested the time (its clock: %s)", device.address, device_time)
        self._spawn(self._send_time_safely(device.address))

    async def _send_time_safely(self, address: str) -> None:
        try:
            await self.send_time_information(address)
        except CulError as err:
            _LOGGER.warning("Sending time to %s failed: %s", address, err)

    async def _time_sync_loop(self) -> None:
        assert self.time_sync_interval is not None
        interval = self.time_sync_interval.total_seconds()
        while True:
            await asyncio.sleep(TIME_SYNC_CHECK_INTERVAL)
            now = time.monotonic()
            for device in list(self.devices.values()):
                if not device.device_type.is_thermostat:
                    continue
                if now - self._last_time_sync.get(device.address, 0.0) < interval:
                    continue
                # One device per check keeps the duty cycle usage low (FHEM uses time slots).
                await self._send_time_safely(device.address)
                break

    def _spawn(self, coro) -> None:  # type: ignore[no-untyped-def]
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # ----------------------------------------------------------------- sending

    def _next_counter(self) -> int:
        self._counter = (self._counter + 1) & 0xFF
        return self._counter

    def _select_cul(self, message: OutgoingMessage) -> Cul | None:
        """Pick the stick to send through: preferred, else best RSSI, else most credit."""
        connected = self.connected_culs
        if not connected:
            return None
        device = self.devices.get(message.dst)
        if device is not None:
            if device.preferred_cul:
                for cul in connected:
                    if cul.name == device.preferred_cul:
                        return cul
            heard = [c for c in connected if c.name in device.rssi_by_cul]
            if heard:
                return max(heard, key=lambda c: device.rssi_by_cul[c.name])
        return max(connected, key=lambda c: c.credits or 0)

    def _is_awake(self, address: str) -> bool:
        device = self.devices.get(address)
        return device is not None and device.awake_until > time.monotonic()

    async def _transmit(self, message: OutgoingMessage, cul: Cul, preamble: bool) -> bytes | None:
        """Send one attempt of a queued frame and wait for its Ack."""
        if message.counter is None:
            message.counter = self._next_counter()
        frame = MaxMessage(
            counter=message.counter,
            flags=message.flags,
            type_id=message.msg_type,
            src=message.src,
            dst=message.dst,
            group=message.group,
            payload=message.payload,
        )
        command = ("Zs" if preamble else "Zf") + frame.to_hex()
        _LOGGER.debug("Sending %s via %s (attempt %d)", frame, cul.name, message.attempts + 1)
        if not message.wait_for_ack:
            await cul.async_send(command)
            return None
        key = (message.dst, message.counter)
        future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._ack_waiters[key] = future
        try:
            await cul.async_send(command)
            async with asyncio.timeout(ACK_TIMEOUT):
                ack = await future
        except TimeoutError:
            raise NoAckError(f"No Ack from {message.dst} for {message.msg_type.name}") from None
        finally:
            self._ack_waiters.pop(key, None)
        if ack[0] & 0x80:
            raise NakError(f"{message.dst} rejected {message.msg_type.name}: {ack.hex()}")
        if message.msg_type is MessageType.WAKE_UP:
            device = self.devices.get(message.dst)
            if device is not None:
                device.awake_until = time.monotonic() + WAKE_UP_DURATION
        return ack

    def enqueue(
        self,
        dst: str,
        msg_type: MessageType,
        payload: bytes = b"",
        *,
        group: int = 0,
        flags: int | None = None,
        src: str | None = None,
        key: Hashable | None = None,
        refresh: Callable[[], bytes] | None = None,
        expires: float | None = None,
    ) -> OutgoingMessage:
        """Queue a frame without waiting; await the returned entry's `future` for the Ack.

        group: group id; every device of the group applies the frame. Only
            set point and fake WT/SC frames use it (as in FHEM): a reset, link
            or profile must not reach the whole room.
        src: sender address, defaults to the controller address (used for the
            fake wall thermostat / shutter contact).
        key: unsent frames with the same key are replaced by newer ones.
        refresh: recomputes the payload right before each attempt.
        expires: give up if the frame could not be sent within this many seconds.
        """
        dst = normalize_address(dst)
        device = self.devices.get(dst)
        if flags is None:
            flags = FLAGS_GROUP if group else FLAGS_DEVICE
        hold = (
            device is not None
            and device.device_type is DeviceType.SHUTTER_CONTACT
            and msg_type is not MessageType.PAIR_PONG
        )
        message = OutgoingMessage(
            dst=dst,
            msg_type=msg_type,
            payload=payload,
            src=normalize_address(src) if src else self.address,
            group=group,
            flags=flags,
            wait_for_ack=dst != BROADCAST,
            hold_until_released=hold,
            key=key,
            refresh=refresh,
            expires_at=time.monotonic() + expires if expires else None,
        )
        queued = self.queue.submit(message)
        if (
            self.fast_send_after_wake_up
            and not hold
            and dst != BROADCAST
            and msg_type is not MessageType.WAKE_UP
            and not self._is_awake(dst)
            and self.queue.count_for(dst) >= WAKE_UP_THRESHOLD
            and not self.queue.count_for(dst, MessageType.WAKE_UP)
        ):
            wake_up = OutgoingMessage(
                dst=dst,
                msg_type=MessageType.WAKE_UP,
                payload=b"\x3f",
                src=self.address,
                group=0,
                flags=FLAGS_DEVICE,
            )
            self.queue.submit(wake_up, before_dst=True)
            wake_up.future.add_done_callback(lambda f: f.exception())  # mark as retrieved
        return queued

    async def send(
        self,
        dst: str,
        msg_type: MessageType,
        payload: bytes = b"",
        **kwargs,  # type: ignore[no-untyped-def]
    ) -> bytes | None:
        """Queue a frame and wait until it is acknowledged. Returns the Ack payload.

        Takes the same keyword arguments as enqueue(). Raises NoAckError,
        NakError, SendExpiredError, SendCancelledError or another CulError.
        """
        return await self.enqueue(dst, msg_type, payload, **kwargs).future

    # ---------------------------------------------------------------- commands

    async def set_temperature(
        self,
        address: str,
        temperature: float,
        mode: Mode = Mode.MANUAL,
        until: datetime | None = None,
    ) -> None:
        """Set the target temperature.

        mode=MANUAL keeps the temperature, mode=AUTO keeps it until the next
        switch point of the week profile, mode=TEMPORARY until `until`.

        If the device is in a group, the frame carries the group id and every
        device of the group applies it (as with a MAX! Cube or wall thermostat).
        """
        if until is not None:
            mode = Mode.TEMPORARY
        command = SetTemperature(mode=mode, temperature=temperature, until=until)
        await self._send_set_temperature(
            address, command, mode=mode, target_temperature=temperature, until=until
        )

    async def set_auto(self, address: str) -> None:
        """Switch to automatic mode following the week profile (the whole group, if any)."""
        await self._send_set_temperature(address, SetTemperature(Mode.AUTO, 0), mode=Mode.AUTO)

    async def set_boost(self, address: str) -> None:
        """Start boost mode (valve opens for the configured boost duration)."""
        await self._send_set_temperature(address, SetTemperature(Mode.BOOST, 0), mode=Mode.BOOST)

    async def _send_set_temperature(
        self, address: str, command: SetTemperature, **values: object
    ) -> None:
        address = normalize_address(address)
        device = self.devices.get(address)
        group = device.group if device else 0
        # Only the newest mode/temperature command matters - per group, as one
        # frame reaches all members.
        key = ("group", group) if group else address
        ack = await self.send(
            address,
            MessageType.SET_TEMPERATURE,
            command.encode(),
            group=group,
            key=(key, MessageType.SET_TEMPERATURE),
        )
        self._optimistic_update(address, ack, **values)
        # Members other than the addressed one do not acknowledge; they report
        # their state with their next status message.
        for member in self.devices_in_group(group):
            if member.address != address and member.device_type.is_thermostat:
                member.update(**values)

    async def set_comfort(self, address: str) -> None:
        """Set the comfort temperature (experimental, FHEM does not use this message)."""
        await self.send(address, MessageType.SET_COMFORT_TEMPERATURE)

    async def set_eco(self, address: str) -> None:
        """Set the eco temperature (experimental, FHEM does not use this message)."""
        await self.send(address, MessageType.SET_ECO_TEMPERATURE)

    async def configure_temperatures(self, address: str, config: TemperatureConfig) -> None:
        """Write comfort/eco/min/max/offset/window-open settings."""
        address = normalize_address(address)
        await self.send(
            address,
            MessageType.CONFIG_TEMPERATURES,
            config.encode(),
            key=(address, MessageType.CONFIG_TEMPERATURES),
        )

    async def configure_valve(self, address: str, config: ValveConfig) -> None:
        """Write boost/decalcification/valve settings of a radiator thermostat."""
        address = normalize_address(address)
        await self.send(
            address,
            MessageType.CONFIG_VALVE,
            config.encode(),
            key=(address, MessageType.CONFIG_VALVE),
        )

    async def set_display_actual_temperature(self, address: str, actual: bool) -> None:
        """Wall thermostat: show the measured (True) or the target temperature (False)."""
        address = normalize_address(address)
        await self.send(
            address,
            MessageType.SET_DISPLAY_ACTUAL_TEMPERATURE,
            bytes([0x04 if actual else 0x00]),
            key=(address, MessageType.SET_DISPLAY_ACTUAL_TEMPERATURE),
        )
        self._optimistic_update(address, None, display_actual_temperature=actual)

    async def factory_reset(self, address: str) -> None:
        """Reset a device to factory settings; it forgets its pairing and links."""
        await self.send(address, MessageType.RESET)
        device = self.devices.get(normalize_address(address))
        if device is not None:
            device.week_profile = WeekProfile.factory_default()
            device.group = 0

    async def wake_up(self, address: str) -> None:
        """Keep a device awake for ~30 s, e.g. before several commands (FHEM: MAX_WakeUp)."""
        await self.send(address, MessageType.WAKE_UP, b"\x3f")

    # ------------------------------------------------------------------ groups

    async def set_group(self, address: str, group: int) -> None:
        """Put a device into group (room) `group`; 0 removes it from its group.

        Devices of one group share commands sent with the group flag, e.g. a
        wall thermostat and the radiator thermostats of a room.
        """
        await self._enqueue_group_id(address, group).future
        self._set_known_group(address, group)

    def _enqueue_group_id(self, address: str, group: int) -> OutgoingMessage:
        if not 0 <= group <= 255:
            raise ValueError("group must be within 0..255")
        address = normalize_address(address)
        return self.enqueue(
            address,
            MessageType.SET_GROUP_ID if group else MessageType.REMOVE_GROUP_ID,
            bytes([group]),
            key=(address, "group"),
        )

    def _set_known_group(self, address: str, group: int) -> None:
        device = self.devices.get(normalize_address(address))
        if device is not None:
            device.group = group
            device.notify()

    def group_representative(self, group: int) -> MaxDevice:
        """Return the thermostat of a group that group frames are addressed to.

        Group frames reach every member, but only the addressed one
        acknowledges: pick the thermostat with the best reception.
        """
        members = [d for d in self.devices_in_group(group) if d.device_type.is_thermostat]
        if not members:
            raise ValueError(f"No known thermostats in group {group}")

        def reception(device: MaxDevice) -> float:
            if device.rssi_by_cul:
                return max(device.rssi_by_cul.values())
            return device.state.rssi if device.state.rssi is not None else -200.0

        return max(members, key=reception)

    async def set_group_temperature(
        self,
        group: int,
        temperature: float,
        mode: Mode = Mode.MANUAL,
        until: datetime | None = None,
    ) -> None:
        """Set the temperature of all thermostats in a group with one frame.

        FHEM sends one frame per member, which costs a second of air time each.
        """
        representative = self.group_representative(group)
        await self.set_temperature(representative.address, temperature, mode, until)

    async def set_group_auto(self, group: int) -> None:
        """Switch all thermostats in a group to automatic mode with one frame."""
        await self.set_auto(self.group_representative(group).address)

    async def group_devices(
        self,
        addresses: Iterable[str],
        group: int,
        *,
        link: bool = True,
        shared_window_contacts: Iterable[str] = (),
    ) -> dict[str, Exception | None]:
        """Make devices one heating zone, like the MAX! Cube does a "room".

        All devices get the group id; thermostats and window contacts are
        linked with each other in both directions, so a wall thermostat
        controls the radiator thermostats and a window contact lowers all of
        them. Window contacts are not linked with each other. Links already
        known are not sent again. Group 0 removes the devices from their group
        and keeps the links.

        A zone is not necessarily a room: a large room with several wall
        thermostats is several zones. `shared_window_contacts` are window
        contacts that act on this zone as well as on others (e.g. one door
        between two zones): they are linked with this zone's thermostats but
        keep their group. Leave such contacts without group (0); whether a
        thermostat accepts a contact's group frame for another group is not
        known.

        Frames to window contacts are sent when the contact transmits next
        (open or close the window). Returns an error or None per device.
        """
        if not 0 <= group <= 255:
            raise ValueError("group must be within 0..255")
        members = self._known_devices(addresses)
        shared = self._known_devices(shared_window_contacts)
        if any(d.device_type is not DeviceType.SHUTTER_CONTACT for d in shared):
            raise ValueError("shared_window_contacts must be window contacts")
        shared = [d for d in shared if d not in members]
        wall_thermostats = [d for d in members if d.device_type is DeviceType.WALL_THERMOSTAT]
        if group and len(wall_thermostats) > 1:
            _LOGGER.warning(
                "Group %d gets %d wall thermostats; they will overrule each other",
                group,
                len(wall_thermostats),
            )
        if not (link and group):
            partners, shared = [], []
        else:
            partners = [d for d in members if d.device_type in _ROOM_TYPES]
        thermostats = [d for d in partners if d.device_type.is_thermostat]

        async def setup(device: MaxDevice, set_group: bool, candidates: list[MaxDevice]) -> None:
            # Queue everything at once: a window contact takes all of it in
            # the moment it listens after transmitting.
            frames: dict[str, OutgoingMessage] = {}
            if set_group:
                frames[""] = self._enqueue_group_id(device.address, group)
            if device.device_type in _ROOM_TYPES:
                for partner in candidates:
                    if (
                        partner is device
                        or partner.address in device.link_partners
                        or device.device_type is partner.device_type is DeviceType.SHUTTER_CONTACT
                    ):
                        continue
                    frames[partner.address] = self.enqueue(
                        device.address,
                        MessageType.ADD_LINK_PARTNER,
                        encode_link_partner(partner.address, partner.device_type),
                    )
            results = await asyncio.gather(
                *(f.future for f in frames.values()), return_exceptions=True
            )
            for partner, result in zip(frames, results, strict=True):
                if isinstance(result, Exception):
                    continue
                if partner:
                    self._update_link_partners(device.address, partner, add=True)
                else:
                    self._set_known_group(device.address, group)
            error = next((r for r in results if isinstance(r, Exception)), None)
            if error is not None:
                raise error

        tasks = [
            *(
                setup(d, True, [*partners, *shared] if d.device_type.is_thermostat else partners)
                for d in members
            ),
            *(setup(d, False, thermostats) for d in shared),
        ]
        devices = [*members, *shared]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {
            d.address: r if isinstance(r, Exception) else None
            for d, r in zip(devices, results, strict=True)
        }

    def _known_devices(self, addresses: Iterable[str]) -> list[MaxDevice]:
        devices = []
        for address in addresses:
            device = self.devices.get(normalize_address(address))
            if device is None:
                raise ValueError(f"Unknown device {address}")
            devices.append(device)
        return devices

    # ------------------------------------------------------------ week profile

    async def set_week_profile(
        self, address: str, profile: WeekProfile, *, force: bool = False
    ) -> list[int]:
        """Write a week profile; returns the weekdays (Monday = 0) that were sent.

        Only days that differ from the last known profile are sent (a full
        profile needs up to 14 frames, more than one full duty cycle budget),
        unless the profile is unknown or `force` is set. The device's
        `week_profile` is updated per day as soon as all frames of that day
        are acknowledged. Raises WeekProfileError listing days that failed.
        """
        address = normalize_address(address)
        device = self.devices.get(address)
        known = device.week_profile if device else None
        days = list(range(7)) if force else profile.changed_days(known)

        queued: dict[int, list[OutgoingMessage]] = {}
        for weekday in days:
            queued[weekday] = [
                self.enqueue(
                    address,
                    MessageType.CONFIG_WEEK_PROFILE,
                    payload,
                    key=(address, MessageType.CONFIG_WEEK_PROFILE, payload[0]),
                )
                for payload in profile.days[weekday].config_payloads(max_day_index(weekday))
            ]

        failed: dict[int, Exception] = {}
        written = known
        for weekday, messages in queued.items():
            results = await asyncio.gather(*(m.future for m in messages), return_exceptions=True)
            error = next((r for r in results if isinstance(r, Exception)), None)
            if error is not None:
                failed[weekday] = error
                continue
            if written is not None:
                written = written.replace(weekday, profile.days[weekday])
        if written is None and not failed and len(days) == 7:
            written = profile
        if device is not None and written is not device.week_profile:
            device.week_profile = written
            device.notify()
        if failed:
            raise WeekProfileError(failed)
        return days

    async def set_day_profile(self, address: str, weekday: int, day: DayProfile) -> None:
        """Write the profile of one weekday (Monday = 0)."""
        address = normalize_address(address)
        device = self.devices.get(address)
        if device is not None and device.week_profile is not None:
            await self.set_week_profile(address, device.week_profile.replace(weekday, day))
            return
        # Profile unknown: send just this day; the full profile stays unknown.
        for payload in day.config_payloads(max_day_index(weekday)):
            await self.send(
                address,
                MessageType.CONFIG_WEEK_PROFILE,
                payload,
                key=(address, MessageType.CONFIG_WEEK_PROFILE, payload[0]),
            )

    # ---------------------------------------------------- link partners / fakes

    async def add_link_partner(self, address: str, partner: str, partner_type: DeviceType) -> None:
        """Link `partner` to the device at `address` (FHEM: set associate).

        Links are directional: to pair a radiator thermostat with a wall
        thermostat, associate each with the other.
        """
        partner = normalize_address(partner)
        payload = encode_link_partner(partner, partner_type)
        await self.send(address, MessageType.ADD_LINK_PARTNER, payload)
        self._update_link_partners(address, partner, add=True)

    async def remove_link_partner(
        self, address: str, partner: str, partner_type: DeviceType
    ) -> None:
        """Remove a link created with add_link_partner (FHEM: set deassociate)."""
        partner = normalize_address(partner)
        payload = encode_link_partner(partner, partner_type)
        await self.send(address, MessageType.REMOVE_LINK_PARTNER, payload)
        self._update_link_partners(address, partner, add=False)

    def _update_link_partners(self, address: str, partner: str, *, add: bool) -> None:
        device = self.devices.get(normalize_address(address))
        if device is None:
            return
        if add:
            device.link_partners.add(partner)
        else:
            device.link_partners.discard(partner)
        device.notify()

    @staticmethod
    def _require_fake(fake: str | None, name: str) -> str:
        if fake is None:
            raise ValueError(f"The fake {name} is disabled (no address configured)")
        return fake

    async def associate_fake_wall_thermostat(self, address: str, associate: bool = True) -> None:
        """Link (or unlink) the CUL's fake wall thermostat with a radiator thermostat."""
        fake = self._require_fake(self.fake_wt_address, "wall thermostat")
        method = self.add_link_partner if associate else self.remove_link_partner
        await method(address, fake, DeviceType.WALL_THERMOSTAT)

    async def associate_fake_shutter_contact(self, address: str, associate: bool = True) -> None:
        """Link (or unlink) the CUL's fake shutter contact with a thermostat."""
        fake = self._require_fake(self.fake_sc_address, "shutter contact")
        method = self.add_link_partner if associate else self.remove_link_partner
        await method(address, fake, DeviceType.SHUTTER_CONTACT)

    async def send_fake_wall_thermostat(
        self, address: str, target_temperature: float, current_temperature: float
    ) -> None:
        """Send set point and measured temperature as the fake wall thermostat.

        The radiator thermostat then regulates on `current_temperature` (e.g.
        from an external room sensor) instead of its own sensor. It must be
        associated first and expects regular updates, like a real wall
        thermostat would send them (FHEM: CUL_MAX set fakeWT).
        """
        fake = self._require_fake(self.fake_wt_address, "wall thermostat")
        address = normalize_address(address)
        control = WallThermostatControl(target_temperature, current_temperature)
        device = self.devices.get(address)
        await self.send(
            address,
            MessageType.WALL_THERMOSTAT_CONTROL,
            control.encode(),
            src=fake,
            group=device.group if device else 0,
            key=(address, MessageType.WALL_THERMOSTAT_CONTROL),
        )

    async def send_fake_shutter_contact(self, address: str, window_open: bool) -> None:
        """Report the window as open/closed as the fake shutter contact.

        An associated thermostat switches to its window-open temperature while
        open (FHEM: CUL_MAX set fakeSC).
        """
        fake = self._require_fake(self.fake_sc_address, "shutter contact")
        address = normalize_address(address)
        state = ShutterContactState(
            state=ShutterState.OPEN if window_open else ShutterState.CLOSED,
            rf_error=False,
            battery_low=False,
        )
        device = self.devices.get(address)
        group = device.group if device else 0
        await self.send(
            address,
            MessageType.SHUTTER_CONTACT_STATE,
            state.encode(),
            src=fake,
            group=group,
            # FHEM uses 0x06 for single devices; real contacts send the same.
            flags=FLAGS_GROUP if group else 0x06,
            key=(address, MessageType.SHUTTER_CONTACT_STATE),
        )

    async def send_time_information(self, address: str) -> None:
        """Send the current local time to a device (FHEM: CUL_MAX_SendTimeInformation).

        The payload is computed right before sending, so a frame that waited
        for duty cycle credit still carries the correct time.
        """
        address = normalize_address(address)
        await self.send(
            address,
            MessageType.TIME_INFORMATION,
            encode_time_information(datetime.now()),
            group=0,
            flags=FLAGS_GROUP,
            key=(address, MessageType.TIME_INFORMATION),
            refresh=lambda: encode_time_information(datetime.now()),
        )
        self._last_time_sync[address] = time.monotonic()

    def _optimistic_update(self, address: str, ack: bytes | None, **values: object) -> None:
        # An Ack with state bytes has already updated the device; a state report
        # may even have arrived since, which we must not overwrite.
        if ack is not None and len(ack) >= 4:
            return
        device = self.devices.get(normalize_address(address))
        if device is not None:
            device.update(**values)
