# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Test fixtures: an in-memory fake CUL stick."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from pycul import Cul


class FakeWriter:
    """Stands in for asyncio.StreamWriter and answers like culfw."""

    def __init__(self, stick: FakeStick) -> None:
        self._stick = stick
        self._buffer = b""

    def write(self, data: bytes) -> None:
        if not self._stick.plugged:
            raise OSError(5, "Input/output error")
        self._buffer += data
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._stick.on_command(line.decode())

    def close(self) -> None:
        self._stick.reader.feed_eof()
        self._stick.closed = True

    async def wait_closed(self) -> None:
        return None


class FakeStick:
    """A scripted culfw stick."""

    def __init__(self) -> None:
        self.reader = asyncio.StreamReader()
        self.writer = FakeWriter(self)
        self.commands: list[str] = []
        #: Send commands the stick accepted (not refused with LOVF).
        self.transmitted: list[str] = []
        self.credits = 900
        self.version = "V 1.67 CUL868"
        #: Called for every Zs/Zf command; may return lines to emit.
        self.on_send: Callable[[str], list[str]] | None = None
        self.plugged = True
        #: The stick swallows commands without answering (hung USB port).
        self.hung = False
        self.closed = False
        self.opened = 0

    def unplug(self) -> None:
        """Pull the stick: the port reports EOF, writes and opening fail."""
        self.plugged = False
        self.reader.feed_eof()

    def emit(self, line: str) -> None:
        self.reader.feed_data(f"{line}\r\n".encode())

    def on_command(self, command: str) -> None:
        self.commands.append(command)
        if self.hung:
            return
        if command == "V":
            self.emit(self.version)
        elif command == "X":
            self.emit(f"21  {self.credits}")
        elif command.startswith(("Zs", "Zf")):
            cost = (100 if command[1] == "s" else 0) + (len(command) - 2) * 4 // 10
            if cost > self.credits:
                self.emit("LOVF")
                return
            self.credits -= cost
            self.transmitted.append(command)
            if self.on_send:
                replies = self.on_send(command)
                loop = asyncio.get_running_loop()
                # Device answers arrive after the stick is done sending.
                for reply in replies:
                    loop.call_later(0.01, self.emit, reply)

    async def factory(self) -> tuple[asyncio.StreamReader, FakeWriter]:
        if not self.plugged:
            raise OSError(2, "No such file or directory")
        if self.opened:
            # A new port after re-plugging (or re-opening).
            self.reader = asyncio.StreamReader()
            self.writer = FakeWriter(self)
            self.closed = False
        self.opened += 1
        return self.reader, self.writer  # type: ignore[return-value]


@pytest.fixture
async def stick() -> FakeStick:
    return FakeStick()


@pytest.fixture
async def cul(stick: FakeStick) -> Cul:
    return Cul("fake", stream_factory=stick.factory, reconnect=False)  # type: ignore[arg-type]
