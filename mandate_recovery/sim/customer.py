"""Latent customer liquidity — the hidden state the agent never sees.

Balance is not noise around a mean. It is a **marked point process**: income
arrives in discrete jumps on days that depend on how the customer earns, and is
drawn down between arrivals by a compound-Poisson spend process. A debit
succeeds when the balance covers it at the instant of presentation.

That single mechanism is what makes both levers in the policy meaningful, and it
makes them meaningful for the *right reason* rather than because they were
hard-coded to be:

* **timing** matters because the balance is above the debit amount for only part
  of the month, concentrated shortly after a salary credit
* **amount** matters because `P(success) = P(balance ≥ a)` is monotone decreasing
  in `a`, so `a · P(balance ≥ a)` has an interior maximum

Month-end clustering of insufficient-funds failures is not injected anywhere. It
falls out of the process, which is the only way it can be honest evidence.

The agent sees none of this. It observes failure codes and the outcomes of its
own bets, and infers a posterior over type — deliberately over a *different*
bucketing than the one below, so the evaluation is not grading a model against
features it was handed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

from ..core.clock import SLOTS_PER_DAY
from ..core.money import Paise, rupees
from .rng import RandomTape


@dataclass(frozen=True, slots=True)
class LiquidityType:
    """One way of earning and spending money.

    `income_days` are days of the month for regular earners. An empty tuple means
    arrivals are Poisson at `arrival_rate_per_day`, which is what irregular
    income actually looks like.
    """

    name: str
    share: float
    income_days: tuple[int, ...]
    arrival_rate_per_day: float
    monthly_income_rupees: float
    income_cv: float
    #: Slot range within the day when income is credited. Salary landing in the
    #: early morning is why the 00:00-10:00 non-peak block is the valuable one.
    credit_slot_range: tuple[int, int]
    spend_events_per_day: float
    #: Mean spend event as a fraction of monthly income. Deliberately small and
    #: frequent: Indian consumers make many low-value UPI payments a day rather
    #: than a few large ones, and the granularity matters. Coarse spend jumps
    #: skip the balance straight past the few-hundred-rupee band, which is
    #: precisely the band a partial collection has to land in.
    spend_event_frac: float
    #: Standing balance as a fraction of monthly income. Thin buffers are the
    #: reason approval rates in this market are what they are.
    buffer_frac: float
    #: Rent, EMIs and bills — a lump that leaves the account within a few days of
    #: income arriving. Without it the account drains smoothly over the month and
    #: a debit billed on the 3rd almost always clears, which is not what the
    #: market data says happens.
    obligation_frac: float
    churn_base_daily: float


#: Eight types. Shares are rough and deliberately weighted towards the thin
#: buffers that dominate the reported failure statistics.
LATENT_TYPES: Final[tuple[LiquidityType, ...]] = (
    LiquidityType(
        name="salaried_1st", share=0.20,
        income_days=(1,), arrival_rate_per_day=0.0,
        monthly_income_rupees=42_000, income_cv=0.10,
        credit_slot_range=(0, 20),              # 00:00-10:00
        spend_events_per_day=9.0, spend_event_frac=0.0046,
        buffer_frac=0.018, obligation_frac=0.46, churn_base_daily=0.0012,
    ),
    LiquidityType(
        name="salaried_7th", share=0.16,
        income_days=(7,), arrival_rate_per_day=0.0,
        monthly_income_rupees=38_000, income_cv=0.10,
        credit_slot_range=(0, 20),
        spend_events_per_day=8.4, spend_event_frac=0.0049,
        buffer_frac=0.015, obligation_frac=0.48, churn_base_daily=0.0012,
    ),
    LiquidityType(
        name="salaried_month_end", share=0.14,
        income_days=(30,), arrival_rate_per_day=0.0,
        monthly_income_rupees=45_000, income_cv=0.12,
        credit_slot_range=(0, 24),
        spend_events_per_day=9.2, spend_event_frac=0.0047,
        buffer_frac=0.016, obligation_frac=0.47, churn_base_daily=0.0012,
    ),
    LiquidityType(
        name="dual_income", share=0.10,
        income_days=(1, 15), arrival_rate_per_day=0.0,
        monthly_income_rupees=64_000, income_cv=0.12,
        credit_slot_range=(0, 22),
        spend_events_per_day=10.5, spend_event_frac=0.0037,
        buffer_frac=0.045, obligation_frac=0.40, churn_base_daily=0.0008,
    ),
    LiquidityType(
        name="gig_irregular", share=0.16,
        income_days=(), arrival_rate_per_day=0.28,
        monthly_income_rupees=26_000, income_cv=0.55,
        credit_slot_range=(16, 44),             # earnings land through the day
        spend_events_per_day=9.8, spend_event_frac=0.0069,
        buffer_frac=0.010, obligation_frac=0.42, churn_base_daily=0.0022,
    ),
    LiquidityType(
        name="business_lumpy", share=0.08,
        income_days=(), arrival_rate_per_day=0.10,
        monthly_income_rupees=90_000, income_cv=0.85,
        credit_slot_range=(18, 40),
        spend_events_per_day=11.0, spend_event_frac=0.0074,
        buffer_frac=0.030, obligation_frac=0.44, churn_base_daily=0.0016,
    ),
    LiquidityType(
        name="student_parental", share=0.09,
        income_days=(), arrival_rate_per_day=0.16,
        monthly_income_rupees=12_000, income_cv=0.45,
        credit_slot_range=(0, 36),
        spend_events_per_day=7.0, spend_event_frac=0.0109,
        buffer_frac=0.012, obligation_frac=0.38, churn_base_daily=0.0030,
    ),
    LiquidityType(
        name="thin_file", share=0.07,
        income_days=(), arrival_rate_per_day=0.22,
        monthly_income_rupees=16_000, income_cv=0.60,
        credit_slot_range=(0, 44),
        spend_events_per_day=7.6, spend_event_frac=0.0122,
        buffer_frac=0.004, obligation_frac=0.40, churn_base_daily=0.0034,
    ),
)


@dataclass(frozen=True, slots=True)
class LatentCustomer:
    """A single customer's hidden truth. Inspection and testing only — never
    handed to the agent, and never used as a model feature."""

    index: int
    type_index: int
    type_name: str
    monthly_income_paise: Paise
    buffer_paise: Paise
    churn_intent: float
    balance: np.ndarray             # paise, per slot


@dataclass(slots=True)
class Population:
    """Vectorised latent state for the whole book.

    Held as arrays rather than objects because the balance trajectory is the hot
    path: 5,000 customers over 35 days at 30-minute resolution is 8.4 million
    points, and the evaluation harness walks it repeatedly.
    """

    type_index: np.ndarray          # (n,)
    monthly_income: np.ndarray      # (n,) paise
    buffer: np.ndarray              # (n,) paise
    churn_intent: np.ndarray        # (n,) in [0, 1]
    #: (slots, n) exogenous balance in paise — income and spend only. Debits are
    #: subtracted on top by the World, so the underlying path stays invariant to
    #: the policy and common random numbers keep working.
    exogenous_balance: np.ndarray

    @property
    def n(self) -> int:
        return int(self.type_index.shape[0])

    @property
    def slots(self) -> int:
        return int(self.exogenous_balance.shape[0])

    def customer(self, i: int) -> LatentCustomer:
        t = LATENT_TYPES[int(self.type_index[i])]
        return LatentCustomer(
            index=i,
            type_index=int(self.type_index[i]),
            type_name=t.name,
            monthly_income_paise=int(self.monthly_income[i]),
            buffer_paise=int(self.buffer[i]),
            churn_intent=float(self.churn_intent[i]),
            balance=self.exogenous_balance[:, i],
        )


def build_population(tape: RandomTape, n: int, days: int) -> Population:
    """Draw `n` customers and simulate their exogenous balance over `days`.

    Every draw is addressed through the tape, so the population produced for a
    given seed is identical no matter what any policy later does to it.
    """
    slots = days * SLOTS_PER_DAY
    shares = np.array([t.share for t in LATENT_TYPES], dtype=float)
    shares = shares / shares.sum()

    type_index = tape.generator("population.type").choice(len(LATENT_TYPES), size=n, p=shares)

    monthly_income = np.zeros(n)
    buffer = np.zeros(n)
    inflow = np.zeros((slots, n), dtype=np.float64)
    outflow = np.zeros((slots, n), dtype=np.float64)

    for ti, t in enumerate(LATENT_TYPES):
        members = np.flatnonzero(type_index == ti)
        if members.size == 0:
            continue
        m = members.size
        gen = tape.generator("population.params", ti)

        # Income scale: lognormal-ish dispersion around the type's mean.
        income = t.monthly_income_rupees * np.exp(
            gen.normal(0.0, t.income_cv, size=m) - 0.5 * t.income_cv**2
        )
        monthly_income[members] = income * 100.0
        buffer[members] = income * t.buffer_frac * 100.0

        _fill_income(tape, t, ti, members, income, inflow, days, gen)
        _fill_obligations(t, members, income, outflow, days, slots, gen)
        _fill_competing_debits(members, outflow, days, slots, gen)
        _fill_spend(tape, t, ti, members, income, outflow, days, slots, gen)

    # Sequential over slots, vectorised over customers. Spending cannot take the
    # balance below zero — an overdraft would quietly make the world kinder than
    # the market it is meant to represent.
    balance = np.empty((slots, n), dtype=np.float32)
    current = buffer.copy()
    for s in range(slots):
        current = current + inflow[s] - np.minimum(outflow[s], current)
        balance[s] = current

    churn_base = np.array([t.churn_base_daily for t in LATENT_TYPES])[type_index]
    churn_intent = np.clip(
        tape.generator("population.churn").beta(1.4, 6.0, size=n) + churn_base * 8.0,
        0.0,
        1.0,
    )

    return Population(
        type_index=type_index,
        monthly_income=monthly_income,
        buffer=buffer,
        churn_intent=churn_intent,
        exogenous_balance=balance,
    )


def _fill_income(
    tape: RandomTape,
    t: LiquidityType,
    ti: int,
    members: np.ndarray,
    income: np.ndarray,
    inflow: np.ndarray,
    days: int,
    gen: np.random.Generator,
) -> None:
    m = members.size
    if t.income_days:
        per_arrival = income / len(t.income_days)
        for day in range(days):
            dom = day % 30 + 1
            if dom not in t.income_days:
                continue
            lo, hi = t.credit_slot_range
            slot = day * SLOTS_PER_DAY + gen.integers(lo, max(lo + 1, hi), size=m)
            jitter = np.exp(gen.normal(0.0, 0.06, size=m))
            np.add.at(inflow, (slot, members), per_arrival * jitter * 100.0)
    else:
        # Irregular income: Poisson arrivals, gamma marks. Mean daily inflow is
        # matched to the type's monthly income so the types stay comparable.
        expected_arrivals = max(t.arrival_rate_per_day * 30.0, 1e-9)
        mark_mean = income / expected_arrivals
        counts = gen.poisson(t.arrival_rate_per_day, size=(days, m))
        for day in range(days):
            active = np.flatnonzero(counts[day] > 0)
            if active.size == 0:
                continue
            k = counts[day][active]
            amounts = gen.gamma(2.0, mark_mean[active] / 2.0) * k
            lo, hi = t.credit_slot_range
            slot = day * SLOTS_PER_DAY + gen.integers(lo, max(lo + 1, hi), size=active.size)
            np.add.at(inflow, (slot, members[active]), amounts * 100.0)


def _fill_obligations(
    t: LiquidityType,
    members: np.ndarray,
    income: np.ndarray,
    outflow: np.ndarray,
    days: int,
    slots: int,
    gen: np.random.Generator,
) -> None:
    """Rent, EMIs and bills, leaving within a few days of income arriving.

    This is the mechanism that makes the market's approval rates reproducible.
    Without it the account drains smoothly across the month, a debit billed on
    the 3rd nearly always clears, and the simulator quietly becomes a world in
    which recovery is easy.
    """
    m = members.size
    lump = income * t.obligation_frac
    if t.income_days:
        pay_days = [d for d in range(days) if (d % 30 + 1) in t.income_days]
        per = lump / max(1, len(t.income_days))
    else:
        # Irregular earners still owe rent on a calendar, not on their income.
        pay_days = [d for d in range(days) if d % 30 in (1, 2)]
        per = lump

    # Two waves, because obligations do not all move at the same speed.
    #
    # The auto-debited share — EMIs, utilities, insurance — fires within hours of
    # the credit landing, typically overnight. The rest is paid by hand over the
    # following days. Modelling only the slow wave leaves a wide, artificially
    # rich window right after payday, and a retry schedule that lands in it looks
    # far better than the market says retry schedules are.
    AUTO_SHARE = 0.62
    for day in pay_days:
        offs = gen.integers(0, 16, size=m)           # 00:00-08:00
        slot = np.minimum(slots - 1, day * SLOTS_PER_DAY + offs)
        jitter = np.exp(gen.normal(0.0, 0.14, size=m))
        np.add.at(outflow, (slot, members), per * AUTO_SHARE * jitter * 100.0)

        lag = gen.integers(0, 4, size=m)
        offs = gen.integers(14, 44, size=m)
        slot = np.minimum(slots - 1, (day + lag) * SLOTS_PER_DAY + offs)
        jitter = np.exp(gen.normal(0.0, 0.22, size=m))
        np.add.at(outflow, (slot, members), per * (1.0 - AUTO_SHARE) * jitter * 100.0)


def _fill_competing_debits(
    members: np.ndarray,
    outflow: np.ndarray,
    days: int,
    slots: int,
    gen: np.random.Generator,
) -> None:
    """The customer's *other* autopay mandates, hitting the same account.

    The typical account carries several standing instructions, and they bill in
    the same first-week window because that is when subscriptions bill. They are
    therefore not background noise: they are direct competition for exactly the
    balance this system is trying to reach, at exactly the moment it tries.

    Leaving them out is what made a naive retry schedule look far better than
    the market says it is — a week of retries would eventually find an
    uncontested moment, which is not the experience of an account with four
    other mandates queued against it.
    """
    m = members.size
    n_other = gen.integers(1, 5, size=m)
    sizes = np.array([149, 199, 299, 399, 499, 699, 999, 1499])
    for k in range(int(n_other.max())):
        firing = np.flatnonzero(n_other > k)
        if firing.size == 0:
            break
        amount = gen.choice(sizes, size=firing.size).astype(float)
        due_dom = gen.choice(np.arange(1, 29), size=firing.size, p=_billing_day_weights())
        for cycle_start in range(0, days, 30):
            day = np.minimum(days - 1, cycle_start + due_dom - 1)
            offs = gen.integers(0, SLOTS_PER_DAY, size=firing.size)
            slot = np.minimum(slots - 1, day * SLOTS_PER_DAY + offs)
            np.add.at(outflow, (slot, members[firing]), amount * 100.0)


def _billing_day_weights() -> np.ndarray:
    """Subscriptions bill early in the month, which is why they collide."""
    days = np.arange(1, 29)
    w = np.exp(-0.10 * (days - 1)) + 0.22
    return w / w.sum()


def _fill_spend(
    tape: RandomTape,
    t: LiquidityType,
    ti: int,
    members: np.ndarray,
    income: np.ndarray,
    outflow: np.ndarray,
    days: int,
    slots: int,
    gen: np.random.Generator,
) -> None:
    """Compound Poisson drawdown, weighted towards waking hours."""
    m = members.size
    mean_event = income * t.spend_event_frac
    counts = gen.poisson(t.spend_events_per_day, size=(days, m))

    # Spending is concentrated between roughly 08:00 and 23:00.
    weights = np.zeros(SLOTS_PER_DAY)
    weights[16:46] = 1.0
    weights[0:16] = 0.12
    weights[46:] = 0.4
    weights /= weights.sum()

    for day in range(days):
        k = counts[day]
        active = np.flatnonzero(k > 0)
        if active.size == 0:
            continue
        for _ in range(int(k.max())):
            firing = active[k[active] > 0]
            if firing.size == 0:
                break
            amounts = gen.gamma(1.6, mean_event[firing] / 1.6)
            offs = gen.choice(SLOTS_PER_DAY, size=firing.size, p=weights)
            np.add.at(outflow, (day * SLOTS_PER_DAY + offs, members[firing]), amounts * 100.0)
            k[firing] -= 1
