"""Training data, and why it has to be collected deliberately.

The obvious source of labelled outcomes is the logs of whatever policy is
already running. That does not work here. `B1` only ever attempts at +24h, +72h
and +168h, always for the full amount — so its logs contain no information about
what happens at hour 6 of day 8, or about collecting 40% of the debit. A model
fitted on them would extrapolate confidently into regions it has never seen, and
the allocator would then optimise against that extrapolation.

So training data comes from an **exploration policy**: legal commitments placed
at randomised times, at randomised delays, and — where the mandate permits it —
at randomised fractions of the amount.

The delay matters more than it looks. An exploration policy that commits at the
first opportunity burns all three attempts within about five days of the due
date, so the logs only ever contain days 2 to 7 of the month. A model fitted on
that has never seen the 15th or the 25th, and will happily extrapolate across a
fortnight it knows nothing about. Exploring *when to wait* is what gives the
day-of-month feature anything to learn from. That is a deliberate choice with a real
cost — exploration collects less than exploitation — and it is the honest way to
bootstrap. In production the equivalent is either a holdout arm or the natural
variation already present in historical retry timing.

Exploration runs on the **training seeds only**. Evaluation seeds are never
touched, and the split is asserted rather than promised.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Mapping, Sequence

import numpy as np

from ..core.clock import IST, SLOTS_PER_DAY
from ..core.money import Paise
from ..core.types import Action, Commit, MandateState
from ..eval.policy import Calendar, Candidate
from .features import FeatureContext, IssuerTracker, extract

MIN_LEAD_SLOTS: Final[int] = 48
MAX_LEAD_SLOTS: Final[int] = 96

#: Amount fractions to explore on variable-amount mandates. Weighted towards the
#: full amount so exploration does not spend the whole budget on partials.
AMOUNT_RATIOS: Final[tuple[float, ...]] = (1.0, 1.0, 1.0, 0.8, 0.6, 0.45, 0.3, 0.2)

#: Days to wait before committing, drawn per attempt. Spreads executions across
#: the billing month instead of clustering them against the due date.
MAX_EXPLORE_DELAY_DAYS: Final[int] = 16


@dataclass
class ExplorationPolicy:
    """Places legal commitments at randomised times and amounts, and records them.

    Every proposal is constructed to satisfy the constraint layer — an
    exploration policy that gets vetoed collects nothing.
    """

    calendar: Calendar
    seed: int = 0
    name: str = "explore · randomised legal commitments"

    rows: list[np.ndarray] = field(default_factory=list)
    labels: list[int] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)

    _rng: np.random.Generator = field(init=False)
    _issuers: IssuerTracker = field(default_factory=IssuerTracker)
    #: mandate_id -> issuer code, supplied by the collector.
    issuer_of: dict[str, str] = field(default_factory=dict)
    _pending: dict[str, tuple[int, np.ndarray]] = field(default_factory=dict)
    _reference: dict[str, Paise] = field(default_factory=dict)
    _last_failure: dict[str, datetime] = field(default_factory=dict)
    _release_slot: dict[tuple[str, int], int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._rng = np.random.default_rng(self.seed)

    def reset(self, seed: int) -> None:
        self._rng = np.random.default_rng(seed)
        self._issuers = IssuerTracker()
        self._pending.clear()
        self._reference.clear()
        self._last_failure.clear()
        self._release_slot.clear()

    # -- policy ------------------------------------------------------------

    def plan(self, batch: Sequence[Candidate], now: datetime) -> Mapping[str, Action]:
        out: dict[str, Action] = {}
        for c in batch:
            # Draw a delay once per (mandate, attempt) and hold it, so the
            # execution lands somewhere across the month rather than always
            # against the due date.
            key = (c.mandate_id, c.state.attempts_used)
            release = self._release_slot.get(key)
            if release is None:
                delay_days = int(self._rng.integers(0, MAX_EXPLORE_DELAY_DAYS))
                release = c.last_failure_slot + delay_days * SLOTS_PER_DAY
                self._release_slot[key] = release
            if c.now_slot < release:
                continue

            slot = self._random_legal_slot(c.now_slot)
            if slot is None:
                continue

            reference = self._reference.setdefault(
                c.mandate_id, max(1, c.state.amount_due_paise)
            )
            ratio = (
                float(self._rng.choice(AMOUNT_RATIOS))
                if c.state.variable_amount_allowed
                else 1.0
            )
            amount = max(1, int(round(c.state.amount_due_paise * ratio)))
            self._last_failure.setdefault(
                c.mandate_id, self.calendar.time_of(c.last_failure_slot)
            )

            row = extract(
                FeatureContext(
                    state=c.state,
                    execute_at=self.calendar.time_of(slot),
                    amount_paise=amount,
                    reference_amount_paise=reference,
                    now=now,
                    last_failure_at=self._last_failure[c.mandate_id],
                    issuers=self._issuers,
                )
            )
            self._pending[c.mandate_id] = (slot, row)
            out[c.mandate_id] = Commit(
                execute_at=self.calendar.time_of(slot), amount_paise=amount
            )
        return out

    def observe(
        self, mandate_id: str, executed_at: datetime, amount: Paise, ok: bool
    ) -> None:
        """Label the row that produced this presentation."""
        pend = self._pending.pop(mandate_id, None)
        self._issuers_observe(mandate_id, ok)
        if pend is None:
            return
        _, row = pend
        self.rows.append(row)
        self.labels.append(int(ok))
        self.groups.append(mandate_id)
        if not ok:
            self._last_failure[mandate_id] = executed_at

    # -- internals ---------------------------------------------------------

    def _issuers_observe(self, mandate_id: str, ok: bool) -> None:
        issuer = self.issuer_of.get(mandate_id)
        if issuer is not None:
            self._issuers.observe(issuer, ok)

    def _random_legal_slot(self, now_slot: int) -> int | None:
        lo = now_slot + MIN_LEAD_SLOTS
        hi = now_slot + MAX_LEAD_SLOTS
        order = self._rng.permutation(np.arange(lo, hi + 1))
        for slot in order:
            if self.calendar.is_legal_execution(int(slot)):
                return int(slot)
        return None


@dataclass(frozen=True, slots=True)
class Dataset:
    X: np.ndarray
    y: np.ndarray
    groups: np.ndarray
    seeds: tuple[int, ...]

    @property
    def n(self) -> int:
        return int(self.X.shape[0])

    @property
    def positive_rate(self) -> float:
        return float(self.y.mean()) if self.n else 0.0


def collect(
    seeds: Sequence[int],
    config,
    origin: datetime | None = None,
) -> Dataset:
    """Run exploration across the training seeds and return the labelled rows."""
    from ..eval.harness import run_policy
    from ..sim.issuer import ISSUERS
    from ..sim.world import World

    origin = origin or datetime(2026, 9, 1, 0, 0, tzinfo=IST)
    rows: list[np.ndarray] = []
    labels: list[int] = []
    groups: list[str] = []

    for seed in seeds:
        world = World.generate(seed, origin, config)
        cal = Calendar(origin=world.origin, horizon_slots=world.horizon_slots)
        policy = ExplorationPolicy(
            cal,
            seed=seed,
            issuer_of={m.mandate_id: ISSUERS[m.issuer].code for m in world.mandates},
        )
        run_policy(policy, world)
        rows.extend(policy.rows)
        labels.extend(policy.labels)
        groups.extend(f"{seed}:{g}" for g in policy.groups)

    X = np.vstack(rows) if rows else np.empty((0, 0))
    return Dataset(
        X=X,
        y=np.asarray(labels, dtype=int),
        groups=np.asarray(groups),
        seeds=tuple(seeds),
    )
