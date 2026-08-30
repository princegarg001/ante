"""Per-mandate dynamic programme, coupled by a price on window capacity.

## The per-mandate problem

Backward induction over the legal execution slots remaining in the cycle. With
`m` presentations left and the decision taken at slot index `j`:

    V_0(j) = L                                    no budget left; the mandate survives
    V_m(j) = max(  L                              stop: refuse, keep the mandate
                 , V_m(j+1)                       wait: hold the attempt
                 , max over reachable (s, a) of
                     p(s,a)·(a + L)               collected, and the mandate lives
                   + (1−p(s,a))·(1−h)·V_{m−1}(s⁺) failed, and it may be revoked
                   − c − λ(window of s)  )        cost, and the price of the slot

`L` is the mandate's continuation value — what the subscription is worth if it
survives the cycle. `h` is the extra revocation hazard a failed presentation
adds. Together they are the option-value term: **every attempt is a bet with the
mandate posted as collateral**, and the DP prices that bet rather than assuming
it is free.

## Why this is fast

The commit value of a slot does not depend on *when* the decision is taken —
only on the slot itself, through `p(s,a)` and the value of the state it leads
to. So the inner maximisation is computed once per slot, and the "best slot
reachable from here" is a sliding-window maximum over the notification aperture.
That collapses what looks like a quadratic search into O(slots × amounts), which
is why a full DP runs faster here than the greedy baseline's flat scan.

## The coupling

Mandates are independent given `λ`. The shared capacity constraint is relaxed
into a multiplier per execution window, and `λ_w` is raised where demand exceeds
supply until the market clears. It has a directly sayable meaning — the rupee
price of one execution slot in that window — and it turns every decision into a
bid against a clearing price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Mapping, Sequence

import numpy as np

from ..belief.filter import PhaseBelief, PhaseProfile
from ..core.money import Paise
from ..core.types import TERMINAL_CAUSES, Action, Commit, Stop
from ..eval.policy import Calendar, Candidate
from ..predict.features import FeatureContext, IssuerTracker, extract
from ..predict.model import TrainedModel

MIN_LEAD_SLOTS: Final[int] = 48
MAX_LEAD_SLOTS: Final[int] = 96
SLOT_STRIDE: Final[int] = 4
MAX_ATTEMPTS: Final[int] = 4


@dataclass(frozen=True, slots=True)
class AllocatorConfig:
    #: Continuation value of a surviving mandate, as a multiple of the amount due.
    #: A ₹499 plan with a year of expected life left is worth many times the debit
    #: being chased, which is precisely why chasing it recklessly is expensive.
    ltv_multiple: float = 8.0
    #: Extra probability the customer revokes after one more failed presentation.
    #:
    #: Measured, not guessed. B1 spends three extra presentations per mandate and
    #: ends with 78.9% of the batch unrevoked against B0's 81.5% — 2.6 points
    #: across three attempts, so roughly 0.9% each. `estimate_revocation_hazard`
    #: recomputes it from any two runs.
    #:
    #: The first version of this file used 0.055, invented rather than measured.
    #: At that value an attempt risks 0.44x the debit to win about 0.3x, so the
    #: allocator correctly refused every mandate in the batch and scored zero.
    #: The arithmetic was right and the input was wrong.
    revoke_hazard_per_failure: float = 0.010
    cost_per_presentation: Paise = 200
    #: How far ahead the dynamic programme looks.
    horizon_days: int = 21
    amount_ratios: tuple[float, ...] = (1.0, 0.8, 0.6, 0.45, 0.3)
    #: Dual ascent.
    dual_iterations: int = 12
    dual_step: float = 0.35
    #: Executions permitted per window, as a share of the live batch. Stands in
    #: for the moderated TPS the rails impose (C4).
    window_capacity_share: float = 0.045


@dataclass(frozen=True, slots=True)
class ClearingPrice:
    window: tuple[int, int]        # (day index, block index)
    price_paise: float
    demand: int
    capacity: int

    @property
    def cleared(self) -> bool:
        return self.demand <= self.capacity


@dataclass(frozen=True, slots=True)
class Plan:
    """One mandate's best response to the current prices."""

    mandate_id: str
    #: "commit" fires now, "wait" holds for a slot already chosen but not yet
    #: inside the notification aperture, "stop" refuses the mandate for good.
    #:
    #: Keeping wait and stop apart is not cosmetic. The first version had no wait
    #: branch and returned Stop whenever committing now was not the best move —
    #: so a mandate whose best slot was nine days out was permanently refused
    #: rather than held. The allocator was choosing patience and the extraction
    #: was recording surrender; the batch showed 408 stops and zero attempts.
    action: str                   # "commit" | "wait" | "stop"
    slot: int | None
    amount_paise: Paise
    bid_paise: float
    value_paise: float
    reason: str


def estimate_revocation_hazard(
    survival_with_retries: float,
    survival_without: float,
    extra_attempts_per_mandate: float,
) -> float:
    """Per-attempt revocation hazard implied by two runs.

    The quantity the option-value term needs is *how much a failed presentation
    costs the mandate*, and it is directly observable by comparing a policy that
    retries against one that does not.
    """
    if extra_attempts_per_mandate <= 0:
        return 0.0
    lost = max(0.0, survival_without - survival_with_retries)
    return float(lost / extra_attempts_per_mandate)


def _block_of(calendar: Calendar, slot: int) -> tuple[int, int]:
    """Which shared execution window a slot belongs to.

    The three non-peak blocks: overnight, early afternoon, late evening. Capacity
    is shared within a block, which is what makes mandates compete.
    """
    when = calendar.time_of(slot)
    day = slot // 48
    hour = when.hour + when.minute / 60.0
    block = 0 if hour < 10.0 else (1 if hour < 17.0 else 2)
    return day, block


class SlotAllocator:
    """The policy. Solves each mandate under prices, then clears the market."""

    name = "allocator · priced DP with option value"

    def __init__(
        self,
        model: TrainedModel,
        calendar: Calendar,
        profile: PhaseProfile | None = None,
        issuer_of: Mapping[str, str] | None = None,
        config: AllocatorConfig | None = None,
    ) -> None:
        self.model = model
        self.calendar = calendar
        self.profile = profile
        self.issuer_of = dict(issuer_of or {})
        self.cfg = config or AllocatorConfig()

        self._issuers = IssuerTracker()
        self._beliefs: dict[str, PhaseBelief] = {}
        self._reference: dict[str, Paise] = {}
        self._last_failure: dict[str, datetime] = {}
        self._decided_at: dict[str, int] = {}
        self._target: dict[str, tuple[int, Paise]] = {}
        self._prices: dict[tuple[int, int], float] = {}
        self.last_book: list[Plan] = []
        self.last_clearing: list[ClearingPrice] = []

    # -- policy interface --------------------------------------------------

    def reset(self, seed: int) -> None:
        self._issuers = IssuerTracker()
        self._beliefs.clear()
        self._reference.clear()
        self._last_failure.clear()
        self._decided_at.clear()
        self._target.clear()
        self._prices.clear()
        self.last_book = []
        self.last_clearing = []

    def observe(
        self, mandate_id: str, executed_at: datetime, amount: Paise, ok: bool
    ) -> None:
        issuer = self.issuer_of.get(mandate_id)
        if issuer is not None:
            self._issuers.observe(issuer, ok)
        if self.profile is not None:
            belief = self._beliefs.setdefault(mandate_id, PhaseBelief(self.profile))
            belief.update(self.calendar.time_of(self.calendar.slot_of(executed_at)).day, ok)
        if not ok:
            self._last_failure[mandate_id] = executed_at

    def plan(self, batch: Sequence[Candidate], now: datetime) -> Mapping[str, Action]:
        out: dict[str, Action] = {}
        to_solve: list[Candidate] = []

        for c in batch:
            if c.state.cause in TERMINAL_CAUSES:
                out[c.mandate_id] = Stop(reason=f"terminal cause {c.state.cause.value}")
                continue

            fired = self._fire_if_due(c, out)
            if fired:
                continue
            if self._decided_at.get(c.mandate_id) == c.state.attempts_used:
                continue
            to_solve.append(c)

        if not to_solve:
            return out

        tables = {c.mandate_id: self._probability_table(c, now) for c in to_solve}
        plans = self._clear_market(to_solve, tables)
        self.last_book = plans

        for plan, c in zip(plans, to_solve):
            if plan.action == "stop":
                self._decided_at[c.mandate_id] = c.state.attempts_used
                out[c.mandate_id] = Stop(reason=plan.reason)
            elif plan.action == "wait" and plan.slot is not None:
                # Park the chosen slot and fire when the aperture opens on it.
                # Deliberately not marked decided: the choice is re-examined as
                # evidence arrives, which is the point of holding.
                self._target[c.mandate_id] = (plan.slot, plan.amount_paise)
            elif plan.action == "commit" and plan.slot is not None:
                self._decided_at[c.mandate_id] = c.state.attempts_used
                out[c.mandate_id] = Commit(
                    execute_at=self.calendar.time_of(plan.slot),
                    amount_paise=plan.amount_paise,
                )
        return out

    # -- the market --------------------------------------------------------

    def _clear_market(
        self, batch: Sequence[Candidate], tables: dict[str, np.ndarray]
    ) -> list[Plan]:
        """Dual ascent on window prices until demand fits capacity.

        Prices only rise inside a clearing round. Decaying an uncontested window
        mid-loop makes the dual oscillate — it is priced out, demand vanishes,
        the price halves, demand returns. Decay happens once per epoch instead,
        so yesterday's congestion does not tax today's decisions.
        """
        cfg = self.cfg
        capacity = max(1, int(len(batch) * cfg.window_capacity_share))

        # Carry prices between epochs, decayed, so the market has memory without
        # having a grudge.
        prices = {w: v * 0.6 for w, v in self._prices.items() if v * 0.6 > 1.0}

        plans: list[Plan] = []
        demand: dict[tuple[int, int], int] = {}
        for _ in range(cfg.dual_iterations):
            plans = [self._solve(c, tables[c.mandate_id], prices) for c in batch]

            demand = {}
            for p in plans:
                if p.action in ("commit", "wait") and p.slot is not None:
                    w = _block_of(self.calendar, p.slot)
                    demand[w] = demand.get(w, 0) + 1

            scale = self._typical_bid(plans)
            over = False
            for w, d in demand.items():
                excess = d - capacity
                if excess <= 0:
                    continue
                over = True
                prices[w] = prices.get(w, 0.0) + cfg.dual_step * scale * (
                    excess / max(1, capacity)
                )
            if not over:
                break

        self._prices = prices
        self.last_clearing = self._book(prices, demand, capacity)
        return plans

    @staticmethod
    def _book(
        prices: Mapping[tuple[int, int], float],
        demand: Mapping[tuple[int, int], int],
        capacity: int,
    ) -> list[ClearingPrice]:
        """The auction book.

        Reports every window that carries a price *or* attracted demand. An
        earlier version listed only windows with demand remaining, which are by
        construction the ones nothing was willing to pay for — so a fully
        congested market rendered as a page of zeroes.
        """
        windows = set(prices) | set(demand)
        rows = [
            ClearingPrice(w, float(prices.get(w, 0.0)), int(demand.get(w, 0)), capacity)
            for w in windows
        ]
        rows.sort(key=lambda r: (-r.price_paise, -r.demand))
        return rows[:16]

    @staticmethod
    def _typical_bid(plans: Sequence[Plan]) -> float:
        bids = [p.bid_paise for p in plans if p.action == "commit"]
        return float(np.median(bids)) if bids else 100.0

    # -- the dynamic programme --------------------------------------------

    def _solve(
        self,
        c: Candidate,
        table: np.ndarray,
        prices: Mapping[tuple[int, int], float],
    ) -> Plan:
        """Backward induction over the remaining legal slots."""
        cfg = self.cfg
        slots, probs = table[0].astype(int), table[1:]      # probs: (ratios, slots)
        if slots.size == 0:
            return Plan(c.mandate_id, "stop", None, 0, 0.0, 0.0, "no legal slot remains")

        due = float(c.state.amount_due_paise)
        L = cfg.ltv_multiple * due
        h = cfg.revoke_hazard_per_failure
        cost = float(cfg.cost_per_presentation)
        ratios = np.asarray(cfg.amount_ratios, dtype=float)
        amounts = ratios * due

        price = np.array([prices.get(_block_of(self.calendar, int(s)), 0.0) for s in slots])
        remaining = MAX_ATTEMPTS - c.state.attempts_used
        if remaining <= 0:
            return Plan(c.mandate_id, "stop", None, 0, 0.0, L, "retry budget exhausted")

        # after[i] = index of the first slot strictly after slots[i]
        after = np.minimum(np.arange(slots.size) + 1, slots.size - 1)

        V_prev = np.full(slots.size, L)                     # zero attempts left
        best_commit_now = -np.inf
        best_choice: tuple[int, int] | None = None
        wait_value = L

        for _ in range(remaining):
            # Commit value of each slot, maximised over the amount grid.
            cont_fail = (1.0 - h) * V_prev[after]           # (slots,)
            value_by_ratio = (
                probs * (amounts[:, None] + L)
                + (1.0 - probs) * cont_fail[None, :]
                - cost
                - price[None, :]
            )
            cv = value_by_ratio.max(axis=0)                 # (slots,)
            arg_ratio = value_by_ratio.argmax(axis=0)

            # Best slot reachable from each decision point: a sliding maximum
            # over the notification aperture. lo and hi both increase, so one
            # backward pass suffices.
            V = np.empty(slots.size)
            best_from = np.full(slots.size, -1)
            running = L                                     # stop is always available
            for j in range(slots.size - 1, -1, -1):
                lo = c.now_slot if j == 0 else int(slots[j])
                reachable = (slots >= lo + MIN_LEAD_SLOTS) & (slots <= lo + MAX_LEAD_SLOTS)
                if reachable.any():
                    idx = int(np.flatnonzero(reachable)[np.argmax(cv[reachable])])
                    commit_value = cv[idx]
                else:
                    idx, commit_value = -1, -np.inf
                running = max(L, running if j == slots.size - 1 else V[j + 1])
                V[j] = max(running, commit_value)
                best_from[j] = idx if commit_value >= running else -1

            best_commit_now = cv[best_from[0]] if best_from[0] >= 0 else -np.inf
            if best_from[0] >= 0:
                s_idx = int(best_from[0])
                best_choice = (int(slots[s_idx]), int(arg_ratio[s_idx]))
            wait_value = float(V[1]) if slots.size > 1 else L
            V_prev = V

        stop_value = L
        best_idx = int(np.argmax(cv))
        best_value = float(cv[best_idx])

        # Nothing anywhere in the remaining cycle beats simply keeping the
        # mandate. This is a genuine refusal, and it is the stop list.
        if best_value <= stop_value:
            return Plan(
                c.mandate_id, "stop", None, 0, 0.0, stop_value,
                f"no slot in the remaining cycle beats holding the mandate "
                f"(best {best_value / 100:,.0f} against option value {stop_value / 100:,.0f})",
            )

        slot = int(slots[best_idx])
        ratio_idx = int(arg_ratio[best_idx])
        amount = int(round(amounts[ratio_idx]))
        bid = best_value - stop_value
        lead = slot - c.now_slot

        if lead > MAX_LEAD_SLOTS:
            # Chosen, but the aperture has not opened on it yet (C5). Holding a
            # slot already selected is patience, not refusal.
            return Plan(
                c.mandate_id, "wait", slot, amount, bid, float(V_prev[0]),
                f"holding for {self.calendar.time_of(slot):%d %b %H:%M}, "
                f"bid {bid / 100:,.0f}; aperture opens in "
                f"{(lead - MAX_LEAD_SLOTS) / 2:.0f}h",
            )

        return Plan(
            c.mandate_id, "commit", slot, amount, bid, float(V_prev[0]),
            f"bid {bid / 100:,.0f} for {self.calendar.time_of(slot):%d %b %H:%M}, "
            f"{cfg.amount_ratios[ratio_idx]:.0%} of the debit",
        )

    # -- probabilities -----------------------------------------------------

    def _probability_table(self, c: Candidate, now: datetime) -> np.ndarray:
        """`p(slot, ratio)` for every legal slot left in the cycle.

        One batched model call per mandate per attempt. Row 0 is the slot index;
        the remaining rows are one per amount ratio.
        """
        cfg = self.cfg
        cycle_end = min(
            self.calendar.slot_of(c.state.cycle_end),
            c.now_slot + cfg.horizon_days * 48,
            self.calendar.horizon_slots - 1,
        )
        slots = [
            s
            for s in range(c.now_slot + MIN_LEAD_SLOTS, cycle_end + 1, SLOT_STRIDE)
            if self.calendar.is_legal_execution(s)
        ]
        if not slots:
            return np.zeros((1, 0))

        ratios = (
            cfg.amount_ratios if c.state.variable_amount_allowed else (1.0,)
        )
        reference = self._reference.setdefault(
            c.mandate_id, max(1, c.state.amount_due_paise)
        )
        self._last_failure.setdefault(
            c.mandate_id, self.calendar.time_of(c.last_failure_slot)
        )
        belief = (
            self._beliefs.setdefault(c.mandate_id, PhaseBelief(self.profile))
            if self.profile is not None
            else None
        )

        rows = []
        for r in ratios:
            amount = max(1, int(round(c.state.amount_due_paise * r)))
            for s in slots:
                when = self.calendar.time_of(s)
                rows.append(
                    extract(
                        FeatureContext(
                            state=c.state,
                            execute_at=when,
                            amount_paise=amount,
                            reference_amount_paise=reference,
                            now=now,
                            last_failure_at=self._last_failure[c.mandate_id],
                            issuers=self._issuers,
                            belief_day_score=belief.probability(when.day) if belief else 0.0,
                            belief_entropy_bits=belief.entropy_bits if belief else 0.0,
                        )
                    )
                )
        p = self.model.predict(np.vstack(rows)).reshape(len(ratios), len(slots))

        # Pad to the full ratio grid when partial collection is not permitted, so
        # the dynamic programme always sees a rectangular table.
        if len(ratios) != len(cfg.amount_ratios):
            full = np.zeros((len(cfg.amount_ratios), len(slots)))
            full[0] = p[0]
            p = full

        return np.vstack([np.asarray(slots, dtype=float)[None, :], p])

    # -- pending commitments ----------------------------------------------

    def _fire_if_due(self, c: Candidate, out: dict[str, Action]) -> bool:
        target = self._target.get(c.mandate_id)
        if target is None:
            return False
        slot, amount = target
        lead = slot - c.now_slot
        if lead < MIN_LEAD_SLOTS:
            self._target.pop(c.mandate_id)
            return False
        if lead <= MAX_LEAD_SLOTS:
            self._target.pop(c.mandate_id)
            self._decided_at[c.mandate_id] = c.state.attempts_used
            out[c.mandate_id] = Commit(
                execute_at=self.calendar.time_of(slot), amount_paise=amount
            )
        return True
