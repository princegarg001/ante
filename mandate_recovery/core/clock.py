"""The IST execution clock and the NPCI non-peak slot grid.

Single source of truth for COMPLIANCE.md C2/C3. Nothing else in the codebase may
hard-code a peak window, and nothing in this package may read the wall clock —
every function that cares about time takes it as an argument.

Peak hours (execution forbidden):   10:00-13:00 and 17:00-21:30 IST
Non-peak (execution permitted):     00:00-10:00, 13:00-17:00, 21:30-24:00 IST

A boundary instant belongs to the window that starts at it: 10:00 is peak,
13:00 is non-peak, 17:00 is peak, 21:30 is non-peak.

IST is UTC+05:30 with no daylight saving, so a fixed offset is exact and avoids a
tzdata dependency that Windows images frequently lack.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Final, Iterator

IST: Final[timezone] = timezone(timedelta(hours=5, minutes=30), name="IST")

#: (start, end) pairs, half-open [start, end), in IST local time.
PEAK_WINDOWS: Final[tuple[tuple[time, time], ...]] = (
    (time(10, 0), time(13, 0)),
    (time(17, 0), time(21, 30)),
)

#: Execution slot granularity. 30 minutes gives 48 slots/day, 33 of them non-peak.
SLOT_MINUTES: Final[int] = 30
SLOTS_PER_DAY: Final[int] = 24 * 60 // SLOT_MINUTES


def to_ist(dt: datetime) -> datetime:
    """Normalise to IST. Naive datetimes are rejected — an ambiguous instant in a
    payments system is a defect, not a convenience.

    The identity fast path matters: the constraint layer calls this several times
    per rule, and the model checker evaluates millions of triples. `astimezone`
    allocates a new datetime even when the zone already matches.
    """
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime not allowed in the execution clock: {dt!r}")
    if dt.tzinfo is IST:
        return dt
    return dt.astimezone(IST)


def is_peak(dt: datetime) -> bool:
    """True if `dt` falls inside an NPCI peak window (C2)."""
    t = to_ist(dt).timetz().replace(tzinfo=None)
    return any(start <= t < end for start, end in PEAK_WINDOWS)


def is_non_peak(dt: datetime) -> bool:
    """True if `dt` is a permitted execution instant (C3)."""
    return not is_peak(dt)


def is_slot_aligned(dt: datetime) -> bool:
    """True if `dt` sits on the SLOT_MINUTES grid with no sub-minute residue."""
    ist = to_ist(dt)
    return (
        ist.second == 0
        and ist.microsecond == 0
        and (ist.hour * 60 + ist.minute) % SLOT_MINUTES == 0
    )


def floor_to_slot(dt: datetime) -> datetime:
    """Round `dt` down to the enclosing slot boundary."""
    ist = to_ist(dt)
    minutes = (ist.hour * 60 + ist.minute) // SLOT_MINUTES * SLOT_MINUTES
    return ist.replace(
        hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0
    )


def ceil_to_slot(dt: datetime) -> datetime:
    """Round `dt` up to the next slot boundary (identity if already aligned)."""
    floored = floor_to_slot(dt)
    return floored if floored == to_ist(dt) else floored + timedelta(minutes=SLOT_MINUTES)


def slot_grid(start: datetime, end: datetime) -> Iterator[datetime]:
    """Every slot-aligned instant in [start, end), peak included."""
    cursor = ceil_to_slot(start)
    stop = to_ist(end)
    step = timedelta(minutes=SLOT_MINUTES)
    while cursor < stop:
        yield cursor
        cursor += step


def non_peak_slots(start: datetime, end: datetime) -> Iterator[datetime]:
    """Every permitted execution slot in [start, end). This is the agent's action grid."""
    return (slot for slot in slot_grid(start, end) if is_non_peak(slot))


def non_peak_slots_per_day() -> int:
    """33, under the 30-minute grid. Computed, not asserted, so the constant can
    never drift away from PEAK_WINDOWS."""
    day = datetime(2026, 1, 1, tzinfo=IST)
    return sum(1 for _ in non_peak_slots(day, day + timedelta(days=1)))
