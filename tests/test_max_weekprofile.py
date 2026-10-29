# Author: Nils Sowen <n.sowen@2scale.net>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for MAX! week profiles."""

from __future__ import annotations

from datetime import datetime

import pytest

from pycul.protocols.max import DayProfile, WeekProfile
from pycul.protocols.max.payloads import PayloadError
from pycul.protocols.max.weekprofile import max_day_index

#: FHEM 10_MAX.pm $defaultWeekProfile (days Saturday..Friday, 52 hex digits each).
FHEM_DEFAULT = (
    "444855084520452045204520452045204520452045204520452044485508452045204520452045204520"
    "452045204520452045204448546c44cc55144520452045204520452045204520452045204448546c44cc"
    "55144520452045204520452045204520452045204448546c44cc551445204520452045204520452045204520"
    "45204448546c44cc55144520452045204520452045204520452045204448546c44cc5514452045204520"
    "452045204520452045204520"
)


def test_factory_default_matches_fhem() -> None:
    profile = WeekProfile.factory_default()
    for weekday, day in enumerate(profile.days):
        index = max_day_index(weekday)
        assert day.encode().hex() == FHEM_DEFAULT[index * 52 : index * 52 + 52]
        assert DayProfile.decode(day.encode()) == day


def test_from_periods_like_ha_schedule() -> None:
    day = DayProfile.from_periods([("06:00", "08:30", 21), ("17:00", "22:00", 21.5)], 17)
    assert [(p.until, p.temperature) for p in day.points] == [
        (360, 17),
        (510, 21),
        (1020, 17),
        (1320, 21.5),
        (1440, 17),
    ]
    assert day.temperature_at(7 * 60) == 21
    assert day.temperature_at(12 * 60) == 17
    assert [p.temperature for p in day.periods()] == [17, 21, 17, 21.5, 17]


def test_from_periods_merges_and_rounds() -> None:
    # Adjacent blocks with the same temperature become one point; 06:07 rounds to 06:05.
    day = DayProfile.from_periods([("06:07", "12:00", 21), ("12:00", "24:00", 21)], 17)
    assert [(p.until, p.temperature) for p in day.points] == [(365, 17), (1440, 21)]


def test_validation() -> None:
    with pytest.raises(PayloadError):
        DayProfile.from_periods([("06:00", "08:00", 21), ("07:00", "09:00", 20)], 17)
    too_many = [(f"{h:02d}:00", f"{h:02d}:30", 21) for h in range(1, 14)]
    with pytest.raises(PayloadError):
        DayProfile.from_periods(too_many, 17)
    with pytest.raises(PayloadError):
        DayProfile.from_points([("06:00", 21)])  # does not end at 24:00
    with pytest.raises(PayloadError):
        DayProfile.from_points([("24:00", 21.3)])


def test_config_payloads_split_after_seven_points() -> None:
    short = WeekProfile.factory_default().days[0]
    [payload] = short.config_payloads(2)
    assert payload[0] == 0x02
    assert len(payload) == 15
    long = DayProfile.from_periods([(f"{h:02d}:00", f"{h:02d}:30", 21) for h in range(4, 10)], 17)
    assert len(long.points) == 13
    first, second = long.config_payloads(5)
    assert first[0] == 0x05
    assert second[0] == 0x15
    assert len(second) == 13


def test_changed_days() -> None:
    base = WeekProfile.factory_default()
    changed = base.replace(2, DayProfile.constant(19))
    assert changed.changed_days(base) == [2]
    assert changed.changed_days(None) == list(range(7))


def test_target_and_next_switch() -> None:
    profile = WeekProfile.factory_default()
    wednesday_noon = datetime(2026, 10, 28, 12, 0)
    assert profile.target_at(wednesday_noon) == 17
    assert profile.next_switch(wednesday_noon) == (datetime(2026, 10, 28, 17, 0), 21)
    saturday_late = datetime(2026, 10, 31, 23, 0)
    assert profile.next_switch(saturday_late) == (datetime(2026, 11, 1, 6, 0), 21)


def test_dict_roundtrip() -> None:
    profile = WeekProfile.factory_default().replace(
        4, DayProfile.from_periods([("07:00", "20:00", 22)], 16.5)
    )
    data = profile.to_dict()
    assert data["friday"][1] == {"from": "07:00", "to": "20:00", "temperature": 22.0}
    assert WeekProfile.from_dict(data) == profile
