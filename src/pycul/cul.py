# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Connection to a busware CUL (or compatible) stick running culfw / a-culfw.

The line protocol and command set follow culfw (http://culfw.de, GPL-2.0)
and FHEM's 00_CUL.pm (GPL-2.0-or-later), see NOTICE.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pycul.exceptions import (
    CulConnectionError,
    CulCreditError,
    CulError,
    CulSendError,
    CulTimeoutError,
)
from pycul.protocols.base import CulProtocol

_LOGGER = logging.getLogger(__name__)

DEFAULT_BAUDRATE = 38400
COMMAND_TIMEOUT = 3.0
#: A send with long preamble occupies the stick for ~1.1 s, plus some slack.
SEND_TIMEOUT = 5.0
CLOSE_TIMEOUT = 2.0
RECONNECT_MIN_DELAY = 5.0
RECONNECT_MAX_DELAY = 60.0
#: Consecutive unanswered commands after which the stick counts as hung (e.g. a
#: USB port that stopped working without the device disappearing).
MAX_SILENT_TIMEOUTS = 2

RE_VERSION = re.compile(r"^V\s+(?P<version>\S+)")
#: Answer to "X": tx report flags (hex) followed by credit10ms.
RE_CREDITS = re.compile(r"^(?P<flags>[0-9A-F]{2})\s+(?P<credits>\d+)$")
RE_SEND_ERROR = re.compile(r"^(LOVF|LENERR|ZERR.*)$")

type StreamFactory = Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]
type ConnectionListener = Callable[[bool], None]


@dataclass(slots=True)
class CulVersion:
    """Firmware version reported by the stick."""

    raw: str
    version: str
    is_a_culfw: bool

    @classmethod
    def parse(cls, line: str) -> CulVersion:
        """Parse the answer of the "V" command, e.g. "V 1.67 CUL868"."""
        match = RE_VERSION.match(line)
        return cls(
            raw=line,
            version=match.group("version") if match else "unknown",
            is_a_culfw="a-culfw" in line,
        )


def rssi_to_dbm(raw: int) -> float:
    """Convert the CC1101 RSSI byte to dBm (see FHEM 00_CUL.pm, CUL_Parse)."""
    return (raw - 256) / 2 - 74 if raw >= 128 else raw / 2 - 74


class _Waiter:
    """A pending request for a line matching a pattern."""

    __slots__ = ("future", "pattern")

    def __init__(self, pattern: re.Pattern[str]) -> None:
        self.pattern = pattern
        self.future: asyncio.Future[str] = asyncio.get_running_loop().create_future()


class Cul:
    """A CUL stick, reachable via a serial device or a pyserial URL.

    `url` may be a device path ("/dev/ttyACM0", "/dev/serial/by-id/...") or any
    pyserial URL, e.g. "socket://192.168.1.10:2323" for ser2net or a CUNO.
    """

    def __init__(
        self,
        url: str,
        baudrate: int = DEFAULT_BAUDRATE,
        *,
        name: str | None = None,
        reconnect: bool = True,
        stream_factory: StreamFactory | None = None,
    ) -> None:
        """Create a CUL. `name` identifies the stick when several are used (default: url)."""
        self.url = url
        self.name = name or url
        self.baudrate = baudrate
        self._reconnect_enabled = reconnect
        self._stream_factory = stream_factory or self._open_serial
        self._protocols: list[CulProtocol] = []
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._waiters: list[_Waiter] = []
        self._command_lock = asyncio.Lock()
        self._connection_listeners: list[ConnectionListener] = []
        self._raw_listeners: list[Callable[[str], None]] = []
        self._closing = False
        #: True once connect() completed; cleared when the connection is lost.
        self._ready = False
        self._lost = False
        self._timeouts = 0
        self._reconnect_now = asyncio.Event()
        self.version: CulVersion | None = None
        #: Remaining send budget in units of 10 ms (1% duty cycle, max 900).
        self.credits: int | None = None

    # ------------------------------------------------------------------ setup

    def add_protocol(self, protocol: CulProtocol) -> None:
        """Attach a protocol. All protocols on one stick must share the RF mode."""
        for existing in self._protocols:
            if existing.rf_mode != protocol.rf_mode:
                raise ValueError(
                    f"Protocol {protocol.name} needs RF mode {protocol.rf_mode}, but "
                    f"{existing.name} already uses {existing.rf_mode} on this CUL"
                )
        protocol.attach(self)
        self._protocols.append(protocol)

    def add_connection_listener(self, listener: ConnectionListener) -> Callable[[], None]:
        """Register a callback for connection state changes. Returns an unsubscribe function."""
        self._connection_listeners.append(listener)
        return lambda: self._connection_listeners.remove(listener)

    def add_raw_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        """Register a callback that receives every line read from the stick."""
        self._raw_listeners.append(listener)
        return lambda: self._raw_listeners.remove(listener)

    @property
    def closing(self) -> bool:
        """Return True after close() was called."""
        return self._closing

    def __repr__(self) -> str:
        return f"Cul({self.name!r})"

    @property
    def connected(self) -> bool:
        """Return True while the stick is connected and initialised."""
        return self._ready and self._writer is not None

    # ------------------------------------------------------------- lifecycle

    async def connect(self) -> None:
        """Open the connection and initialise the stick and all protocols.

        Raises CulConnectionError if the stick cannot be opened or does not
        answer like a culfw device.
        """
        self._closing = False
        try:
            self._reader, self._writer = await self._stream_factory()
        except Exception as err:  # OSError, serial.SerialException, ...
            raise CulConnectionError(f"Cannot open {self.url}: {err}") from err

        self._reader_task = asyncio.create_task(self._read_loop(), name="pycul-reader")
        try:
            await self._initialise()
        except Exception:
            await self._teardown()
            raise
        self._ready = True
        self._lost = False
        self._timeouts = 0
        self._notify_connection(True)
        for protocol in self._protocols:
            await protocol.async_connected(self)

    async def close(self) -> None:
        """Close the connection and stop reconnecting."""
        self._closing = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconnect_task
            self._reconnect_task = None
        for protocol in self._protocols:
            await protocol.async_cul_closed(self)
        was_connected = self.connected
        await self._teardown()
        if was_connected:
            self._notify_connection(False)

    async def _open_serial(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        # Imported lazily so tests and non-serial transports do not need it.
        import serial_asyncio_fast

        return await serial_asyncio_fast.open_serial_connection(
            url=self.url, baudrate=self.baudrate
        )

    async def _initialise(self) -> None:
        # Some sticks print garbage after opening; an empty line resyncs culfw's parser.
        self._write("")
        for attempt in range(3):
            try:
                line = await self._query("V", RE_VERSION, COMMAND_TIMEOUT)
                break
            except CulTimeoutError:
                if attempt == 2:
                    raise CulConnectionError(
                        f"{self.url} did not answer the version command; is it a CUL?"
                    ) from None
        self.version = CulVersion.parse(line)
        _LOGGER.info("Connected to %s, firmware %s", self.url, self.version.raw)

        for protocol in self._protocols:
            for command in protocol.init_commands(self):
                self._write(command)
        await self.async_get_credits()

    async def _teardown(self) -> None:
        self._ready = False
        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._writer:
            # Closing the port of an unplugged stick may fail, or never finish.
            with contextlib.suppress(Exception):
                self._writer.close()
                async with asyncio.timeout(CLOSE_TIMEOUT):
                    await self._writer.wait_closed()
        self._writer = None
        self._reader = None
        self.version = None
        self._fail_waiters(CulConnectionError("Connection closed"))

    def _fail_waiters(self, error: CulError) -> None:
        for waiter in self._waiters:
            if not waiter.future.done():
                waiter.future.set_exception(error)
        self._waiters.clear()

    def _notify_connection(self, connected: bool) -> None:
        for listener in list(self._connection_listeners):
            try:
                listener(connected)
            except Exception:
                _LOGGER.exception("Error in connection listener")

    def _connection_lost(self) -> None:
        """Start tearing down and reconnecting, once per lost connection."""
        if self._lost or self._closing or not self._ready:
            return
        self._lost = True
        self._reconnect_task = asyncio.create_task(self._handle_connection_lost())

    async def _handle_connection_lost(self) -> None:
        _LOGGER.warning("Connection to %s lost", self.url)
        await self._teardown()
        self._notify_connection(False)
        if self._reconnect_enabled and not self._closing:
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    def request_reconnect(self) -> None:
        """Try to connect now, e.g. because the stick was plugged in again.

        Skips the current back-off wait, or starts reconnecting if the stick is
        not connected and nothing is trying (e.g. after a failed first connect()).
        """
        if self._closing or self.connected:
            return
        self._reconnect_now.set()
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        delay = RECONNECT_MIN_DELAY
        while not self._closing:
            # Wait for the back-off delay or for request_reconnect().
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(delay):
                    await self._reconnect_now.wait()
            self._reconnect_now.clear()
            try:
                await self.connect()
            except CulError as err:
                # Not only CulConnectionError: a stick that answers "V" but not "X"
                # raises CulTimeoutError, which used to end the reconnect loop.
                _LOGGER.debug("Reconnect to %s failed: %s", self.url, err)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)
            except Exception:
                _LOGGER.exception("Unexpected error reconnecting to %s", self.url)
                delay = RECONNECT_MAX_DELAY
            else:
                return

    # ---------------------------------------------------------------- reading

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                data = await self._reader.readline()
                if not data:
                    break
                line = data.decode("ascii", errors="replace").strip()
                if line:
                    self._dispatch_line(line)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.debug("Read error on %s: %s", self.url, err)
        # EOF or read error (e.g. stick unplugged): a separate task tears down,
        # as this task cannot cancel itself.
        self._reader_task = None
        if self._ready:
            self._connection_lost()
        else:
            # Still initialising: let connect() fail now instead of timing out.
            self._fail_waiters(CulConnectionError(f"{self.url} closed the connection"))

    def _dispatch_line(self, line: str) -> None:
        _LOGGER.debug("<- %s", line)
        self._timeouts = 0  # the stick is alive
        for listener in list(self._raw_listeners):
            try:
                listener(line)
            except Exception:
                _LOGGER.exception("Error in raw listener")

        for waiter in self._waiters:
            if not waiter.future.done() and waiter.pattern.match(line):
                waiter.future.set_result(line)
                self._waiters.remove(waiter)
                return

        for protocol in self._protocols:
            if protocol.handles(line):
                try:
                    protocol.handle_line(line, self)
                except Exception:
                    _LOGGER.exception("Protocol %s failed to handle %r", protocol.name, line)
                return
        _LOGGER.debug("Unhandled line from %s: %s", self.url, line)

    # ---------------------------------------------------------------- writing

    def _write(self, command: str) -> None:
        if self._writer is None:
            raise CulConnectionError(f"{self.url} is not connected")
        _LOGGER.debug("-> %s", command)
        try:
            self._writer.write(f"{command}\n".encode("ascii"))
        except Exception as err:  # OSError, SerialException: stick unplugged
            self._connection_lost()
            raise CulConnectionError(f"Cannot write to {self.url}: {err}") from err

    def _note_timeout(self) -> None:
        """Count a command the stick did not answer; give up on a hung stick."""
        self._timeouts += 1
        if self._timeouts >= MAX_SILENT_TIMEOUTS and self._ready:
            _LOGGER.warning("%s stopped answering", self.url)
            self._connection_lost()

    def _add_waiter(self, pattern: re.Pattern[str]) -> _Waiter:
        waiter = _Waiter(pattern)
        self._waiters.append(waiter)
        return waiter

    def _remove_waiter(self, waiter: _Waiter) -> None:
        with contextlib.suppress(ValueError):
            self._waiters.remove(waiter)

    async def _query(self, command: str, pattern: re.Pattern[str], timeout: float) -> str:
        waiter = self._add_waiter(pattern)
        try:
            self._write(command)
            async with asyncio.timeout(timeout):
                return await waiter.future
        except TimeoutError:
            self._note_timeout()
            raise CulTimeoutError(f"No answer to {command!r} from {self.url}") from None
        finally:
            self._remove_waiter(waiter)

    def write_raw(self, command: str) -> None:
        """Send a raw culfw command without waiting for an answer."""
        self._write(command)

    async def async_query(
        self, command: str, pattern: str | re.Pattern[str], timeout: float = COMMAND_TIMEOUT
    ) -> str:
        """Send a command and return the first line matching `pattern`."""
        compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
        async with self._command_lock:
            return await self._query(command, compiled, timeout)

    async def async_get_credits(self) -> int:
        """Query and return the remaining send credit (credit10ms)."""
        async with self._command_lock:
            line = await self._query("X", RE_CREDITS, COMMAND_TIMEOUT)
        self.credits = int(RE_CREDITS.match(line)["credits"])  # type: ignore[index]
        return self.credits

    async def async_send(self, command: str) -> None:
        """Send a radio command (e.g. "Zs...") and confirm the stick accepted it.

        culfw processes commands sequentially and stays silent on a successful
        send. We therefore follow the send command with "X": its answer marks
        the end of the transmission and reports the remaining credit, while an
        error line (LOVF, LENERR, ZERR...) would arrive before it.
        """
        async with self._command_lock:
            error = self._add_waiter(RE_SEND_ERROR)
            credits = self._add_waiter(RE_CREDITS)
            try:
                self._write(command)
                self._write("X")
                async with asyncio.timeout(SEND_TIMEOUT):
                    line = await credits.future
            except TimeoutError:
                self._note_timeout()
                raise CulTimeoutError(f"No confirmation for {command!r} from {self.url}") from None
            finally:
                self._remove_waiter(error)
                self._remove_waiter(credits)

        self.credits = int(RE_CREDITS.match(line)["credits"])  # type: ignore[index]
        if error.future.done():
            message = error.future.result()
            if message == "LOVF":
                raise CulCreditError(
                    f"Duty cycle limit reached on {self.url} (credit10ms={self.credits})"
                )
            raise CulSendError(f"{self.url} rejected {command!r}: {message}")
        error.future.cancel()
