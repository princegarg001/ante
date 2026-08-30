"""B2 — greedy expected value. The ablation that isolates the allocator.

B2 uses the calibrated survival model to pick the best `(slot, amount)` pair
available right now, and commits to it immediately. It has everything the
allocator has *except* the allocation: no budget reasoning, no option value, no
shadow price for a scarce execution window, no notion that a slot spent here is
a slot not spent elsewhere.

That makes the ladder read cleanly:

    B1 → B2      what the probability model is worth
    B2 → policy  what treating retries as a scarce, priced resource is worth

Without B2, a gain over B1 could be entirely the model, and the central claim of
this project — that the interesting problem is allocation rather than timing —
would be unsupported by its own evidence.

"Greedy" here means *myopic about scarcity*, not *impatient*. B2 scores every
legal slot in the remaining cycle and commits to the best one as soon as the
notification aperture opens on it. What it never does is reason about the slot
being scarce: it does not price an attempt, weigh the option value of the
mandate, or consider that a window it wants is one another mandate also wants.

The first version of this baseline could only see 24–48 hours ahead and
committed immediately, so its three attempts all landed within five days of the
failure. That made it lose heavily to B1 — but for the wrong reason. B1 reaches
+168h and catches the next payday, and the fitted pay-cycle profile shows
success falling to near zero eight days after a credit and rising again around
day 26. B2 was losing on *reach*, not on allocation, which would have made the
eventual comparison against the allocator meaningless.

A baseline has to lose for the reason you claim it loses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Mapping, Sequence

import numpy as np

from ..belief.filter import PhaseBelief, PhaseProfile
from ..core.money import Paise
from ..core.types import TERMINAL_CAUSES, Action, Commit, Stop
from ..predict.features import FeatureContext, IssuerTracker, extract
from ..predict.model import TrainedModel
from .policy import Calendar, Candidate

MIN_LEAD_SLOTS: Final[int] = 48
MAX_LEAD_SLOTS: Final[int] = 96

#: Every fourth slot. Finer buys nothing: within two hours the model's features
#: barely move, and the scan runs over the whole remaining cycle.
SLOT_STRIDE: Final[int] = 4

#: How far ahead to score. Long enough to reach the next pay cycle, which is the
#: reach that matters in this market.
HORIZON_SLOTS: Final[int] = 30 * 48

AMOUNT_RATIOS: Final[tuple[float, ...]] = (1.0, 0.8, 0.6, 0.45, 0.3)

#: Below this the attempt is not worth the presentation cost plus the risk to
#: the mandate. Deliberately crude — pricing this properly is the allocator's job.
MIN_EV_PAISE: Final[Paise] = 300


@dataclass
class GreedyEV:
    model: TrainedModel
    calendar: Calendar
    issuer_of: dict[str, str] = field(default_factory=dict)
    #: B2 carries the same pay-cycle posterior the allocator does.
    #:
    #: This is not generosity, it is the whole point of the ablation. The model
    #: is trained with the belief score and its entropy among the features, so a
    #: B2 that passed zeros would be handicapped on *information* — and the gap
    #: to the allocator would then partly measure "I know something you do not"
    #: rather than "treating slots as scarce is worth something". The comparison
    #: has to differ in exactly one thing.
    profile: PhaseProfile | None = None
    name: str = "B2 · greedy EV, no budget reasoning"

    _issuers: IssuerTracker = field(default_factory=IssuerTracker)
    _reference: dict[str, Paise] = field(default_factory=dict)
    _last_failure: dict[str, datetime] = field(default_factory=dict)
    _decided_at_attempt: dict[str, int] = field(default_factory=dict)
    _target: dict[str, tuple[int, int]] = field(default_factory=dict)
    _beliefs: dict[str, PhaseBelief] = field(default_factory=dict)

    def reset(self, seed: int) -> None:
        self._issuers = IssuerTracker()
        self._reference.clear()
        self._last_failure.clear()
        self._decided_at_attempt.clear()
        self._target.clear()
        self._beliefs.clear()

    # -- policy ------------------------------------------------------------

    def plan(self, batch: Sequence[Candidate], now: datetime) -> Mapping[str, Action]:
        out: dict[str, Action] = {}
        pending: list[tuple[Candidate, np.ndarray, list[tuple[int, float]]]] = []

        for c in batch:
            if c.state.cause in TERMINAL_CAUSES:
                out[c.mandate_id] = Stop(reason=f"terminal cause {c.state.cause.value}")
                continue
            # Greedy decides once per attempt, not once per epoch. Re-scoring an
            # unchanged mandate every four hours would be 50x the work for the
            # same answer.
            target = self._target.get(c.mandate_id)
            if target is not None:
                slot, amount = target
                lead = slot - c.now_slot
                if lead < MIN_LEAD_SLOTS:
                    self._target.pop(c.mandate_id)      # missed it; re-score
                elif lead <= MAX_LEAD_SLOTS:
                    self._target.pop(c.mandate_id)
                    self._decided_at_attempt[c.mandate_id] = c.state.attempts_used
                    out[c.mandate_id] = Commit(
                        execute_at=self.calendar.time_of(slot), amount_paise=amount
                    )
                    continue
                else:
                    continue                            # still waiting

            if self._decided_at_attempt.get(c.mandate_id) == c.state.attempts_used:
                continue

            options = self._options(c)
            if not options:
                continue
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
            rows = np.vstack(
                [
                    extract(
                        FeatureContext(
                            state=c.state,
                            execute_at=self.calendar.time_of(slot),
                            amount_paise=max(1, int(round(c.state.amount_due_paise * r))),
                            reference_amount_paise=reference,
                            now=now,
                            last_failure_at=self._last_failure[c.mandate_id],
                            issuers=self._issuers,
                            belief_day_score=(
                                belief.probability(self.calendar.time_of(slot).day)
                                if belief else 0.0
                            ),
                            belief_entropy_bits=belief.entropy_bits if belief else 0.0,
                        )
                    )
                    for slot, r in options
                ]
            )
            pending.append((c, rows, options))

        if not pending:
            return out

        # One batched call for the whole epoch.
        stacked = np.vstack([rows for _, rows, _ in pending])
        probabilities = self.model.predict(stacked)

        cursor = 0
        for c, rows, options in pending:
            n = rows.shape[0]
            p = probabilities[cursor : cursor + n]
            cursor += n

            amounts = np.array(
                [max(1, int(round(c.state.amount_due_paise * r))) for _, r in options],
                dtype=float,
            )
            ev = amounts * p
            best = int(np.argmax(ev))
            self._decided_at_attempt[c.mandate_id] = c.state.attempts_used

            if ev[best] < MIN_EV_PAISE:
                out[c.mandate_id] = Stop(
                    reason=f"greedy: best expected value {ev[best] / 100:.2f} below floor"
                )
                continue

            slot, _ = options[best]
            lead = slot - c.now_slot
            if lead > MAX_LEAD_SLOTS:
                # The best slot is real but not yet committable (C5). Wait, and
                # re-score when the aperture opens on it. Waiting for a slot you
                # have already chosen is not budget reasoning.
                self._decided_at_attempt.pop(c.mandate_id, None)
                self._target[c.mandate_id] = (slot, int(amounts[best]))
                continue

            out[c.mandate_id] = Commit(
                execute_at=self.calendar.time_of(slot),
                amount_paise=int(amounts[best]),
            )
        return out

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

    # -- internals ---------------------------------------------------------

    def _options(self, c: Candidate) -> list[tuple[int, float]]:
        """Legal (slot, ratio) pairs anywhere in the remaining cycle.

        Scored over the whole horizon rather than only the current aperture, so
        the choice is about *where the money is* rather than about how far ahead
        the policy happens to be able to see.
        """
        ratios = AMOUNT_RATIOS if c.state.variable_amount_allowed else (1.0,)
        cycle_end_slot = min(
            self.calendar.slot_of(c.state.cycle_end),
            c.now_slot + HORIZON_SLOTS,
            self.calendar.horizon_slots - 1,
        )
        options: list[tuple[int, float]] = []
        for slot in range(c.now_slot + MIN_LEAD_SLOTS, cycle_end_slot + 1, SLOT_STRIDE):
            if not self.calendar.is_legal_execution(slot):
                continue
            options.extend((slot, r) for r in ratios)
        return options
