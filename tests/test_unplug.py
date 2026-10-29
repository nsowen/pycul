# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for unplugging and re-plugging a stick."""

from __future__ import annotations

import asyncio

import pytest

from pycul import Cul, CulConnectionError, CulTimeoutError
from pycul import cul as cul_module
from pycul.protocols.max import DeviceType, MaxDevice, MaxProtocol, MessageType
from tests.conftest import FakeStick
from tests.test_max_protocol import HT, ME, ack_responder, sent_messages


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    # Long back-off: reconnecting quickly must come from request_reconnect().
    monkeypatch.setattr(cul_module, "RECONNECT_MIN_DELAY", 30.0)
    monkeypatch.setattr(cul_module, "COMMAND_TIMEOUT", 0.05)
    monkeypatch.setattr(cul_module, "SEND_TIMEOUT", 0.05)


async def wait_for(condition, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


@pytest.fixture
async def setup(stick: FakeStick, no_backoff: None):
    cul = Cul("fake", stream_factory=stick.factory)  # type: ignore[arg-type]
    proto = MaxProtocol(ME, time_sync_interval=None)
    cul.add_protocol(proto)
    proto.add_device(MaxDevice(HT, DeviceType.HEATING_THERMOSTAT))
    states: list[bool] = []
    cul.add_connection_listener(states.append)
    await cul.connect()
    yield cul, proto, states
    await cul.close()


async def test_unplug_and_replug(setup, stick: FakeStick) -> None:  # type: ignore[no-untyped-def]
    cul, _proto, states = setup
    stick.unplug()
    await wait_for(lambda: not cul.connected)
    assert states == [True, False]

    cul.request_reconnect()  # still unplugged: fails, keeps trying
    await asyncio.sleep(0.05)
    assert not cul.connected

    stick.plugged = True
    stick.commands.clear()
    cul.request_reconnect()  # e.g. Home Assistant saw the USB device again
    await wait_for(lambda: cul.connected)
    assert states == [True, False, True]
    # Moritz mode and the auto-ack address are set up again on the new port.
    assert {"Zr", f"Za{ME}"} <= set(stick.commands)


async def test_write_error_is_a_lost_connection(setup, stick: FakeStick) -> None:  # type: ignore[no-untyped-def]
    cul, _proto, states = setup
    stick.plugged = False  # unplugged, EOF not seen yet
    with pytest.raises(CulConnectionError):
        await cul.async_get_credits()
    await wait_for(lambda: states == [True, False])


async def test_hung_stick_is_reconnected(setup, stick: FakeStick) -> None:  # type: ignore[no-untyped-def]
    cul, _proto, states = setup
    stick.hung = True
    for _ in range(cul_module.MAX_SILENT_TIMEOUTS):
        with pytest.raises(CulTimeoutError):
            await cul.async_get_credits()
    await wait_for(lambda: states == [True, False])
    assert stick.closed  # the port was closed to be opened again

    stick.hung = False
    cul.request_reconnect()
    await wait_for(lambda: cul.connected)


async def test_frame_waits_for_replug_without_using_retries(setup, stick: FakeStick) -> None:  # type: ignore[no-untyped-def]
    cul, proto, _states = setup
    ack_responder(stick)
    stick.unplug()
    await wait_for(lambda: not cul.connected)
    task = asyncio.create_task(proto.set_auto(HT))
    await asyncio.sleep(0.1)
    assert not task.done()
    [queued] = proto.queue.pending
    assert queued.attempts == 0

    stick.plugged = True
    cul.request_reconnect()
    await asyncio.wait_for(task, 2)
    assert [m.type for m in sent_messages(stick)] == [MessageType.SET_TEMPERATURE]
