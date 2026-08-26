"""A clairvoyant policy that still obeys the law.

Beating a baseline by twelve percent says nothing about how much was left on the
table. This says how much there was.

The oracle sees the customer's balance trajectory — the one deliberate exception
to the rule that a policy cannot reach latent state — and is bound by every
constraint in `COMPLIANCE.md`: the retry cap, the peak windows, the two-sided
notification aperture, the serialization rule. It therefore measures the ceiling
for *any lawful policy*, not the ceiling for a policy with no rules.

The number to report from it is recovery efficiency:

    (policy − B1) / (oracle − B1)

which answers "how much of the achievable did we achieve" rather than "how much
better than the heuristic did we look".

::: honesty
This is a strong achievable policy, not a proven supremum. It commits to the
single best reachable slot rather than searching every sequence of up to three
commitments, so a cleverer clairvoyant could do slightly better on mandates
where partial collections across several attempts beat one full collection. It
is an upper bound on what a *single well-timed attempt* can do, and a close
lower bound on the true optimum. Reported as such rather than as "optimal".
:::
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Mapping, Sequence

from ..core.money import Paise
from ..core.types import Action, Commit, Stop
from ..sim.world import Doom, World
from .policy import Calendar, Candidate

MIN_LEAD_SLOTS: Final[int] = 48
MAX_LEAD_SLOTS: Final[int] = 96


@dataclass
class ClairvoyantOracle:
    """Knows the future. Still cannot break the rules."""

    world: World
    calendar: Calendar
    name: str = "oracle · clairvoyant, lawful"
    #: Give up on mandates nothing could collect from, so the oracle's stop list
    #: is the true floor for what any policy should refuse.
    stop_on_hopeless: bool = True
    _customer: dict[str, int] = field(default_factory=dict)
    _best: dict[str, tuple[int, int, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._customer = {m.mandate_id: m.customer for m in self.world.mandates}
        self._variable = {
            m.mandate_id: m.variable_amount_allowed for m in self.world.mandates
        }
        self._doom = {m.mandate_id: m.doom for m in self.world.mandates}
        self._cycle_end = {m.mandate_id: m.cycle_end_slot for m in self.world.mandates}

    def reset(self, seed: int) -> None:
        self._best.clear()

    def plan(self, batch: Sequence[Candidate], now: datetime) -> Mapping[str, Action]:
        out: dict[str, Action] = {}
        for c in batch:
            mid = c.mandate_id
            if self._doom[mid] in (Doom.ACCOUNT_CLOSED, Doom.ALREADY_REVOKED, Doom.VALIDITY_LAPSED):
                if self.stop_on_hopeless:
                    out[mid] = Stop(reason=f"clairvoyant: {self._doom[mid].value}")
                continue

            outstanding = c.state.amount_due_paise
            if outstanding <= 0:
                continue

            best_slot, best_take = self._best_reachable(c, outstanding)
            if best_slot is None or best_take <= 0:
                if self.stop_on_hopeless and self._nothing_left(c, outstanding):
                    out[mid] = Stop(reason="clairvoyant: no reachable slot collects")
                continue

            lead = best_slot - c.now_slot
            if lead < MIN_LEAD_SLOTS or lead > MAX_LEAD_SLOTS:
                continue                      # wait until the aperture opens on it

            out[mid] = Commit(
                execute_at=self.calendar.time_of(best_slot), amount_paise=best_take
            )
        return out

    # -- internals ---------------------------------------------------------

    def _best_reachable(
        self, c: Candidate, outstanding: Paise
    ) -> tuple[int | None, Paise]:
        """The legal slot in the remaining window that collects the most.

        Cached per mandate. The balance path is fixed by the seed, so the answer
        only changes when the outstanding amount changes or when the chosen slot
        has fallen out of reach — which is exactly when the cache is invalidated.
        Without this the scan runs once per candidate per epoch and the suite
        takes minutes instead of seconds.
        """
        cached = self._best.get(c.mandate_id)
        if cached is not None:
            out_cached, slot_cached, take_cached = cached
            if out_cached == outstanding and slot_cached >= c.now_slot + MIN_LEAD_SLOTS:
                return slot_cached, take_cached

        customer = self._customer[c.mandate_id]
        variable = self._variable[c.mandate_id]
        lo = c.now_slot + MIN_LEAD_SLOTS
        hi = min(self._cycle_end[c.mandate_id], self.calendar.horizon_slots - 1)

        best_slot: int | None = None
        best_take: Paise = 0
        for slot in range(lo, hi + 1):
            if not self.calendar.is_legal_execution(slot):
                continue
            balance = self.world.balance_at(customer, slot)
            take = min(outstanding, balance) if variable else (
                outstanding if balance >= outstanding else 0
            )
            if take > best_take:
                best_take, best_slot = take, slot
                if take >= outstanding:
                    break                     # nothing beats collecting in full
        if best_slot is not None:
            self._best[c.mandate_id] = (outstanding, best_slot, best_take)
        return best_slot, best_take

    def _nothing_left(self, c: Candidate, outstanding: Paise) -> bool:
        slot, take = self._best_reachable(c, outstanding)
        return slot is None or take <= 0
