"""The one door every policy goes through.

Baselines, the clairvoyant oracle and the allocator all implement this. That
matters for a reason beyond tidiness: if a baseline could reach the world by a
different route than the policy under test, the comparison would be measuring
the harness as much as the policy.

A policy sees `MandateState` and nothing else. It cannot read a balance, a
liquidity type, or a churn intent, because the world does not expose them —
the oracle is the single deliberate exception, and it is labelled as an upper
bound rather than a competitor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Protocol, Sequence, runtime_checkable

from ..core.clock import SLOTS_PER_DAY, SLOT_MINUTES, is_non_peak, to_ist
from ..core.types import Action, MandateState


@dataclass(frozen=True, slots=True)
class Calendar:
    """Slot arithmetic, and nothing else.

    Policies need to convert between slots and instants and to know which slots
    are legal to execute in. They are given this rather than the world, so that
    reaching for a balance is not merely discouraged but impossible.
    """

    origin: datetime
    horizon_slots: int

    def time_of(self, slot: int) -> datetime:
        return self.origin + timedelta(minutes=SLOT_MINUTES * int(slot))

    def slot_of(self, when: datetime) -> int:
        delta = to_ist(when) - self.origin
        return int(delta.total_seconds() // (SLOT_MINUTES * 60))

    def is_legal_execution(self, slot: int) -> bool:
        return 0 <= slot < self.horizon_slots and is_non_peak(self.time_of(slot))

    def next_legal(self, slot: int, limit: int | None = None) -> int | None:
        """First legal execution slot at or after `slot`, or None within `limit`."""
        stop = min(self.horizon_slots, slot + (limit if limit is not None else 2 * SLOTS_PER_DAY))
        for s in range(max(0, slot), stop):
            if self.is_legal_execution(s):
                return s
        return None


@dataclass(frozen=True, slots=True)
class Candidate:
    """One mandate awaiting a decision at this epoch."""

    mandate_id: str
    state: MandateState
    #: Slot of the most recent failed presentation. The recovery clock starts here.
    last_failure_slot: int
    #: Slot the decision is being taken at. `Commit` targets must land in
    #: [now + 48, now + 96] slots to satisfy the notification aperture (C5).
    now_slot: int


@runtime_checkable
class Policy(Protocol):
    """Propose one action per mandate. The harness decides what is permitted."""

    name: str

    def plan(
        self, batch: Sequence[Candidate], now: datetime
    ) -> Mapping[str, Action]:
        """Return an action for each mandate the policy wants to act on.

        Mandates omitted from the mapping are treated as `Wait`. Actions the
        constraint layer refuses are recorded as attempted violations and not
        executed — which is how a policy designed for another market gets
        measured here without being given a pass.
        """
        ...

    def reset(self, seed: int) -> None:
        """Clear per-run state. Called before every seed so a policy cannot
        carry information between runs."""
        ...
