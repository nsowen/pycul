# pycul

Asyncio Python library for busware CUL sticks (and compatible hardware such as
nanoCUL, CUNO, CUN) running [culfw](http://culfw.de) or
[a-culfw](https://github.com/heliflieger/a-culfw).

The first supported radio protocol is eQ-3 MAX! (culfw "Moritz" mode): the CUL
acts as the controller of your thermostats, the way a MAX! Cube or FHEM's
`CUL_MAX` does. The library is built so further culfw protocols (FS20, FHT,
HomeMatic, ...) can be added as separate protocol handlers.

pycul is the backend of the [hacul](https://github.com/nsowen/hacul) Home Assistant integration but
has no Home Assistant dependency.

Status: alpha. The protocol details come from FHEM's long-standing
implementation.

## Why

Our office heats with ten MAX! radiator thermostats and their wall
thermostats, run by FHEM with a CUL on a Raspberry Pi. When the Pi crashed,
it was time to move to Home Assistant, but not to replace working hardware.
pycul is the radio side of that move: a standalone library for the CUL, used
by the [hacul](https://github.com/nsowen/hacul) integration, so the existing thermostats keep working
with their pairing.

## Requirements

- Python 3.12 or newer
- A CUL with culfw 1.53 or newer, or any a-culfw build. Older firmware works for
  basic operation but has known MAX! bugs and no fake wall thermostat support.
- 868 MHz hardware. The stick can be connected via USB or over the network (ser2net, CUNO).

## Installation

pycul is not on PyPI yet. Install from a checkout:

```bash
pip install -e '.[test]'
```

## Quick start

```python
import asyncio

from pycul import Cul
from pycul.protocols.max import MaxProtocol


async def main() -> None:
    cul = Cul("/dev/serial/by-id/usb-busware.de_CUL868-if00")
    max_proto = MaxProtocol(address="123456")
    cul.add_protocol(max_proto)

    def on_state(device):
        s = device.state
        print(device.address, s.mode, s.target_temperature, s.current_temperature, s.valve_position)

    max_proto.add_device_added_listener(lambda device: device.add_listener(on_state))

    await cul.connect()
    print(cul.version.raw, "credit:", cul.credits)

    await asyncio.sleep(120)  # watch traffic for a while
    await max_proto.set_temperature("0a1b2c", 21.5)
    await max_proto.set_auto("0a1b2c")
    await cul.close()


asyncio.run(main())
```

The `url` argument of `Cul` accepts a device path or any
[pyserial URL](https://pyserial.readthedocs.io/en/latest/url_handlers.html),
for example `socket://192.168.1.10:2323` for a stick behind ser2net.

## The controller address

MAX! devices only take commands from the controller they are paired with. The
`address` you pass to `MaxProtocol` is the controller address of your CUL.

- **Migrating from FHEM:** use the address of your `CUL_MAX` device (FHEM's
  default is `123456`). Devices paired with FHEM then keep working without
  re-pairing. pycul adopts every device that sends messages to that address.
- **Devices paired with a MAX! Cube:** factory-reset the device, then pair it
  with pycul (`start_pairing()` or `pycul pair`, then press the device's pair button).

Only one controller (FHEM, a Cube, or pycul) should be active per address at a time.

## Duty cycle and the send queue

On 868 MHz the CUL may send for 1% of the time. culfw tracks this as
`credit10ms`: at most 900, plus one per second. A command to a sleeping
thermostat needs a 1 second wake-up preamble and costs about 110 credits, so a
full budget lasts for about eight commands; after that you get roughly one
command every two minutes. `Cul.credits` shows the current value after each send.

All commands go through `MaxProtocol.queue`, which keeps one frame on air at a
time and waits for credit instead of failing. Every command method awaits its
frame's Ack; `enqueue()` returns the queue entry without waiting. The queue:

- retries a frame up to twice when the Ack does not arrive within 3 s (as in
  FHEM); a negative Ack (`NakError`) is not retried;
- replaces queued, not yet sent frames that serve the same purpose. Moving a
  set point slider five times sends one frame, not five;
- holds frames for window contacts until the contact transmits, because it
  only listens right after sending;
- recomputes time information right before sending;
- waits for a stick to (re)connect; `expires=` limits how long a frame may wait.

`queue.pending`, `queue.waiting_for_credit` and `queue.add_listener()` let an
application show what is waiting; `queue.clear()` drops unsent frames.

With `fast_send_after_wake_up=True` the queue wakes a device up first when at
least three frames wait for it and sends the rest without preamble, which
costs about a tenth of the credit. FHEM had this disabled because the first
frame after the wake-up was lost, so it is off by default.

## Supported MAX! features

| Feature | API |
|---|---|
| Receive thermostat, wall thermostat, window contact and eco switch state, also from the state in every Ack | `MaxDevice.state`, listeners |
| Status flags: battery, radio link, buttons locked, automatic daylight saving time, paired with a controller | `MaxDeviceState` |
| Eco switch key presses, counted once even though the switch sends each press to every linked thermostat | `state.button`, `state.button_presses` |
| Set temperature (manual, auto until next switch point, temporary until a date) | `set_temperature()` |
| Automatic (week profile) mode, boost | `set_auto()`, `set_boost()` |
| Pairing, re-pairing after battery change, adoption of already paired devices | `start_pairing()`, `adopt_devices` |
| Time sync: answer time requests and send the time periodically | `send_time_information()`, `time_sync_interval` |
| Temperature configuration (comfort, eco, min/max, offset, window open) | `configure_temperatures()` |
| Valve configuration (boost, decalcification, max valve, valve offset) | `configure_valve()` |
| Wall thermostat display mode | `set_display_actual_temperature()` |
| Link partners | `add_link_partner()`, `remove_link_partner()` |
| Fake wall thermostat (feed an external room temperature to a radiator thermostat) | `associate_fake_wall_thermostat()`, `send_fake_wall_thermostat()` |
| Fake shutter contact (window open/closed from another sensor) | `associate_fake_shutter_contact()`, `send_fake_shutter_contact()` |
| Wake-up, factory reset | `wake_up()`, `factory_reset()` |
| Groups (heating zones): assign, set up a zone with links and shared window contacts, set temperature or auto mode with one frame | `set_group()`, `group_devices()`, `set_group_temperature()`, `set_group_auto()` |
| Learn group ids and link partners from traffic between devices | `MaxDevice.group`, `MaxDevice.link_partners` |
| Suggest zones from learned links | `link_clusters()` |
| Week profiles | `set_week_profile()`, `set_day_profile()`, `MaxDevice.week_profile` |
| Several sticks for one MAX! network | attach one `MaxProtocol` to several `Cul`s |

Failures surface as `NoAckError`, `NakError`, `SendExpiredError` or
`SendCancelledError` (all subclasses of `CulError`).

### Device state and Acks

`MaxDevice.state` holds the last known values; `None` means unknown. Besides
the regular status messages, every Ack a thermostat or window contact sends
carries its current state (mode, valve position, set point, status flags), so
any command, a time update included, refreshes the device. Acks are too short
for the end date of a temporary set point; the known end date is kept.

Frames between other devices are decoded as well: a wall thermostat telling
its radiator thermostats the room temperature updates the wall thermostat's
state even though nothing was addressed to the controller.

### Groups (heating zones)

A group id (FHEM: `groupid`, the Cube calls it room) ties a wall thermostat,
radiator thermostats and window contacts together into one heating zone. A
zone is not necessarily a room: a large room with two wall thermostats is two
zones. Two things make a working zone:

- **The group id.** A set point sent to one member with the group id and flag
  0x04 is applied by all members. `set_temperature()`, `set_auto()` and
  `set_boost()` on a grouped device therefore need a single frame, and
  `set_group_temperature()` / `set_group_auto()` send one frame to the member
  with the best reception. Only the addressed device acknowledges; the other
  members are updated optimistically and report their real state with their
  next status message. Unsent set points for the same group are merged.
- **Links.** Devices only talk to their link partners: a wall thermostat sends
  its set point and room temperature to the radiator thermostats it is linked
  with, a window contact reports to the thermostats it is linked with.

Only set points and the frames of the fake wall thermostat / window contact
carry the group id. Configuration, links, week profiles and a factory reset go
to the addressed device only, as in FHEM.

`group_devices()` sets up a zone like the Cube sets up a room:

```python
results = await max_proto.group_devices(
    ["0a1b2c", "0d1e2f", "0e0e0e", "5c5c5c"],
    group=3,
    shared_window_contacts=["5d5d5d"],  # e.g. a door between two zones
)
# {"0a1b2c": None, ..., "5c5c5c": NoAckError(...)} - an error or None per device
```

Every device gets the group id; thermostats and window contacts are linked
with each other in both directions (window contacts not with each other).
Shared window contacts are linked with the zone's thermostats but keep their
group, so one contact can act on several zones; leave such contacts without
group. Links that are already known are not sent again, so repeating the call
after a partial failure only sends what is missing. The frames for a window
contact are queued together and go out the next time it transmits (open or
close the window). A zone of four devices needs about 16 frames, half the
hourly duty cycle budget. Group 0 removes the devices from their group and
keeps the links. A zone with more than one wall thermostat is logged as a
warning: each of them sends its set point to all thermostats it is linked
with, so they overrule each other.

### Learning groups and links

Neither the group id nor the link partners can be queried from a device, and
FHEM never stored them unless they were set through FHEM. pycul learns both by
listening:

- Frames a device sends to its link partners (thermostat state, wall
  thermostat control, set point, window contact and eco switch state) reveal
  the link: the receiver is in the sender's `link_partners`.
- Such frames carry the sender's group id when it is in a group (flag 0x04).
  Frames to the controller always carry group 0, which is why the group seems
  to be missing when only those are looked at.

Learned values are kept in `MaxDevice.group` and `MaxDevice.link_partners` and
persisted with `to_dict()`; links created or removed with
`add_link_partner()` / `remove_link_partner()` are recorded too.
`link_clusters()` turns the links into sets of devices that belong together,
i.e. zones as they are actually set up. Partners that have not transmitted yet
are included; the CUL's own fake devices are not, as they may be linked with
thermostats of every room.

Learning needs traffic between devices: wall thermostats send regularly,
radiator thermostats when their valve moves, window contacts when the window
is opened or closed. `pycul topology` (see below) listens and prints the result.

### Week profiles

A thermostat stores up to 13 switch points per day and follows them in
automatic mode on its own, also when the controller is offline. pycul builds
days from time periods with a base temperature in between, the same shape as a
Home Assistant schedule helper with a `temperature` data field per block:

```python
from pycul.protocols.max import DayProfile, WeekProfile

workday = DayProfile.from_periods([("06:00", "08:30", 21), ("17:00", "22:30", 21)], 17)
weekend = DayProfile.from_periods([("07:30", "23:00", 21)], 17)
profile = WeekProfile.from_days({d: workday for d in range(5)} | {5: weekend, 6: weekend})

await max_proto.set_week_profile("0a1b2c", profile)
```

Times are rounded to the device's 5 minute grid; a day that would need more
than 13 switch points raises `PayloadError` before anything is sent.

Profiles cannot be read back over the air. `MaxDevice.week_profile` is
therefore the last profile written successfully (or the factory profile after
pairing), and `None` if unknown, for example right after migrating from FHEM.
Writing a full profile takes 7 to 14 frames, more than one full duty cycle
budget, so `set_week_profile()` only sends days that differ from the known
profile and updates `week_profile` per day once all frames of that day are
acknowledged. `target_at()` and `next_switch()` compute the scheduled
temperature and the next change for display purposes.

### Several sticks

One `MaxProtocol` can be attached to several CULs, for example to reach
devices on another floor:

```python
max_proto = MaxProtocol(address="123456")
for url, name in (("/dev/ttyACM0", "ground"), ("socket://192.168.1.10:2323", "attic")):
    cul = Cul(url, name=name)
    cul.add_protocol(max_proto)
    await cul.connect()
```

All sticks use the same controller address. A frame heard by several sticks is
processed once; the RSSI per stick is kept in `MaxDevice.rssi_by_cul`, and
each command is sent through the stick that hears the target device best
(`MaxDevice.preferred_cul` overrides this). If a stick goes offline, the others
take over. Only one frame is on air at a time across all sticks, so they do not
collide. Each stick acknowledges frames addressed to the controller on its own,
so devices may receive the same Ack twice; this is harmless and matches FHEM's
behaviour with several CULs.

### Fake wall thermostat and shutter contact

The CUL can emulate a wall thermostat (default address `111111`) and a window
contact (`222222`), which is useful to feed values from other sensors into
the thermostats. Link the fake to the thermostat once, then send updates:

```python
await max_proto.associate_fake_wall_thermostat("0a1b2c")
await max_proto.send_fake_wall_thermostat(
    "0a1b2c", target_temperature=21.0, current_temperature=19.6
)

await max_proto.associate_fake_shutter_contact("0a1b2c")
await max_proto.send_fake_shutter_contact("0a1b2c", window_open=True)
```

A radiator thermostat linked to a wall thermostat regulates on the wall
thermostat's temperature and expects regular updates. Each update consumes
duty cycle, so a few minutes between updates is a sensible interval.

## Command line tool

Installing the package provides a `pycul` command for testing and one-off actions:

```bash
pycul -d /dev/ttyACM0 info
pycul -d /dev/ttyACM0 monitor                      # print all frames and device states
pycul -d /dev/ttyACM0 topology --seconds 3600      # learn groups and links, sends nothing
pycul -d /dev/ttyACM0 set-temp 0a1b2c 21.5
pycul -d /dev/ttyACM0 set-temp 0a1b2c 18 --until "2026-11-01 06:00"
pycul -d /dev/ttyACM0 auto 0a1b2c
pycul -d /dev/ttyACM0 pair --seconds 120
pycul -d /dev/ttyACM0 associate 0a1b2c fake-wt
pycul -d /dev/ttyACM0 fake-wt 0a1b2c 21 19.6
pycul -d /dev/ttyACM0 group 0a1b2c 2
pycul -d /dev/ttyACM0 group-temp 2 20.5 0a1b2c 0d1e2f
pycul -d /dev/ttyACM0 profile 0a1b2c --day monday "06:00-08:30=21,17:00-22:00=21" --base 17
pycul -d /dev/ttyACM0 profile 0a1b2c --file profile.json
pycul -d /dev/ttyACM0 raw V                        # raw culfw command
```

`-d` can be given several times to use several sticks. `-a` sets the
controller address (default `123456`). `PYCUL_DEVICE` (comma separated) and
`PYCUL_MAX_ADDRESS` can replace `-d` and `-a`. Run `pycul --help` and `pycul <command> --help`
for all options.

`topology` shows how an existing installation is set up: devices only reveal
their group id and link partners in frames to each other, so let it listen for
an hour, or turn a wall thermostat's wheel and open a window meanwhile. The
result lists the groups seen on air, the links, and the devices that form a
zone through their links; `--json` prints it machine readable. `monitor` and
`topology` reconnect when the stick is unplugged and plugged in again, the
other commands fail right away.

## Connection handling

A `Cul` reconnects on its own when the stick disappears (unplugged, USB reset,
ser2net restart), with a back-off from 5 to 60 seconds; `request_reconnect()`
skips the wait, for example when the host reports a new USB device. A stick
that stops answering without disappearing (two commands in a row without
answer) is closed and reopened as well. Frames that could not be sent because
no stick was connected stay queued and keep their retries.

Use the `/dev/serial/by-id/...` path: after re-plugging, `/dev/ttyACM0` may
come back as `/dev/ttyACM1`.

## Differences from FHEM

pycul follows FHEM's protocol handling (`10_MAX.pm`, `14_CUL_MAX.pm`). Some
long-standing problems of FHEM's implementation are fixed on the way. The
references are to the FHEM source; several of them carry a `ToDo` comment
there.

**Window contacts never get their frames.** A window contact only listens
right after it transmitted. FHEM holds frames for it and sends them in
`CUL_MAX_SQH` only after the contact sent an Ack, which it does in practice
only while pairing. The check on the contact's regular state messages is
commented out (`# ToDo Test FK` in `CUL_MAX_Parse`). Linking a window contact
with a thermostat after pairing therefore rarely reaches it. pycul releases
held frames on any frame from the contact and sends everything queued for it
in that window.

**The send queue is dropped without a stick.** If `CUL_MAX` has no IODev at
the moment, `CUL_MAX_SQH` empties the whole send queue
(`$hash->{sendQueue} = []`), losing every pending command. pycul keeps frames
queued until a stick is connected again; a frame that did not reach the air
keeps its retries.

**The group id is never read.** `CUL_MAX_Parse` extracts the group from every
frame but never passes it on (`# ToDo : GroupID vom Device holen !` in
`CUL_MAX_Send`); the `groupid` reading only reflects what was set through FHEM.
Links are only known if FHEM created them. pycul learns both from traffic
between devices.

**Fast sending is dead code.** `CUL_MAX_SQH` computes whether a device is
awake and the 1 second preamble can be skipped, then overrides the result with
`$needPreamble = 1;`. Every frame costs the full preamble. pycul offers fast
sending after a wake-up as an option (`fast_send_after_wake_up`), off by
default because FHEM's authors saw the first frame after a wake-up get lost.

**A zone set point costs one frame per thermostat.** FHEM sets temperatures
per device, and each of these frames carries the group id, so the first one
already reaches every thermostat of the group; setting all of them sends the
same command again for each. pycul sends one frame per zone.

**Repeated commands pile up.** FHEM queues every command; moving a slider five
times sends five frames of about one second air time each. pycul merges unsent
frames with the same purpose.

**Acks erase the end of a temporary set point.** An Ack carries the
thermostat's state without the end date; FHEM's `MAX_Parse` then clears the
`until` value. pycul keeps it.

**Valve percentages are truncated.** `MAX_Set` converts with `int()`, so a
maximum valve opening of 50 % becomes 127 instead of 128 of 255. pycul rounds.

One problem is not FHEM's and cannot be fixed on this side: culfw cuts
received frames longer than its buffer (`MAX_MORITZ_MSG` in `rf_moritz.c`) to
the buffer size and rewrites the length byte, so a truncated frame looks
valid. MAX! frames stay well below that limit.

## Hardware test

[`examples/example.py`](examples/example.py) is both a usage example and a
test for a real setup. It connects, listens for a while, then for each given
thermostat sends the time, sets a test temperature, checks the Ack and
restores the previous mode:

```bash
python examples/example.py -d /dev/ttyACM0 -a 123456 -t 0a1b2c -t 0d1e2f
```

Use `-v` to print every radio frame and `--debug` to log the raw serial
traffic. The exit code is non-zero if any thermostat failed.

## Architecture

```
Cul (one per stick)        serial/network connection, command/response matching,
 │                         send confirmation and credit tracking, reconnect
 └── CulProtocol           one handler per radio protocol, attachable to several sticks
      └── MaxProtocol      MAX!: frames, Acks, pairing, groups, learning, week profiles
           ├── SendQueue   one frame on air, retries, credit waits, coalescing
           └── MaxDevice   device model with state, week profile and listeners
```

A protocol declares the culfw commands that switch a stick into its mode and
the output lines it consumes; each callback receives the stick it concerns.
culfw drives one CC1101 transceiver per stick, so all protocols attached to one
`Cul` must use the same RF mode; `add_protocol()` enforces this. To add a
protocol, subclass `pycul.protocols.CulProtocol` and implement
`init_commands()` and `handle_line()`.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e '.[test]' ruff
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

The tests run against an in-memory culfw emulation (`tests/conftest.py`); no
hardware is needed.

## License and credits

pycul is licensed under the GNU General Public License v2.0 or later, see
[LICENSE](LICENSE).

The MAX! protocol knowledge comes from the FHEM modules `10_MAX.pm` and
`14_CUL_MAX.pm` (Matthias Gehre, Wzut) and from culfw's `rf_moritz.c`, both
GPL-licensed. Modules that follow these sources say so in their headers.
[NOTICE.md](NOTICE.md) lists all sources.

Author: Nils Sowen <n.sowen@2scale.net>
