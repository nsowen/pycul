# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Send queue for MAX! frames.

Modelled on FHEM's CUL_MAX send queue (14_CUL_MAX.pm: CUL_MAX_Send,
CUL_MAX_SQH; (c) 2012 Matthias Gehre, (c) 2019 Wzut, GPL-2.0-or-later):
one frame on air at a time, Ack timeout and retries, waiting for duty cycle
credit, holding frames for shutter contacts until they are awake, and
refreshing time information right before it is sent.

Additions compared to FHEM:
- every frame has a future, so callers can await the outcome;
- frames with the same `key` that have not been sent yet are coalesced: a newer
  set point replaces an older queued one instead of costing another second of
  air time (both callers get the outcome of the newer frame);
- optional expiry, and the stick is chosen per frame (several CULs).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pycul.exceptions import (
    CulConnectionError,
    CulCreditError,
    CulError,
    NakError,
    SendCancelledError,
    SendExpiredError,
)
from pycul.protocols.max.const import MAX_RETRIES, MessageType

if TYPE_CHECKING:
    from pycul.cul import Cul

_LOGGER = logging.getLogger(__name__)

#: Credit (10 ms units) of the 1 s wake-up preamble (culfw rf_moritz.c: moritz_sendraw).
PREAMBLE_CREDIT = 100
#: Longest single wait for credit before re-evaluating the queue.
MAX_CREDIT_WAIT = 60.0
#: Wait before retrying when no stick is connected.
NO_CUL_WAIT = 5.0


def credit_cost(frame_length: int, preamble: bool) -> int:
    """Credit culfw charges for a frame of `frame_length` bytes incl. the length byte.

    Keep in sync with culfw's moritz_sendraw: (preamble ? 100 : 0) + bits / 10.
    """
    return (PREAMBLE_CREDIT if preamble else 0) + frame_length * 8 // 10


@dataclass(eq=False)
class OutgoingMessage:
    """A frame waiting in (or travelling through) the send queue."""

    dst: str
    msg_type: MessageType
    payload: bytes
    src: str
    group: int
    flags: int
    wait_for_ack: bool = True
    #: Shutter contacts only listen right after they transmitted.
    hold_until_released: bool = False
    #: Frames with equal keys replace each other while unsent.
    key: Hashable | None = None
    #: Called before every attempt to recompute the payload (e.g. current time).
    refresh: Callable[[], bytes] | None = None
    expires_at: float | None = None
    future: asyncio.Future[bytes | None] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    created: float = field(default_factory=time.monotonic)
    counter: int | None = None
    attempts: int = 0
    in_flight: bool = False
    released: bool = False

    @property
    def frame_length(self) -> int:
        """Bytes on air incl. the length byte (11 header bytes + payload)."""
        return 11 + len(self.payload)

    def __str__(self) -> str:
        return f"{self.msg_type.name} -> {self.dst} ({self.payload.hex() or '-'})"


type Transmit = Callable[[OutgoingMessage, "Cul", bool], Awaitable[bytes | None]]
type SelectCul = Callable[[OutgoingMessage], "Cul | None"]
type IsAwake = Callable[[str], bool]


class SendQueue:
    """FIFO send queue with one frame in flight."""

    def __init__(self, transmit: Transmit, select_cul: SelectCul, is_awake: IsAwake) -> None:
        self._transmit = transmit
        self._select_cul = select_cul
        self._is_awake = is_awake
        self._queue: list[OutgoingMessage] = []
        self._changed = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._listeners: list[Callable[[], None]] = []
        #: (cul name, seconds) while the queue waits for duty cycle credit.
        self.waiting_for_credit: tuple[str, float] | None = None

    # ----------------------------------------------------------------- public

    @property
    def pending(self) -> list[OutgoingMessage]:
        """Return the queued frames in send order (first may be in flight)."""
        return list(self._queue)

    def __len__(self) -> int:
        return len(self._queue)

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call `listener` whenever the queue content changes."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def submit(self, message: OutgoingMessage, *, before_dst: bool = False) -> OutgoingMessage:
        """Queue a frame and return the queued entry; await its `future` for the Ack.

        If an unsent frame with the same key is queued, it takes over the new
        payload and keeps its position; the returned entry is then that frame.
        before_dst: insert in front of the first queued frame to the same device.
        """
        if message.key is not None:
            for queued in self._queue:
                if queued.key == message.key and not queued.in_flight:
                    _LOGGER.debug("Coalescing %s into queued frame", message)
                    # The type may differ (SetGroupId / RemoveGroupId share a key).
                    queued.msg_type = message.msg_type
                    queued.group = message.group
                    queued.flags = message.flags
                    queued.payload = message.payload
                    queued.refresh = message.refresh
                    queued.expires_at = message.expires_at
                    self._notify()
                    return queued
        index = len(self._queue)
        if before_dst:
            index = next(
                (
                    i
                    for i, queued in enumerate(self._queue)
                    if queued.dst == message.dst and not queued.in_flight
                ),
                index,
            )
        self._queue.insert(index, message)
        self._ensure_worker()
        self._notify()
        return message

    def release(self, address: str) -> None:
        """Mark held frames to `address` as sendable (the device just transmitted)."""
        released = False
        for queued in self._queue:
            if queued.dst == address and queued.hold_until_released:
                queued.released = True
                released = True
        if released:
            self._changed.set()

    def count_for(self, address: str, msg_type: MessageType | None = None) -> int:
        """Return the number of queued frames to `address` (optionally of one type)."""
        return sum(
            1
            for queued in self._queue
            if queued.dst == address and (msg_type is None or queued.msg_type is msg_type)
        )

    def clear(self, address: str | None = None) -> int:
        """Drop unsent frames (to `address`, or all). Returns how many were dropped."""
        dropped = [
            queued
            for queued in self._queue
            if not queued.in_flight and (address is None or queued.dst == address)
        ]
        for queued in dropped:
            self._queue.remove(queued)
            if not queued.future.done():
                queued.future.set_exception(SendCancelledError(f"{queued} was cancelled"))
        if dropped:
            self._notify()
        return len(dropped)

    def poke(self) -> None:
        """Re-evaluate the queue now (e.g. after a stick connected)."""
        self._changed.set()

    async def stop(self) -> None:
        """Stop the worker and cancel all frames."""
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        for queued in self._queue:
            if not queued.future.done():
                queued.future.set_exception(SendCancelledError(f"{queued} was cancelled"))
        self._queue.clear()
        self._notify()

    # ----------------------------------------------------------------- worker

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="pycul-max-queue")
        self._changed.set()

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                _LOGGER.exception("Error in queue listener")

    def _finish(
        self, message: OutgoingMessage, result: bytes | None = None, error: Exception | None = None
    ) -> None:
        with contextlib.suppress(ValueError):
            self._queue.remove(message)
        if not message.future.done():
            if error is not None:
                message.future.set_exception(error)
            else:
                message.future.set_result(result)
        self._notify()

    def _next(self) -> OutgoingMessage | None:
        for queued in self._queue:
            if not queued.hold_until_released or queued.released:
                return queued
        return None

    async def _wait(self, timeout: float | None) -> None:
        # The event is cleared at the start of each round, so changes that
        # happened while the worker awaited something else are not lost.
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                await self._changed.wait()

    def _expire(self) -> float | None:
        """Fail expired frames; return seconds until the next expiry."""
        now = time.monotonic()
        next_expiry: float | None = None
        for queued in list(self._queue):
            if queued.expires_at is None or queued.in_flight:
                continue
            if queued.expires_at <= now:
                self._finish(queued, error=SendExpiredError(f"{queued} expired in the queue"))
            elif next_expiry is None or queued.expires_at - now < next_expiry:
                next_expiry = queued.expires_at - now
        return next_expiry

    async def _run(self) -> None:
        while self._queue:
            self._changed.clear()
            next_expiry = self._expire()
            message = self._next()
            if message is None:
                await self._wait(next_expiry)
                continue

            cul = self._select_cul(message)
            if cul is None:
                await self._wait(NO_CUL_WAIT)
                continue

            preamble = not self._is_awake(message.dst)
            if message.refresh is not None:
                try:
                    message.payload = message.refresh()
                except Exception as err:
                    _LOGGER.exception("Cannot build payload of %s", message)
                    self._finish(message, error=err)
                    continue
            needed = credit_cost(message.frame_length, preamble)
            if cul.credits is None or cul.credits < needed:
                try:
                    credits = await cul.async_get_credits()
                except CulError:
                    await self._wait(NO_CUL_WAIT)
                    continue
                if credits < needed:
                    # culfw regains one credit10ms per second.
                    wait = min(needed - credits + 1, MAX_CREDIT_WAIT)
                    self.waiting_for_credit = (cul.name, wait)
                    _LOGGER.info(
                        "%s: credit %d < %d, %s waits %.0f s (%d frames queued)",
                        cul.name,
                        credits,
                        needed,
                        message,
                        wait,
                        len(self._queue),
                    )
                    self._notify()
                    await self._wait(wait)
                    continue
            self.waiting_for_credit = None

            message.in_flight = True
            try:
                ack = await self._transmit(message, cul, preamble)
            except CulCreditError:
                continue  # credits were refreshed by the failed send; re-evaluate
            except NakError as err:
                self._finish(message, error=err)
                continue
            except CulConnectionError as err:
                # The stick is gone (unplugged): the frame did not go out, so it
                # keeps its retries and waits for this or another stick.
                _LOGGER.debug("%s not sent, %s is disconnected: %s", message, cul.name, err)
                if message.hold_until_released:
                    message.released = False  # its listening window has passed
                await self._wait(NO_CUL_WAIT)
                continue
            except CulError as err:
                message.attempts += 1
                if message.attempts > MAX_RETRIES:
                    _LOGGER.warning("Giving up on %s: %s", message, err)
                    self._finish(message, error=err)
                else:
                    _LOGGER.debug("Retrying %s (%d): %s", message, message.attempts, err)
                    message.released = False  # a shutter contact must wake up again
                continue
            except Exception as err:
                # A bug must not leave the caller waiting forever or stop the worker.
                _LOGGER.exception("Unexpected error sending %s", message)
                self._finish(message, error=err)
                continue
            finally:
                message.in_flight = False
            self._finish(message, ack)
        self.waiting_for_credit = None
