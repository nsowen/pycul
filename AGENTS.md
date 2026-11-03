# AGENTS.md — pycul

Instructions for AI coding agents and contributors working in this repository.

## Project overview

pycul is an asyncio Python library (plus a CLI) that runs an eQ-3 MAX! home
heating network through a culfw-based 868 MHz USB stick (NanoCUL, CUL868,
CUNO/CUN): the stick acts as the MAX! controller the way a MAX! Cube or FHEM's
`CUL_MAX` does. The protocol logic is a port of FHEM's long-standing MAX!
implementation with documented bug fixes; [hacul](https://github.com/nsowen/hacul)
is the Home Assistant integration built on top.

## Code layout

```
src/pycul/
  cul.py                 Cul: one instance per stick — serial/network I/O,
                         command/response matching, credit tracking, reconnect
  cli.py                 the `pycul` command line tool (argparse subcommands)
  exceptions.py          CulError hierarchy (NoAckError, NakError, ...)
  protocols/
    base.py              CulProtocol: attachable radio protocol handlers
    max/
      message.py         MaxMessage: frame envelope (addresses, counters, RSSI)
      payloads.py        encode/decode of message payloads (bit layouts)
      device.py          MaxDevice / MaxDeviceState: device model, listeners,
                         to_dict()/from_dict() persistence
      protocol.py        MaxProtocol: message handling, pairing, Acks, groups,
                         learning, fake devices
      queue.py           SendQueue: one frame on air, retries, credit waits,
                         coalescing, hold-and-release for shutter contacts
      weekprofile.py     DayProfile/WeekProfile: switch point math
      const.py           enums, protocol constants (FHEM-derived)
tests/                   pytest suite against an in-memory culfw emulation
examples/example.py      usage example and hardware smoke test
```

## Conventions

- **Payload types** are `@dataclass(slots=True, frozen=True)` value objects
  with `decode(cls, payload: bytes)` and `encode() -> bytes` methods
  (`message.py` and `device.py` use `slots=True` variants suited to their
  mutability). Invalid data raises `PayloadError`.
- **Docstrings reference the FHEM equivalent** of anything ported, e.g.
  `"""Encode the payload (FHEM: CUL_MAX_Set fakeWT)."""` — keep these pointers
  so behaviour can be traced back to the reference implementation. Module
  docstrings name the FHEM modules the code derives from.
- **Module headers** carry the author line and
  `# SPDX-License-Identifier: GPL-2.0-or-later`; code derived from FHEM or
  culfw says so and is listed in `NOTICE.md`.
- Full **type hints** everywhere (`from __future__ import annotations`),
  `snake_case` for functions/variables, `PascalCase` classes, module-level
  constants in `SCREAMING_SNAKE_CASE`.
- Prefer explicit, small pure functions for bit twiddling (`decode_until`,
  `encode_temperature`, ...) next to the payload classes that use them.
- Formatting/linting: **ruff** (`ruff check . && ruff format --check .`).

## Testing

- `python -m pytest` (install first: `pip install -e '.[test]'`).
- Tests live in `tests/`, named `test_<area>_<behaviour>`, e.g.
  `test_config_payloads_match_fhem_defaults`,
  `test_shutter_contact_frames_wait_until_it_transmits`.
- **Payload tests compare hex encodings against known-good values** (captured
  from FHEM behaviour or real air traffic) and round-trip encode/decode.
  Keep them byte-exact; they are the protocol regression suite.
- Protocol/queue behaviour is tested against the **in-memory culfw emulation**
  (`tests/conftest.py`, `FakeStick`) — extend the emulation rather than
  reaching for mocks of pycul's own classes.
- **Hardware-in-the-loop testing needs a real stick and real devices** and
  cannot run in CI. Keep all protocol logic reachable from unit tests so the
  hardware test (`examples/example.py`) stays a smoke test, not the only
  coverage. Anything only verifiable on hardware should be noted in the
  commit/PR description.
- Async tests are `async def test_...` functions; no event loop plumbing
  beyond what pytest gives you.

## Command line

```bash
pycul -d <serial-device> -a <controller-address> <command>
```

`-d` takes a device path or pyserial URL (repeatable for several sticks,
env `PYCUL_DEVICE`), `-a` the six-hex-digit controller address (env
`PYCUL_MAX_ADDRESS`, default `123456` — the FHEM default, so keep it the
default). Subcommands: `info`, `monitor`, `topology`, `raw`, `set-temp`,
`auto`, `pair`, `group`, `profile`, `config-temps`, ... — see `pycul --help`.
The CLI is also the manual test tool against real hardware.

## Git conventions

- Small, focused commits: one behaviour or fix each.
- The commit message body explains **why** the change is needed and, for
  protocol behaviour, which FHEM reference or field observation backs it
  (bug it fixes, frame sequence it reproduces).
- New protocol behaviour arrives with its payload/queue tests in the same
  commit.
