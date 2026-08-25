"""The peak-window boundaries are load-bearing regulatory facts (C2/C3).

If these tests are wrong, every number the system reports is wrong, so they assert
the boundary instants explicitly rather than sampling the interior.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mandate_recovery.core.clock import (
    IST,
    SLOT_MINUTES,
    ceil_to_slot,
    floor_to_slot,
    is_non_peak,
    is_peak,
    is_slot_aligned,
    non_peak_slots,
    non_peak_slots_per_day,
    slot_grid,
    to_ist,
)


def at(h: int, m: int = 0) -> datetime:
    return datetime(2026, 9, 1, h, m, tzinfo=IST)


@pytest.mark.parametrize(
    "hour,minute,expected_peak",
    [
        (0, 0, False),    # midnight — the widest non-peak block
        (9, 59, False),
        (10, 0, True),    # peak window opens, inclusive
        (12, 59, True),
        (13, 0, False),   # peak window closes, exclusive
        (16, 59, False),
        (17, 0, True),    # second peak window opens
        (21, 0, True),
        (21, 29, True),
        (21, 30, False),  # closes at 21:30 exactly, exclusive
        (23, 59, False),
    ],
)
def test_peak_window_boundaries(hour: int, minute: int, expected_peak: bool) -> None:
    dt = at(hour, minute)
    assert is_peak(dt) is expected_peak
    assert is_non_peak(dt) is (not expected_peak)


def test_non_peak_slots_per_day_is_thirty_three() -> None:
    """48 slots/day minus 15 peak slots (6 in 10:00-13:00, 9 in 17:00-21:00)."""
    assert non_peak_slots_per_day() == 33


def test_non_peak_blocks_total_sixteen_and_a_half_hours() -> None:
    assert non_peak_slots_per_day() * SLOT_MINUTES == 16 * 60 + 30


def test_naive_datetime_is_rejected() -> None:
    """An ambiguous instant in a payments system is a defect, not a convenience."""
    with pytest.raises(ValueError, match="naive datetime"):
        to_ist(datetime(2026, 9, 1, 12, 0))


def test_peakness_is_evaluated_in_ist_not_utc() -> None:
    """05:00 UTC is 10:30 IST — peak. A UTC-naive implementation would miss this."""
    utc_dt = datetime(2026, 9, 1, 5, 0, tzinfo=timezone.utc)
    assert to_ist(utc_dt).hour == 10
    assert is_peak(utc_dt)


def test_slot_alignment_and_rounding() -> None:
    assert is_slot_aligned(at(6, 30))
    assert not is_slot_aligned(at(6, 31))
    assert floor_to_slot(at(6, 45)) == at(6, 30)
    assert ceil_to_slot(at(6, 45)) == at(7, 0)
    assert ceil_to_slot(at(6, 30)) == at(6, 30)  # identity when already aligned


def test_slot_grid_is_half_open() -> None:
    slots = list(slot_grid(at(0), at(2)))
    assert slots[0] == at(0, 0)
    assert slots[-1] == at(1, 30)
    assert at(2, 0) not in slots


def test_non_peak_slots_excludes_every_peak_instant() -> None:
    day = at(0)
    slots = list(non_peak_slots(day, day + timedelta(days=1)))
    assert len(slots) == 33
    assert all(is_non_peak(s) for s in slots)
    assert at(10, 0) not in slots
    assert at(21, 0) not in slots
    assert at(21, 30) in slots
