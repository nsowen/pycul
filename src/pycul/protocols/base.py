# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Base class for radio protocols that run on top of one or more CULs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pycul.cul import Cul


class CulProtocol(ABC):
    """A radio protocol handled by culfw (e.g. MAX!, FS20, HomeMatic).

    culfw drives a single CC1101 transceiver per stick, which can only be in
    one RF mode at a time (e.g. "moritz" for MAX!, "slowrf" for FS20/FHT/HMS,
    "asksin" for HomeMatic). Protocols that share an RF mode (FS20 + FHT +
    HMS) can be attached to the same CUL; protocols with different RF modes
    need separate sticks.

    One protocol instance may be attached to several CULs to cover a larger
    area with one radio network (FHEM: IOgrp). Every callback therefore gets
    the CUL it concerns.
    """

    #: Short, unique protocol name (e.g. "max").
    name: str
    #: culfw RF mode this protocol needs.
    rf_mode: str
    #: First characters of the culfw output lines this protocol consumes.
    line_prefixes: tuple[str, ...]

    def __init__(self) -> None:
        self._culs: list[Cul] = []

    @property
    def culs(self) -> list[Cul]:
        """Return all CULs this protocol is attached to."""
        return list(self._culs)

    @property
    def connected_culs(self) -> list[Cul]:
        """Return the attached CULs that are currently connected."""
        return [cul for cul in self._culs if cul.connected]

    def attach(self, cul: Cul) -> None:
        """Attach the protocol to a CUL. Called by Cul.add_protocol()."""
        if cul not in self._culs:
            self._culs.append(cul)

    @abstractmethod
    def init_commands(self, cul: Cul) -> list[str]:
        """Return the culfw commands that switch `cul` into this protocol's mode.

        They are sent after every (re)connect, when the firmware version is known.
        """

    def handles(self, line: str) -> bool:
        """Return True if the given culfw output line belongs to this protocol."""
        return line.startswith(self.line_prefixes)

    @abstractmethod
    def handle_line(self, line: str, cul: Cul) -> None:
        """Process one line received from `cul`."""

    async def async_connected(self, cul: Cul) -> None:  # noqa: B027
        """Run after `cul` has been (re)connected and initialised."""

    async def async_cul_closed(self, cul: Cul) -> None:  # noqa: B027
        """Run when `cul` is closed on purpose (not on connection loss)."""
