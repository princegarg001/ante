"""The opponents.

Three of them today. `B2` needs a calibrated probability model and arrives with
it on day 5.

`B1` is not a straw man — the +24h/+72h/+168h schedule is what a competent team
actually ships, and published data suggests moving the first retry from +2h to
+24h is worth around 6.5% on its own. If the allocator beats it by three
percent, the claim is three percent.

`B3` is the interesting one. It is Stripe's published shape — roughly eight
attempts across two weeks — transplanted unchanged. It is not included to
embarrass anyone: that policy is correct for card rails, where attempts are
plentiful and a decision can be executed the moment it is taken. It is included
because quantifying what happens when it is moved to Indian rails is the
cleanest available statement of why this problem needs its own design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Mapping, Sequence

from ..core.types import TERMINAL_CAUSES, Action, Commit, Stop, Wait
from .policy import Calendar, Candidate

MIN_LEAD_SLOTS: Final[int] = 48    # 24h
MAX_LEAD_SLOTS: Final[int] = 96    # 48h


@dataclass
class NoRetry:
    """B0 — the floor. Establishes what the batch is worth if nobody acts."""

    calendar: Calendar
    name: str = "B0 · no retry"

    def reset(self, seed: int) -> None:
        return None

    def plan(self, batch: Sequence[Candidate], now: datetime) -> Mapping[str, Action]:
        return {}


@dataclass
class FixedSchedule:
    """B1 — the industry heuristic: +24h, +72h, +168h after the failure.

    Constraint-aware, because a team shipping this in India would make it so:
    targets are snapped forward to a legal execution slot, committed only once
    the notification aperture opens, and abandoned when the cause is terminal.

    That last part was added after the first run showed B1 proposing thousands
    of retries against revoked and closed mandates. Leaving it in would have
    handed the allocator an easy win against an opponent that was documented as
    strong and was not. A baseline has to be the best version of itself.
    """

    calendar: Calendar
    offsets_hours: tuple[int, ...] = (24, 72, 168)
    name: str = "B1 · fixed +24/+72/+168h"
    _origin_failure: dict[str, int] = field(default_factory=dict)

    def reset(self, seed: int) -> None:
        self._origin_failure.clear()

    def plan(self, batch: Sequence[Candidate], now: datetime) -> Mapping[str, Action]:
        out: dict[str, Action] = {}
        for c in batch:
            if c.state.cause in TERMINAL_CAUSES:
                out[c.mandate_id] = Stop(reason=f"terminal cause {c.state.cause.value}")
                continue
            anchor = self._origin_failure.setdefault(c.mandate_id, c.last_failure_slot)
            index = c.state.attempts_used - 1        # 1 original execution already spent
            if not 0 <= index < len(self.offsets_hours):
                continue
            target = anchor + self.offsets_hours[index] * 2
            slot = _schedule(self.calendar, c.now_slot, target)
            if slot is None:
                continue
            out[c.mandate_id] = Commit(
                execute_at=self.calendar.time_of(slot),
                amount_paise=c.state.amount_due_paise,
            )
        return out


@dataclass
class StripeStyle:
    """B3 — a Western retry playbook, transplanted unchanged.

    Two things about it are wrong here, and both are the point:

    * it schedules from *now* rather than into the notification aperture, so
      short-dated attempts are simply not available (C5)
    * it is blind to peak windows, so a share of its chosen times are illegal (C2)

    It also proposes up to eight attempts where four are permitted (C1). The
    harness records every refusal and executes none of them, so B3 is measured
    on what it could lawfully achieve — which is the honest comparison, and
    still leaves the violation count on the table.
    """

    calendar: Calendar
    #: Roughly the published Smart Retries shape: eight attempts over two weeks.
    offsets_hours: tuple[int, ...] = (1, 6, 24, 48, 96, 168, 240, 336)
    name: str = "B3 · Stripe-style, 8 attempts / 2 weeks"
    _origin_failure: dict[str, int] = field(default_factory=dict)
    _index: dict[str, int] = field(default_factory=dict)

    def reset(self, seed: int) -> None:
        self._origin_failure.clear()
        self._index.clear()

    def plan(self, batch: Sequence[Candidate], now: datetime) -> Mapping[str, Action]:
        out: dict[str, Action] = {}
        for c in batch:
            anchor = self._origin_failure.setdefault(c.mandate_id, c.last_failure_slot)
            i = self._index.get(c.mandate_id, 0)
            if i >= len(self.offsets_hours):
                continue
            target = anchor + self.offsets_hours[i] * 2
            if target < c.now_slot:
                # The moment has passed. A card-rails policy would simply fire.
                self._index[c.mandate_id] = i + 1
                target = c.now_slot
            elif target > c.now_slot + MAX_LEAD_SLOTS:
                continue                              # not yet its turn
            else:
                self._index[c.mandate_id] = i + 1

            # Deliberately no aperture snapping and no peak-window check.
            out[c.mandate_id] = Commit(
                execute_at=self.calendar.time_of(min(target, self.calendar.horizon_slots - 1)),
                amount_paise=c.state.amount_due_paise,
            )
        return out


@dataclass
class StopEverything:
    """A control. Refuses immediately, so the stop-list accounting can be checked
    against a policy whose stop list is the entire batch."""

    calendar: Calendar
    name: str = "control · stop everything"

    def reset(self, seed: int) -> None:
        return None

    def plan(self, batch: Sequence[Candidate], now: datetime) -> Mapping[str, Action]:
        return {c.mandate_id: Stop(reason="control") for c in batch}


def _schedule(calendar: Calendar, now_slot: int, target_slot: int) -> int | None:
    """Snap a desired execution time into the legal aperture, or decline.

    Returns None when the target is still too far out to commit to — under C5 a
    notification raised more than 48h ahead is refused, so the correct action is
    to wait rather than to commit early.
    """
    lead = target_slot - now_slot
    if lead > MAX_LEAD_SLOTS:
        return None
    slot = max(target_slot, now_slot + MIN_LEAD_SLOTS)
    legal = calendar.next_legal(slot, limit=2 * 48)
    if legal is None or legal - now_slot > MAX_LEAD_SLOTS:
        return None
    return legal
