# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Week profiles (heating schedules) of MAX! thermostats.

On the device a day is a list of up to 13 switch points "temperature until
time", the last one ending at 24:00. Each point is a 16 bit value:
temperature * 2 in the upper 7 bits, end time / 5 minutes in the lower 9 bits.
Days are numbered from Saturday (0) to Friday (6). Format and factory profile
follow FHEM's 10_MAX.pm (MAX_ParseWeekProfile, MAX_Set weekProfile),
(c) 2012 Matthias Gehre, (c) 2019 Wzut, GPL-2.0-or-later, see NOTICE.md.

The profile cannot be read back over the air. pycul therefore only knows a
profile after it wrote it (or after pairing, when the device has the factory
profile).

Besides switch points, a day can be built from time periods with a base
temperature in between (`DayProfile.from_periods`), which maps directly to
Home Assistant's schedule helper (blocks with a `temperature` data field).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from pycul.protocols.max.payloads import PayloadError, encode_temperature

MAX_SWITCH_POINTS = 13
MINUTES_PER_DAY = 24 * 60
POINTS_PER_FIRST_PART = 7
#: Unused slots contain "17 degree until 24:00" (FHEM: "4520").
_FILLER = 0x4520

WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def max_day_index(weekday: int) -> int:
    """Convert a Python weekday (Monday = 0) to the device's day index (Saturday = 0)."""
    return (weekday + 2) % 7


def _minutes(value: time | str | int) -> int:
    """Convert "HH:MM", "24:00", a time or minutes since midnight to minutes."""
    if isinstance(value, int):
        minutes = value
    elif isinstance(value, time):
        minutes = MINUTES_PER_DAY if value == time.max else value.hour * 60 + value.minute
    else:
        hour, _, minute = value.partition(":")
        minutes = int(hour) * 60 + int(minute[:2] or 0)
    if not 0 <= minutes <= MINUTES_PER_DAY:
        raise PayloadError(f"Time out of range: {value!r}")
    return minutes


def format_minutes(minutes: int) -> str:
    """Format minutes since midnight as "HH:MM" (1440 -> "24:00")."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(slots=True, frozen=True)
class SwitchPoint:
    """`temperature` applies until `until` minutes after midnight (5..1440)."""

    until: int
    temperature: float

    def __post_init__(self) -> None:
        if self.until % 5 or not 0 < self.until <= MINUTES_PER_DAY:
            raise PayloadError(f"Switch time must be a multiple of 5 minutes: {self.until}")
        encode_temperature(self.temperature)  # validates range
        if self.temperature * 2 != int(self.temperature * 2):
            raise PayloadError(f"Temperature must be in 0.5 degree steps: {self.temperature}")

    def encode(self) -> int:
        return (encode_temperature(self.temperature) << 9) | (self.until // 5)

    @classmethod
    def decode(cls, value: int) -> SwitchPoint:
        minutes = (value & 0x1FF) * 5
        return cls(until=minutes or MINUTES_PER_DAY, temperature=((value >> 9) & 0x3F) / 2)


@dataclass(slots=True, frozen=True)
class Period:
    """A time range [start, end) with a temperature, in minutes since midnight."""

    start: int
    end: int
    temperature: float


@dataclass(slots=True, frozen=True)
class DayProfile:
    """The schedule of one day as switch points (ascending, last until 24:00)."""

    points: tuple[SwitchPoint, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.points) <= MAX_SWITCH_POINTS:
            raise PayloadError(f"A day needs 1..{MAX_SWITCH_POINTS} switch points")
        previous = 0
        for point in self.points:
            if point.until <= previous:
                raise PayloadError("Switch points must be in ascending order")
            previous = point.until
        if previous != MINUTES_PER_DAY:
            raise PayloadError("The last switch point must end at 24:00")

    # -------------------------------------------------------------- building

    @classmethod
    def constant(cls, temperature: float) -> DayProfile:
        """One temperature for the whole day."""
        return cls((SwitchPoint(MINUTES_PER_DAY, temperature),))

    @classmethod
    def from_points(cls, points: Iterable[tuple[time | str | int, float]]) -> DayProfile:
        """Build from (until, temperature) pairs; adjacent equal temperatures are merged."""
        merged: list[SwitchPoint] = []
        for until, temperature in points:
            point = SwitchPoint(_minutes(until), float(temperature))
            if merged and merged[-1].temperature == point.temperature:
                merged[-1] = point
            else:
                merged.append(point)
        return cls(tuple(merged))

    @classmethod
    def from_periods(
        cls,
        periods: Iterable[tuple[time | str | int, time | str | int, float]],
        base_temperature: float,
    ) -> DayProfile:
        """Build from (start, end, temperature) periods; gaps use `base_temperature`.

        Times are rounded down to 5 minutes, the device's resolution.
        Raises PayloadError if periods overlap or the day needs more than 13
        switch points.
        """
        points: list[tuple[int, float]] = []
        cursor = 0
        for start, end, temperature in sorted(
            ((_minutes(s) // 5 * 5, _minutes(e) // 5 * 5, t) for s, e, t in periods)
        ):
            if end <= start:
                continue  # shorter than 5 minutes after rounding
            if start < cursor:
                raise PayloadError(
                    f"Overlapping periods at {format_minutes(start)}-{format_minutes(end)}"
                )
            if start > cursor:
                points.append((start, base_temperature))
            points.append((end, temperature))
            cursor = end
        if cursor < MINUTES_PER_DAY:
            points.append((MINUTES_PER_DAY, base_temperature))
        return cls.from_points(points)

    # ------------------------------------------------------------- reading

    def periods(self) -> list[Period]:
        """Return the day as consecutive periods covering 00:00-24:00."""
        result: list[Period] = []
        start = 0
        for point in self.points:
            result.append(Period(start, point.until, point.temperature))
            start = point.until
        return result

    def temperature_at(self, minutes: int) -> float:
        """Return the scheduled temperature at `minutes` after midnight."""
        for point in self.points:
            if minutes < point.until:
                return point.temperature
        return self.points[-1].temperature

    # -------------------------------------------------------------- coding

    def encode(self) -> bytes:
        """Encode all 13 slots (26 bytes)."""
        values = [p.encode() for p in self.points]
        values += [_FILLER] * (MAX_SWITCH_POINTS - len(values))
        return b"".join(v.to_bytes(2, "big") for v in values)

    @classmethod
    def decode(cls, data: bytes) -> DayProfile:
        """Decode 26 bytes; slots after the point ending at 24:00 are ignored."""
        if len(data) < 2 * MAX_SWITCH_POINTS:
            raise PayloadError("A day profile has 26 bytes")
        points: list[SwitchPoint] = []
        for index in range(MAX_SWITCH_POINTS):
            point = SwitchPoint.decode(int.from_bytes(data[2 * index : 2 * index + 2], "big"))
            points.append(point)
            if point.until == MINUTES_PER_DAY:
                break
        return cls(tuple(points))

    def config_payloads(self, max_day: int) -> list[bytes]:
        """Return the ConfigWeekProfile payload(s) for this day.

        The first frame carries switch points 1-7, a second one points 8-13
        and is only needed if the day uses more than 7 points: the device stops
        reading at the point ending at 24:00 (FHEM: MAX_Set weekProfile).
        """
        data = self.encode()
        split = 2 * POINTS_PER_FIRST_PART
        payloads = [bytes([max_day]) + data[:split]]
        if len(self.points) > POINTS_PER_FIRST_PART:
            payloads.append(bytes([0x10 | max_day]) + data[split:])
        return payloads

    def to_dict(self) -> list[dict[str, str | float]]:
        """Serialise as a list of {"from", "to", "temperature"} periods."""
        return [
            {
                "from": format_minutes(p.start),
                "to": format_minutes(p.end),
                "temperature": p.temperature,
            }
            for p in self.periods()
        ]

    @classmethod
    def from_dict(cls, data: Iterable[Mapping[str, object]]) -> DayProfile:
        """Restore a day serialised with to_dict()."""
        return cls.from_points((str(p["to"]), float(p["temperature"])) for p in data)  # type: ignore[arg-type]


#: Factory profile: 17 degree, 21 degree 06:00-22:00 on weekends,
#: 06:00-09:00 and 17:00-23:00 on weekdays.
_WEEKDAY = DayProfile.from_points(
    [("06:00", 17), ("09:00", 21), ("17:00", 17), ("23:00", 21), ("24:00", 17)]
)
_WEEKEND = DayProfile.from_points([("06:00", 17), ("22:00", 21), ("24:00", 17)])


@dataclass(slots=True, frozen=True)
class WeekProfile:
    """Seven day profiles, indexed by Python weekday (Monday = 0)."""

    days: tuple[DayProfile, DayProfile, DayProfile, DayProfile, DayProfile, DayProfile, DayProfile]

    @classmethod
    def factory_default(cls) -> WeekProfile:
        """The profile a device has after a factory reset."""
        return cls((_WEEKDAY,) * 5 + (_WEEKEND,) * 2)  # type: ignore[arg-type]

    @classmethod
    def from_days(cls, days: Mapping[int | str, DayProfile]) -> WeekProfile:
        """Build from {weekday or name: DayProfile}; all seven days are required."""
        by_index: dict[int, DayProfile] = {}
        for key, day in days.items():
            index = WEEKDAY_NAMES.index(key.lower()) if isinstance(key, str) else key
            by_index[index] = day
        if sorted(by_index) != list(range(7)):
            raise PayloadError("A week profile needs all seven days")
        return cls(tuple(by_index[i] for i in range(7)))  # type: ignore[arg-type]

    def replace(self, weekday: int, day: DayProfile) -> WeekProfile:
        """Return a copy with one day replaced."""
        days = list(self.days)
        days[weekday] = day
        return WeekProfile(tuple(days))  # type: ignore[arg-type]

    def changed_days(self, other: WeekProfile | None) -> list[int]:
        """Return the weekdays that differ from `other` (all if other is unknown)."""
        if other is None:
            return list(range(7))
        return [i for i in range(7) if self.days[i] != other.days[i]]

    def target_at(self, when: datetime) -> float:
        """Return the scheduled temperature at `when` (device local time)."""
        return self.days[when.weekday()].temperature_at(when.hour * 60 + when.minute)

    def next_switch(self, when: datetime) -> tuple[datetime, float] | None:
        """Return the next time the scheduled temperature changes and the new value."""
        current = self.target_at(when)
        midnight = datetime.combine(when.date(), time())
        for offset in range(8):
            day = self.days[(when.weekday() + offset) % 7]
            for period in day.periods():
                start = midnight + timedelta(days=offset, minutes=period.start)
                if start > when and period.temperature != current:
                    return start, period.temperature
        return None

    def to_dict(self) -> dict[str, list[dict[str, str | float]]]:
        """Serialise as {"monday": [{"from", "to", "temperature"}, ...], ...}."""
        return {WEEKDAY_NAMES[i]: day.to_dict() for i, day in enumerate(self.days)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Iterable[Mapping[str, object]]]) -> WeekProfile:
        """Restore a profile serialised with to_dict()."""
        return cls.from_days({name: DayProfile.from_dict(day) for name, day in data.items()})


__all__ = [
    "MAX_SWITCH_POINTS",
    "WEEKDAY_NAMES",
    "DayProfile",
    "Period",
    "SwitchPoint",
    "WeekProfile",
    "format_minutes",
    "max_day_index",
]
