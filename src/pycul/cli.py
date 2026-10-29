# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Command line tool to test a CUL stick and MAX! devices.

Examples:
    pycul -d /dev/ttyACM0 info
    pycul -d /dev/ttyACM0 monitor
    pycul -d /dev/ttyACM0 topology --seconds 3600
    pycul -d /dev/ttyACM0 -a 123456 set-temp 0a1b2c 21.5
    pycul -d /dev/ttyACM0 profile 0a1b2c --day monday "06:00-08:30=21,17:00-22:00=21" --base 17
    pycul -d /dev/ttyACM0 -d socket://192.168.1.10:2323 pair --seconds 120
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TextIO

from pycul import Cul, CulError
from pycul.protocols.max import (
    DEFAULT_ADDRESS,
    DEFAULT_FAKE_SC_ADDRESS,
    DEFAULT_FAKE_WT_ADDRESS,
    DayProfile,
    DeviceType,
    MaxDevice,
    MaxMessage,
    MaxProtocol,
    Mode,
    TemperatureConfig,
    ValveConfig,
    WeekProfile,
)
from pycul.protocols.max.protocol import TYPE_BY_MESSAGE
from pycul.protocols.max.weekprofile import WEEKDAY_NAMES

#: Seconds to keep listening after a command, to show the device's reaction.
FOLLOW_UP = 3.0


def _print_device(device: MaxDevice) -> None:
    state = device.state
    fields = {
        "mode": state.mode.name.lower() if state.mode is not None else None,
        "target": state.target_temperature,
        "current": state.current_temperature,
        "valve": state.valve_position,
        "until": state.until,
        "window": state.window,
        "battery_low": state.battery_low,
        "rf_error": state.rf_error,
        "locked": state.panel_locked,
        "rssi": state.rssi,
    }
    text = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    print(f"{_now()}  STATE {device.address} ({device.device_type.name}): {text}")


def _print_message(message: MaxMessage) -> None:
    print(f"{_now()}  RX    {message}")


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _parse_until(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError as err:
        raise argparse.ArgumentTypeError("expected 'YYYY-MM-DD HH:MM'") from err


def _parse_day(value: str, base: float) -> DayProfile:
    """Parse "06:00-08:30=21,17:00-22:00=21.5" (empty: base temperature all day)."""
    periods = []
    for part in filter(None, (p.strip() for p in value.split(","))):
        times, _, temperature = part.partition("=")
        start, _, end = times.partition("-")
        periods.append((start, end, float(temperature)))
    return DayProfile.from_periods(periods, base)


def _learn_topology(max_proto: MaxProtocol) -> None:
    """Register every device seen talking, and print what is learned about it."""
    known: dict[str, tuple[int, frozenset[str]]] = {}

    def learned(device: MaxDevice) -> None:
        current = (device.group, frozenset(device.link_partners))
        previous = known.get(device.address, (0, frozenset()))
        known[device.address] = current
        if current[0] != previous[0]:
            print(f"{_now()}  LEARN {device.address} is in group {device.group}")
        for partner in sorted(current[1] - previous[1]):
            print(f"{_now()}  LEARN {device.address} is linked to {partner}")

    def register(message: MaxMessage) -> None:
        # Runs before the protocol looks the sender up, so it learns from this frame.
        device_type = TYPE_BY_MESSAGE.get(message.type)
        if device_type and message.src not in max_proto.devices:
            max_proto.add_device(MaxDevice(message.src, device_type))

    max_proto.add_message_listener(register)
    max_proto.add_device_added_listener(lambda device: device.add_listener(learned))


def _topology_report(max_proto: MaxProtocol, as_json: bool, out: TextIO | None = None) -> None:
    devices = max_proto.devices
    groups: dict[int, list[str]] = {}
    for address, device in sorted(devices.items()):
        if device.group:
            groups.setdefault(device.group, []).append(address)
    clusters = max_proto.link_clusters()
    if as_json:
        print(
            json.dumps(
                {
                    "devices": {
                        address: {
                            "type": device.device_type.name.lower(),
                            "group": device.group,
                            "link_partners": sorted(device.link_partners),
                        }
                        for address, device in sorted(devices.items())
                    },
                    "groups": groups,
                    "linked": [sorted(cluster) for cluster in clusters],
                },
                indent=2,
            ),
            file=out,
        )
        return

    def label(address: str) -> str:
        device = devices.get(address)
        return f"{address} ({device.device_type.name.lower()})" if device else address

    print("\nGroups seen on air:")
    for group, members in sorted(groups.items()):
        print(f"  {group:3d}: {', '.join(label(a) for a in members)}")
    if not groups:
        print("  none (devices without group id, or no frames between devices heard)")
    print("Links (sender -> link partners):")
    for address, device in sorted(devices.items()):
        if device.link_partners:
            print(f"  {label(address)} -> {', '.join(sorted(device.link_partners))}")
    print("Devices linked with each other (probably one room each):")
    for cluster in clusters:
        print(f"  {', '.join(label(a) for a in sorted(cluster))}")
    alone = sorted(a for a, d in devices.items() if not d.group and not d.link_partners)
    alone = [a for a in alone if not any(a in c for c in clusters)]
    if alone:
        print(f"Nothing learned yet: {', '.join(label(a) for a in alone)}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pycul", description=__doc__.splitlines()[0])
    parser.add_argument(
        "-d",
        "--device",
        action="append",
        help="serial device or pyserial URL; repeat for several sticks (env PYCUL_DEVICE, "
        "comma separated)",
    )
    parser.add_argument("-b", "--baudrate", type=int, default=38400)
    parser.add_argument(
        "-a",
        "--address",
        default=os.environ.get("PYCUL_MAX_ADDRESS", DEFAULT_ADDRESS),
        help="MAX! controller address of the CUL (env PYCUL_MAX_ADDRESS, default %(default)s)",
    )
    parser.add_argument("--fake-wt", default=DEFAULT_FAKE_WT_ADDRESS, help="fake WT address")
    parser.add_argument("--fake-sc", default=DEFAULT_FAKE_SC_ADDRESS, help="fake SC address")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="-v info, -vv debug")
    parser.add_argument(
        "--wall", action="store_true", help="target device is a wall thermostat (default: radiator)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="show firmware version and send credit")
    monitor = sub.add_parser("monitor", help="print all received MAX! frames and device states")
    monitor.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = ^C)")
    topology = sub.add_parser(
        "topology",
        help="listen and report groups and links the devices use among each other",
        description="Devices only reveal their group id and link partners in frames to "
        "each other (a wall thermostat to its radiator thermostats, a window contact "
        "reporting a change). Listen long enough for those: an hour, or turn a wall "
        "thermostat's wheel and open a window. Nothing is sent.",
    )
    topology.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = ^C)")
    topology.add_argument("--json", action="store_true", help="print the result as JSON")
    raw = sub.add_parser("raw", help="send a raw culfw command and print the output")
    raw.add_argument("raw_command")

    set_temp = sub.add_parser("set-temp", help="set target temperature (manual mode)")
    set_temp.add_argument("target")
    set_temp.add_argument("temperature", type=float)
    set_temp.add_argument("--until", type=_parse_until, help="temporary until 'YYYY-MM-DD HH:MM'")
    set_temp.add_argument("--auto", action="store_true", help="keep auto mode until next switch")
    for name, help_text in (
        ("auto", "switch to automatic (week profile) mode"),
        ("boost", "start boost"),
        ("time", "send the current time"),
        ("wakeup", "keep the device awake for ~30 s"),
        ("reset", "factory reset (device forgets pairing and links)"),
    ):
        sub.add_parser(name, help=help_text).add_argument("target")
    display = sub.add_parser("display", help="wall thermostat: show actual or target temperature")
    display.add_argument("target")
    display.add_argument("what", choices=["actual", "target"])

    temps = sub.add_parser("config-temps", help="write the temperature configuration")
    temps.add_argument("target")
    defaults = TemperatureConfig()
    temps.add_argument("--comfort", type=float, default=defaults.comfort)
    temps.add_argument("--eco", type=float, default=defaults.eco)
    temps.add_argument("--maximum", type=float, default=defaults.maximum)
    temps.add_argument("--minimum", type=float, default=defaults.minimum)
    temps.add_argument("--offset", type=float, default=defaults.measurement_offset)
    temps.add_argument("--window-temp", type=float, default=defaults.window_open_temperature)
    temps.add_argument("--window-minutes", type=int, default=defaults.window_open_duration)

    valve = sub.add_parser("config-valve", help="write the valve configuration (radiator only)")
    valve.add_argument("target")
    vdefaults = ValveConfig()
    valve.add_argument("--boost-minutes", type=int, default=vdefaults.boost_duration)
    valve.add_argument("--boost-valve", type=int, default=vdefaults.boost_valve_position)
    valve.add_argument("--decalc-day", default=vdefaults.decalcification_day)
    valve.add_argument("--decalc-hour", type=int, default=vdefaults.decalcification_hour)
    valve.add_argument("--max-valve", type=int, default=vdefaults.max_valve_setting)
    valve.add_argument("--valve-offset", type=int, default=vdefaults.valve_offset)

    for name, help_text in (
        ("associate", "link a partner to the target device"),
        ("deassociate", "remove a link partner from the target device"),
    ):
        link = sub.add_parser(name, help=help_text)
        link.add_argument("target")
        link.add_argument("partner", help="partner address, 'fake-wt' or 'fake-sc'")
        link.add_argument(
            "--partner-type",
            choices=[t.name.lower() for t in DeviceType if t is not DeviceType.UNKNOWN],
            default="heating_thermostat",
            help="type of a real partner device (default: %(default)s)",
        )
    fake_wt = sub.add_parser(
        "fake-wt", help="send set point + measured temp as fake wall thermostat"
    )
    fake_wt.add_argument("target")
    fake_wt.add_argument("temperature", type=float, help="set point")
    fake_wt.add_argument("measured", type=float, help="measured room temperature")
    fake_sc = sub.add_parser("fake-sc", help="report a window state as fake shutter contact")
    fake_sc.add_argument("target")
    fake_sc.add_argument("window", choices=["open", "closed"])

    group = sub.add_parser("group", help="set the group (room) id of a device, 0 removes it")
    group.add_argument("target")
    group.add_argument("group_id", type=int)
    group_temp = sub.add_parser("group-temp", help="set the temperature of all group members")
    group_temp.add_argument("group_id", type=int)
    group_temp.add_argument("temperature", type=float)
    group_temp.add_argument("members", nargs="+", help="addresses of the group members")

    profile = sub.add_parser("profile", help="write a week profile (or one day of it)")
    profile.add_argument("target")
    source = profile.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--day",
        nargs=2,
        metavar=("WEEKDAY", "PERIODS"),
        help='e.g. monday "06:00-08:30=21,17:00-22:00=21"; outside periods: --base',
    )
    source.add_argument("--file", type=Path, help="JSON as produced by WeekProfile.to_dict()")
    source.add_argument("--factory", action="store_true", help="the factory default profile")
    profile.add_argument("--base", type=float, default=17.0, help="temperature between periods")

    pair = sub.add_parser("pair", help="accept new devices (press the pair button on the device)")
    pair.add_argument("--seconds", type=float, default=60)
    return parser


async def _run(args: argparse.Namespace) -> int:
    # Listening commands survive a re-plugged stick; one-shot commands fail fast.
    listening = args.command in ("monitor", "topology")
    culs = [Cul(url, args.baudrate, reconnect=listening) for url in args.device]
    max_proto = MaxProtocol(
        args.address,
        time_sync_interval=None,
        fake_wt_address=args.fake_wt,
        fake_sc_address=args.fake_sc,
    )
    for cul in culs:
        cul.add_protocol(max_proto)
    if args.command == "topology":
        if args.json:
            sys.stdout = sys.stderr  # progress to stderr, only the JSON to stdout
        _learn_topology(max_proto)

    target: str | None = getattr(args, "target", None)
    if target:
        device_type = DeviceType.WALL_THERMOSTAT if args.wall else DeviceType.HEATING_THERMOSTAT
        target = target.lower()
        max_proto.add_device(MaxDevice(target, device_type))
    for member in getattr(args, "members", None) or []:
        max_proto.add_device(
            MaxDevice(member.lower(), DeviceType.HEATING_THERMOSTAT, group=args.group_id)
        )

    if args.command == "topology":
        # Only what is learned, not every frame.
        max_proto.add_device_added_listener(
            lambda device: print(f"{_now()}  SEEN  {device.address} {device.device_type.name}")
        )
    else:
        max_proto.add_message_listener(_print_message)
        max_proto.add_device_added_listener(
            lambda device: (
                print(
                    f"{_now()}  NEW   {device.address} {device.device_type.name} {device.serial}"
                ),
                device.add_listener(_print_device),
            )
        )
        for device in max_proto.devices.values():
            device.add_listener(_print_device)

    for cul in culs:
        await cul.connect()
        assert cul.version is not None
        print(f"Connected {cul.name}: {cul.version.raw} (credit10ms={cul.credits})")
    print(f"MAX! controller address {args.address}")

    def credits() -> str:
        return ", ".join(f"{c.name}={c.credits}" for c in culs)

    max_proto.queue.add_listener(
        lambda: (
            max_proto.queue.waiting_for_credit
            and print(
                f"{_now()}  WAIT  {len(max_proto.queue)} queued, waiting "
                f"{max_proto.queue.waiting_for_credit[1]:.0f} s for credit on "
                f"{max_proto.queue.waiting_for_credit[0]}"
            )
        )
    )

    async def command(name: str, action: Callable[[], Awaitable[object]]) -> None:
        print(f"{_now()}  TX    {name} -> {target or 'group'}")
        await action()
        print(f"{_now()}  OK    {name} acknowledged (credit10ms: {credits()})")
        await asyncio.sleep(FOLLOW_UP)

    try:
        match args.command:
            case "info":
                for cul in culs:
                    await cul.async_get_credits()
                print(f"credit10ms: {credits()} (1% duty cycle, max 900, +1/s)")
            case "monitor":
                print("Listening, press Ctrl+C to stop ...")
                await asyncio.sleep(args.seconds or float("inf"))
            case "topology":
                print("Listening for frames between devices, press Ctrl+C to stop ...")
                try:
                    await asyncio.sleep(args.seconds or float("inf"))
                finally:
                    # With --json, stdout was redirected to stderr for the progress lines.
                    _topology_report(max_proto, args.json, sys.__stdout__ if args.json else None)
            case "raw":
                for cul in culs:
                    cul.add_raw_listener(
                        lambda line, name=cul.name: print(f"{_now()}  <-    {name}: {line}")
                    )
                culs[0].write_raw(args.raw_command)
                await asyncio.sleep(FOLLOW_UP)
            case "set-temp":
                mode = Mode.AUTO if args.auto else Mode.MANUAL
                await command(
                    f"set-temp {args.temperature}",
                    lambda: max_proto.set_temperature(
                        target, args.temperature, mode=mode, until=args.until
                    ),
                )
            case "auto":
                await command("auto", lambda: max_proto.set_auto(target))
            case "boost":
                await command("boost", lambda: max_proto.set_boost(target))
            case "time":
                await command("time", lambda: max_proto.send_time_information(target))
            case "display":
                await command(
                    f"display {args.what}",
                    lambda: max_proto.set_display_actual_temperature(target, args.what == "actual"),
                )
            case "config-temps":
                config = TemperatureConfig(
                    comfort=args.comfort,
                    eco=args.eco,
                    maximum=args.maximum,
                    minimum=args.minimum,
                    measurement_offset=args.offset,
                    window_open_temperature=args.window_temp,
                    window_open_duration=args.window_minutes,
                )
                await command(
                    "config-temps", lambda: max_proto.configure_temperatures(target, config)
                )
            case "config-valve":
                vconfig = ValveConfig(
                    boost_duration=args.boost_minutes,
                    boost_valve_position=args.boost_valve,
                    decalcification_day=args.decalc_day,
                    decalcification_hour=args.decalc_hour,
                    max_valve_setting=args.max_valve,
                    valve_offset=args.valve_offset,
                )
                await command("config-valve", lambda: max_proto.configure_valve(target, vconfig))
            case "wakeup":
                await command("wakeup", lambda: max_proto.wake_up(target))
            case "reset":
                await command("reset", lambda: max_proto.factory_reset(target))
            case "associate" | "deassociate":
                add = args.command == "associate"
                if args.partner == "fake-wt":
                    action = lambda: max_proto.associate_fake_wall_thermostat(target, add)  # noqa: E731
                elif args.partner == "fake-sc":
                    action = lambda: max_proto.associate_fake_shutter_contact(target, add)  # noqa: E731
                else:
                    partner_type = DeviceType[args.partner_type.upper()]
                    method = max_proto.add_link_partner if add else max_proto.remove_link_partner
                    action = lambda: method(target, args.partner, partner_type)  # noqa: E731
                await command(f"{args.command} {args.partner}", action)
            case "fake-wt":
                await command(
                    f"fake-wt {args.temperature}/{args.measured}",
                    lambda: max_proto.send_fake_wall_thermostat(
                        target, args.temperature, args.measured
                    ),
                )
            case "fake-sc":
                await command(
                    f"fake-sc {args.window}",
                    lambda: max_proto.send_fake_shutter_contact(target, args.window == "open"),
                )
            case "group":
                await command(
                    f"group {args.group_id}", lambda: max_proto.set_group(target, args.group_id)
                )
            case "group-temp":
                # One frame with the group id; only the addressed member acknowledges.
                await command(
                    f"group-temp {args.group_id} {args.temperature}",
                    lambda: max_proto.set_group_temperature(args.group_id, args.temperature),
                )
            case "profile":
                if args.day:
                    weekday = WEEKDAY_NAMES.index(args.day[0].lower())
                    day = _parse_day(args.day[1], args.base)
                    print(f"{args.day[0]}: {day.to_dict()}")
                    await command(
                        f"profile {args.day[0]}",
                        lambda: max_proto.set_day_profile(target, weekday, day),
                    )
                else:
                    week = (
                        WeekProfile.factory_default()
                        if args.factory
                        else WeekProfile.from_dict(json.loads(args.file.read_text()))
                    )
                    await command("profile", lambda: max_proto.set_week_profile(target, week))
            case "pair":
                max_proto.start_pairing(args.seconds)
                print(f"Pairing for {args.seconds:.0f} s - press the pair button on the device ...")
                await asyncio.sleep(args.seconds + FOLLOW_UP)
    except CulError as err:
        print(f"{_now()}  ERROR {type(err).__name__}: {err}", file=sys.stderr)
        return 1
    finally:
        for cul in culs:
            await cul.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point of the `pycul` command."""
    args = _build_parser().parse_args(argv)
    if not args.device and os.environ.get("PYCUL_DEVICE"):
        args.device = os.environ["PYCUL_DEVICE"].split(",")
    if not args.device:
        print("No device given, use -d or PYCUL_DEVICE", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=(logging.WARNING, logging.INFO, logging.DEBUG)[min(args.verbose, 2)],
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0
    except CulError as err:
        print(f"ERROR {type(err).__name__}: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
