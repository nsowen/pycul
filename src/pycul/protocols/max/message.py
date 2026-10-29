# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""MAX! radio frames as exchanged with culfw ("Z" lines).

Frame layout (all values big endian, see FHEM 14_CUL_MAX.pm, CUL_MAX_Parse,
and culfw clib/rf_moritz.c; GPL, see NOTICE.md):

    len(1) counter(1) flags(1) type(1) src(3) dst(3) group(1) payload(len-10)

culfw prints received frames as "Z" + hex, followed by one RSSI byte when the
RSSI report flag is set (X21).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pycul.cul import rssi_to_dbm
from pycul.protocols.max.const import BROADCAST, MessageType

HEADER_LENGTH = 10  # bytes after the length byte, without payload


class InvalidFrameError(ValueError):
    """A culfw line could not be decoded as a MAX! frame."""


@dataclass(slots=True, frozen=True)
class MaxMessage:
    """A single MAX! radio frame."""

    counter: int
    flags: int
    type_id: int
    src: str
    dst: str
    group: int = 0
    payload: bytes = b""
    rssi: float | None = field(default=None, compare=False)

    @property
    def type(self) -> MessageType:
        """Return the message type (MessageType.UNKNOWN for unknown ids)."""
        return MessageType(self.type_id)

    @property
    def is_broadcast(self) -> bool:
        """Return True if the frame is addressed to everybody."""
        return self.dst == BROADCAST

    @classmethod
    def from_line(cls, line: str) -> MaxMessage:
        """Decode a culfw output line like "Z0F0004601234560000000019001F00D0"."""
        if not line.startswith("Z"):
            raise InvalidFrameError(f"Not a MAX! frame: {line!r}")
        try:
            data = bytes.fromhex(line[1:])
        except ValueError as err:
            raise InvalidFrameError(f"Invalid hex in {line!r}") from err
        if not data:
            raise InvalidFrameError(f"Empty frame: {line!r}")

        length = data[0]
        if length < HEADER_LENGTH:
            raise InvalidFrameError(f"Frame too short: {line!r}")
        rssi: float | None = None
        if len(data) == length + 2:
            rssi = rssi_to_dbm(data[-1])
            data = data[:-1]
        elif len(data) != length + 1:
            # culfw truncates frames >= 30 bytes instead of dropping them.
            raise InvalidFrameError(
                f"Length mismatch in {line!r}: header says {length}, got {len(data) - 1}"
            )

        return cls(
            counter=data[1],
            flags=data[2],
            type_id=data[3],
            src=data[4:7].hex(),
            dst=data[7:10].hex(),
            group=data[10],
            payload=bytes(data[11:]),
            rssi=rssi,
        )

    def to_bytes(self) -> bytes:
        """Encode the frame including the leading length byte."""
        body = (
            bytes([self.counter & 0xFF, self.flags & 0xFF, self.type_id & 0xFF])
            + bytes.fromhex(self.src)
            + bytes.fromhex(self.dst)
            + bytes([self.group & 0xFF])
            + self.payload
        )
        return bytes([len(body)]) + body

    def to_hex(self) -> str:
        """Encode the frame as upper case hex as expected by culfw's Zs/Zf commands."""
        return self.to_bytes().hex().upper()

    def __str__(self) -> str:
        return (
            f"{self.type.name if self.type is not MessageType.UNKNOWN else hex(self.type_id)} "
            f"#{self.counter} {self.src}->{self.dst} grp={self.group} flags={self.flags:02x} "
            f"payload={self.payload.hex() or '-'}"
            + (f" rssi={self.rssi}" if self.rssi is not None else "")
        )
