# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for the CUL connection."""

from __future__ import annotations

import asyncio

import pytest

from pycul import Cul, CulCreditError, CulTimeoutError, rssi_to_dbm
from pycul import cul as cul_module
from tests.conftest import FakeStick

ACK = "Z0B01000212345600ABCD0001"


async def test_connect_reads_version_and_credits(cul: Cul, stick: FakeStick) -> None:
    states: list[bool] = []
    cul.add_connection_listener(states.append)
    await cul.connect()
    assert cul.connected
    assert cul.version is not None
    assert cul.version.version == "1.67"
    assert cul.credits == 900
    assert states == [True]
    await cul.close()
    assert states == [True, False]


async def test_a_culfw_version(cul: Cul, stick: FakeStick) -> None:
    stick.version = "V 1.26.08 a-culfw Build: 197 (2018-12-01_10-21-17) CUL868 (F-Band: 868MHz)"
    await cul.connect()
    assert cul.version is not None
    assert cul.version.is_a_culfw
    assert cul.version.version == "1.26.08"
    await cul.close()


async def test_send_updates_credits(cul: Cul, stick: FakeStick) -> None:
    await cul.connect()
    await cul.async_send("Zs" + ACK[1:])
    assert cul.credits is not None
    assert cul.credits < 900
    await cul.close()


async def test_send_without_credit_raises(cul: Cul, stick: FakeStick) -> None:
    await cul.connect()
    stick.credits = 5
    with pytest.raises(CulCreditError):
        await cul.async_send("Zs" + ACK[1:])
    await cul.close()


async def test_query_timeout(cul: Cul, stick: FakeStick) -> None:
    await cul.connect()
    with pytest.raises(CulTimeoutError):
        await cul.async_query("t", r"^[0-9A-F]{8}$", timeout=0.05)
    await cul.close()


async def test_raw_listener(cul: Cul, stick: FakeStick) -> None:
    lines: list[str] = []
    cul.add_raw_listener(lines.append)
    await cul.connect()
    stick.emit(ACK)
    await cul.async_get_credits()
    assert ACK in lines
    await cul.close()


def test_rssi_conversion() -> None:
    assert rssi_to_dbm(0) == -74
    assert rssi_to_dbm(0x80) == -138
    assert rssi_to_dbm(0xFF) == -74.5


async def test_reconnect_survives_a_stick_that_does_not_answer(
    stick: FakeStick, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cul_module, "RECONNECT_MIN_DELAY", 0.01)
    monkeypatch.setattr(cul_module, "COMMAND_TIMEOUT", 0.05)
    attempts = 0
    silent = FakeStick()
    # Answers the version, but not "X": connect() raises CulTimeoutError.
    silent.on_command = lambda c: silent.emit(silent.version) if c == "V" else None  # type: ignore[method-assign]

    async def factory():  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        # First a stick that times out (CulTimeoutError), then a working one.
        if attempts == 2:
            return await silent.factory()
        stick.reader = asyncio.StreamReader()
        return await stick.factory()

    cul = Cul("fake", stream_factory=factory)
    await cul.connect()
    stick.reader.feed_eof()
    for _ in range(100):
        await asyncio.sleep(0.02)
        if cul.connected and attempts >= 3:
            break
    assert cul.connected
    await cul.close()
