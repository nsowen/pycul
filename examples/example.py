# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""pycul example and hardware test for eQ-3 MAX! thermostats.

Walks through the library API step by step against a real CUL stick:

  1. connect to the CUL and read firmware version / send credit
  2. listen to the MAX! traffic and show every device that talks to us
  3. for each given thermostat: send the time, set a test temperature,
     check the device acknowledged it, then restore the previous mode

Usage:
    python examples/example.py --device /dev/ttyACM0 --address 123456
    python examples/example.py -d /dev/ttyACM0 -t 0a1b2c -t 0d1e2f --test-temperature 22
    python examples/example.py -d socket://192.168.1.10:2323 --listen 300

The address must be the one your thermostats are paired with (FHEM's
CUL_MAX default is 123456), otherwise they ignore our commands.
Each command costs ~1.1 s of the 1% duty cycle (credit10ms ~110 of 900).
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from pycul import Cul, CulError
from pycul.protocols.max import (
    DeviceType,
    MaxDevice,
    MaxMessage,
    MaxProtocol,
    Mode,
)

#: Credit needed per thermostat in step 3 (three sends with long preamble).
CREDIT_PER_THERMOSTAT = 3 * 110


def _mode(mode: Mode | None) -> str:
    return mode.name.lower() if mode is not None else "unknown"


def on_message(message: MaxMessage) -> None:
    """Called for every MAX! frame the CUL receives, including foreign traffic."""
    print(f"  RX     {message}")


def on_state(device: MaxDevice) -> None:
    """Called whenever a device's state changes (status messages, Acks, commands)."""
    s = device.state
    print(
        f"  STATE  {device.address} {device.device_type.name}: mode={_mode(s.mode)} "
        f"target={s.target_temperature} current={s.current_temperature} "
        f"valve={s.valve_position} battery_low={s.battery_low} rssi={s.rssi}"
    )


def on_device_added(device: MaxDevice) -> None:
    """Called for devices that pair with us or are already paired and talk to us."""
    print(f"  NEW    {device.address} {device.device_type.name} serial={device.serial}")
    device.add_listener(on_state)


async def test_thermostat(max_proto: MaxProtocol, address: str, test_temperature: float) -> bool:
    """Send time, a test temperature and restore the previous mode. Returns True on success."""
    device = max_proto.devices[address]
    before_mode = device.state.mode
    before_temperature = device.state.target_temperature
    print(
        f"\n== Thermostat {address}: before mode={_mode(before_mode)} target={before_temperature}"
    )
    try:
        print("  -> send time information")
        await max_proto.send_time_information(address)
        print("  OK   time acknowledged")

        print(f"  -> set {test_temperature} °C (manual)")
        await max_proto.set_temperature(address, test_temperature, mode=Mode.MANUAL)
        acknowledged = device.state.target_temperature
        print(f"  OK   acknowledged, device reports target={acknowledged}")
        if acknowledged != test_temperature:
            print("  WARN device reports a different target temperature")
        await asyncio.sleep(2)

        print("  -> restore previous mode")
        if before_mode is Mode.MANUAL and before_temperature is not None:
            await max_proto.set_temperature(address, before_temperature)
        else:
            # Unknown or AUTO before: back to the week profile.
            await max_proto.set_auto(address)
        print(
            f"  OK   restored, mode={_mode(device.state.mode)} "
            f"target={device.state.target_temperature}"
        )
    except CulError as err:
        print(f"  FAIL {type(err).__name__}: {err}")
        return False
    return True


async def main(args: argparse.Namespace) -> int:
    # --- 1. connect -----------------------------------------------------------
    # A Cul owns the serial connection; protocols are attached before connecting.
    cul = Cul(args.device, args.baudrate)
    max_proto = MaxProtocol(args.address, time_sync_interval=None)
    cul.add_protocol(max_proto)

    max_proto.add_device_added_listener(on_device_added)
    if args.verbose:
        max_proto.add_message_listener(on_message)
    # Thermostats we want to test are registered up front, so state reports are
    # tracked even before they talk to us.
    for address in args.thermostat:
        max_proto.add_device(MaxDevice(address.lower(), DeviceType.HEATING_THERMOSTAT))

    print(f"== Connecting to {args.device}")
    await cul.connect()
    assert cul.version is not None
    print(f"  firmware: {cul.version.raw}")
    print(f"  credit10ms: {cul.credits} (max 900, +1 per second)")

    try:
        # --- 2. listen --------------------------------------------------------
        print(f"\n== Listening for {args.listen:.0f} s (thermostats report on changes)")
        await asyncio.sleep(args.listen)
        print(f"  known devices: {', '.join(max_proto.devices) or 'none'}")

        # --- 3. commands ------------------------------------------------------
        results: dict[str, bool] = {}
        for address in args.thermostat:
            credits = await cul.async_get_credits()
            if credits < CREDIT_PER_THERMOSTAT:
                wait = CREDIT_PER_THERMOSTAT - credits
                print(f"\n  credit10ms={credits}, waiting {wait} s for the duty cycle budget ...")
                await asyncio.sleep(wait)
            results[address.lower()] = await test_thermostat(
                max_proto, address.lower(), args.test_temperature
            )

        print("\n== Summary")
        print(f"  connection: OK ({cul.version.version}, credit10ms={cul.credits})")
        print(f"  devices heard: {len(max_proto.devices)}")
        for address, ok in results.items():
            print(f"  thermostat {address}: {'OK' if ok else 'FAILED'}")
        return 0 if all(results.values()) else 1
    finally:
        await cul.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="pycul MAX! hardware test")
    parser.add_argument(
        "-d", "--device", required=True, help="e.g. /dev/ttyACM0 or socket://host:port"
    )
    parser.add_argument("-b", "--baudrate", type=int, default=38400)
    parser.add_argument(
        "-a", "--address", default="123456", help="controller address (default 123456)"
    )
    parser.add_argument(
        "-t",
        "--thermostat",
        action="append",
        default=[],
        help="radiator thermostat address to test",
    )
    parser.add_argument(
        "--listen", type=float, default=60, help="seconds to listen first (default 60)"
    )
    parser.add_argument("--test-temperature", type=float, default=21.0)
    parser.add_argument("-v", "--verbose", action="store_true", help="print every radio frame")
    parser.add_argument("--debug", action="store_true", help="debug logging incl. serial lines")
    arguments = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if arguments.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        raise SystemExit(asyncio.run(main(arguments)))
    except KeyboardInterrupt:
        pass
