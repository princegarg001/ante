"""The observable feature boundary.

Every feature here is something a merchant genuinely has at decision time: the
mandate's own history, the clock, the amount being attempted, and the issuer's
recent behaviour across the merchant's own book. Nothing is read from the
simulator's latent state — not the balance, not the liquidity type, not the
churn intent.

That restraint is the point. A model given the customer's balance would predict
beautifully and prove nothing, and a reviewer would find it in about a minute.

Two features carry most of the signal, and both are there for a reason the
design predicted rather than discovered:

* `amount_ratio` — the attempted amount over the mandate's normal debit. Varying
  this is what turns a classifier into a survival curve, because
  `P(success | ratio)` *is* the balance's survival function in normalised units.
* `day_of_month` — because income arrives on a calendar and obligations chase it
  within hours, the balance is above the debit for only part of the month.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Sequence

import numpy as np

from ..core.clock import SLOTS_PER_DAY, to_ist
from ..core.money import Paise
from ..core.types import Category, MandateState

FEATURE_NAMES: Final[tuple[str, ...]] = (
    "amount_ratio",
    "log_reference_amount",
    "day_of_month",
    "dom_sin",
    "dom_cos",
    "hour_of_day",
    "hour_sin",
    "hour_cos",
    "attempt_index",
    "hours_since_last_failure",
    "days_to_cycle_end",
    "prior_failures",
    "contacts_used",
    "issuer_success_rate",
    "issuer_attempts_seen",
    "is_variable_amount",
    "is_raised_ceiling_category",
)

N_FEATURES: Final[int] = len(FEATURE_NAMES)


@dataclass(slots=True)
class IssuerTracker:
    """Running success rate per issuer, from the merchant's own observations.

    Legitimately observable: a merchant sees the outcome of every debit it
    presents. Smoothed towards the book-wide rate so a bank seen twice does not
    get a confident estimate.
    """

    successes: dict[str, float] = field(default_factory=dict)
    attempts: dict[str, float] = field(default_factory=dict)
    prior_weight: float = 20.0
    prior_rate: float = 0.3

    def observe(self, issuer: str, ok: bool) -> None:
        self.attempts[issuer] = self.attempts.get(issuer, 0.0) + 1.0
        self.successes[issuer] = self.successes.get(issuer, 0.0) + float(ok)

    def rate(self, issuer: str) -> float:
        n = self.attempts.get(issuer, 0.0)
        s = self.successes.get(issuer, 0.0)
        return (s + self.prior_rate * self.prior_weight) / (n + self.prior_weight)

    def seen(self, issuer: str) -> float:
        return self.attempts.get(issuer, 0.0)


@dataclass(frozen=True, slots=True)
class FeatureContext:
    """Everything needed to describe a candidate execution."""

    state: MandateState
    execute_at: datetime
    amount_paise: Paise
    #: The mandate's normal debit, i.e. what it bills when nothing has gone wrong.
    reference_amount_paise: Paise
    now: datetime
    last_failure_at: datetime
    issuers: IssuerTracker


def extract(ctx: FeatureContext) -> np.ndarray:
    """One row. Order matches `FEATURE_NAMES`."""
    exec_ist = to_ist(ctx.execute_at)
    ref = max(1, ctx.reference_amount_paise)
    ratio = ctx.amount_paise / ref

    dom = exec_ist.day
    hour = exec_ist.hour + exec_ist.minute / 60.0

    hours_since = max(
        0.0, (to_ist(ctx.now) - to_ist(ctx.last_failure_at)).total_seconds() / 3600.0
    )
    days_left = max(
        0.0, (to_ist(ctx.state.cycle_end) - to_ist(ctx.execute_at)).total_seconds() / 86400.0
    )

    issuer = ctx.state.issuer_id
    return np.array(
        [
            ratio,
            # The mandate's *scale*, not the amount being attempted. Deliberately
            # invariant to the amount: sweeping the ratio must move exactly one
            # feature, or the monotonic constraint on the ratio can be undone by
            # an unconstrained feature moving alongside it.
            np.log1p(ref / 100.0),
            float(dom),
            # Cyclical encodings: the 31st is adjacent to the 1st, and a tree
            # splitting on a raw day number cannot express that.
            np.sin(2 * np.pi * dom / 30.0),
            np.cos(2 * np.pi * dom / 30.0),
            hour,
            np.sin(2 * np.pi * hour / 24.0),
            np.cos(2 * np.pi * hour / 24.0),
            float(ctx.state.attempts_used),
            hours_since,
            days_left,
            float(max(0, ctx.state.attempts_used - 1)),
            float(ctx.state.contacts_used),
            ctx.issuers.rate(issuer),
            np.log1p(ctx.issuers.seen(issuer)),
            float(ctx.state.variable_amount_allowed),
            float(ctx.state.category is not Category.STANDARD),
        ],
        dtype=np.float64,
    )


def extract_batch(contexts: Sequence[FeatureContext]) -> np.ndarray:
    if not contexts:
        return np.empty((0, N_FEATURES))
    return np.vstack([extract(c) for c in contexts])


def amount_axis(row: np.ndarray, ratios: Sequence[float]) -> np.ndarray:
    """Copy one feature row across a grid of amount ratios.

    Exactly one column moves: `amount_ratio`. That is what makes the survival
    curve monotone in practice and not merely in principle — the booster is
    constrained in the ratio, so if any unconstrained feature moved alongside it
    the guarantee would be void. An earlier version also varied the log amount,
    and the resulting curve rose at 0.8 of the debit.
    """
    out = np.repeat(row[None, :], len(ratios), axis=0)
    out[:, 0] = np.asarray(ratios, dtype=float)
    return out
