"""The world simulator.

Two classes of test here, and the second is the unusual one.

The first is ordinary: determinism, no negative balances, no latent state
leaking into what the agent can see.

The second checks that the world has the *structure the design claims it has* —
that insufficient-funds failures cluster around the billing cycle rather than
being uniform, that outages arrive in runs rather than independently, and that
lowering the amount genuinely raises the chance of collection. Those are the
mechanisms the policy is going to exploit. If they are not present, every
downstream result is an artefact.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from mandate_recovery.core.clock import IST, SLOTS_PER_DAY
from mandate_recovery.core.money import rupees
from mandate_recovery.core.types import CauseClass, MandateStatus
from mandate_recovery.sim import World, WorldConfig
from mandate_recovery.sim.issuer import IssuerHealth
from mandate_recovery.sim.world import Doom

ORIGIN = datetime(2026, 9, 1, 0, 0, tzinfo=IST)
SMALL = WorldConfig(n_mandates=400, days=35)


@pytest.fixture(scope="module")
def world() -> World:
    return World.generate(42, ORIGIN, SMALL)


# --------------------------------------------------------------------------- #
# Reproducibility
# --------------------------------------------------------------------------- #


def test_same_seed_reproduces_the_book_exactly() -> None:
    a = World.generate(7, ORIGIN, SMALL)
    b = World.generate(7, ORIGIN, SMALL)
    assert np.array_equal(a.population.exogenous_balance, b.population.exogenous_balance)
    assert np.array_equal(a.issuers.health, b.issuers.health)
    assert [m.mandate_id for m in a.mandates] == [m.mandate_id for m in b.mandates]
    assert [m.amount_due for m in a.mandates] == [m.amount_due for m in b.mandates]
    assert [m.due_slot for m in a.mandates] == [m.due_slot for m in b.mandates]


def test_different_seeds_produce_different_worlds() -> None:
    a = World.generate(7, ORIGIN, SMALL)
    b = World.generate(8, ORIGIN, SMALL)
    assert not np.array_equal(
        a.population.exogenous_balance, b.population.exogenous_balance
    )


def test_stream_names_are_hashed_stably_across_processes() -> None:
    """Python's built-in hash() is salted per process. Using it to address a
    stream would make runs irreproducible across invocations — a bug that stays
    hidden for exactly as long as you only ever look at one run."""
    from mandate_recovery.sim.rng import _stable_hash

    assert _stable_hash("population.type") == _stable_hash("population.type")
    assert _stable_hash("a") != _stable_hash("b")
    # Pinned so a change to the hashing scheme is a deliberate, visible act.
    assert _stable_hash("population.type") == 17393605087839508475


# --------------------------------------------------------------------------- #
# Common random numbers — the property the evaluation depends on
# --------------------------------------------------------------------------- #


def test_the_world_is_invariant_to_what_a_policy_does() -> None:
    """Two policies on the same seed must meet the identical world.

    Without this the difference between two policies is mostly a difference in
    the customers they happened to be given, and a few percent of uplift cannot
    be distinguished from noise no matter how many seeds are run.
    """
    busy = World.generate(11, ORIGIN, SMALL)
    idle = World.generate(11, ORIGIN, SMALL)

    # Hammer one of them with a very different action sequence.
    for m in busy.mandates[:150]:
        for offset in (0, 48, 96):
            busy.present(
                m.mandate_id,
                busy.time_of(min(m.due_slot + offset, busy.horizon_slots - 1)),
                m.amount_due,
            )
        busy.notify(m.mandate_id, busy.time_of(m.due_slot))

    assert np.array_equal(
        busy.population.exogenous_balance, idle.population.exogenous_balance
    )
    assert np.array_equal(busy.issuers.health, idle.issuers.health)
    assert np.array_equal(busy.issuers.system_down, idle.issuers.system_down)

    # And the per-customer variate tapes are addressed, not consumed: an
    # untouched customer sees the identical draws in both worlds.
    untouched = 300
    assert np.array_equal(
        busy._tech_uniform(untouched), idle._tech_uniform(untouched)
    )
    assert np.array_equal(
        busy._revoke_uniform(untouched), idle._revoke_uniform(untouched)
    )


def test_a_successful_debit_still_reduces_available_balance() -> None:
    """Common random numbers must not be bought by making the world inert."""
    w = World.generate(13, ORIGIN, SMALL)
    m = next(
        x for x in w.mandates
        if x.doom is Doom.NONE and w.balance_at(x.customer, x.due_slot) > x.amount_due * 3
    )
    before = w.balance_at(m.customer, m.due_slot)
    result = w.present(m.mandate_id, w.time_of(m.due_slot), m.amount_due)
    assert result.ok
    assert w.balance_at(m.customer, m.due_slot) == before - m.amount_due


# --------------------------------------------------------------------------- #
# The agent must not be able to see the answer
# --------------------------------------------------------------------------- #


def test_the_observable_state_carries_no_latent_information(world: World) -> None:
    m = world.mandates[0]
    observable = world.observable(m)
    fields = set(observable.__slots__)
    for leak in ("balance", "churn_intent", "liquidity", "type_index", "doom", "income"):
        assert not any(leak in f for f in fields), f"latent state leaked as {leak}"


def test_balance_is_never_negative(world: World) -> None:
    """Spending is capped at available funds. An overdraft would quietly make the
    world kinder than the market it represents."""
    assert float(world.population.exogenous_balance.min()) >= 0.0


# --------------------------------------------------------------------------- #
# Structure the policy is going to rely on
# --------------------------------------------------------------------------- #


def test_insufficient_funds_failures_are_not_uniform_across_the_month() -> None:
    """If failures were uniform in time, timing would be worthless and any
    timing result downstream would be an artefact of the harness."""
    w = World.generate(21, ORIGIN, WorldConfig(n_mandates=1200, days=35))
    by_day = np.zeros(28)
    tries = np.zeros(28)
    for m in w.mandates:
        if m.doom is not Doom.NONE:
            continue
        day = (m.due_slot // SLOTS_PER_DAY) % 28
        r = w.present(m.mandate_id, w.time_of(m.due_slot), m.amount_due)
        tries[day] += 1
        by_day[day] += int(not r.ok)

    seen = tries > 12
    assert seen.sum() >= 8, "not enough coverage to judge"
    rates = by_day[seen] / tries[seen]
    assert rates.max() - rates.min() > 0.18, (
        f"failure rate is nearly flat across the month ({rates.min():.2f}"
        f"–{rates.max():.2f}); the balance process is not doing its job"
    )


def test_issuer_outages_arrive_in_runs_not_independently() -> None:
    """Independent technical failures would let any policy that spreads its
    attempts diversify away a risk that is in fact correlated."""
    w = World.generate(31, ORIGIN, SMALL)
    lane = w.issuers.health[:, 0]
    down = lane == IssuerHealth.DOWN
    if down.sum() < 5:
        pytest.skip("no outage on this seed")
    # Consecutive down-slots, versus what independence would give.
    runs, current = [], 0
    for v in down:
        if v:
            current += 1
        elif current:
            runs.append(current)
            current = 0
    if current:
        runs.append(current)
    assert max(runs) >= 2, "outages last a single slot — they are not incidents"


def test_lowering_the_amount_raises_the_chance_of_collection() -> None:
    """The amount lever must be real in the world, not only in the design.

    Counts the slots in a mandate's recovery window where the balance would
    cover the full amount against a partial one. If a partial collection is
    never easier, the policy's most differentiated feature has nothing to work
    with here and any gain it shows later is coming from somewhere else.
    """
    w = World.generate(41, ORIGIN, WorldConfig(n_mandates=600, days=35))
    improved = 0
    considered = 0
    for m in w.mandates:
        if m.doom is not Doom.NONE:
            continue
        lo, hi = m.due_slot, min(m.due_slot + 14 * SLOTS_PER_DAY, w.horizon_slots)
        if hi - lo < 100:
            continue
        bal = w.population.exogenous_balance[lo:hi, m.customer]
        full = int((bal >= m.amount_due).sum())
        half = int((bal >= m.amount_due * 0.5).sum())
        considered += 1
        if half > full:
            improved += 1
    assert considered > 100
    assert improved / considered > 0.25, (
        "halving the amount almost never opens a new window; the balance process "
        "is too coarse for the amount lever to mean anything"
    )


def test_the_amount_lever_pays_where_theory_says_it_should() -> None:
    """`a · P(balance ≥ a)` peaks at a partial amount only on part of the book,
    and the test asserts the *gradient* rather than a global share.

    Measured on seed 43: never below a debit-to-balance ratio of 0.35, then 9%
    and 15% as the debit approaches and exceeds a good-day balance. That is the
    honest shape of this lever — it is not a universal trick that beats every
    timing model everywhere. It pays on thin accounts carrying large debits,
    which is 36% of the book and 45% of the value at risk.

    Asserting a flat "15% of all mandates prefer partial" would have been an
    invented bar, and the earlier version of this test did exactly that.
    """
    w = World.generate(43, ORIGIN, WorldConfig(n_mandates=1200, days=35))
    fractions = np.array([1.0, 0.75, 0.5, 0.3, 0.15])
    light, heavy = [], []
    for m in w.mandates:
        if m.doom is not Doom.NONE:
            continue
        lo, hi = m.due_slot, min(m.due_slot + 14 * SLOTS_PER_DAY, w.horizon_slots)
        if hi - lo < 100:
            continue
        bal = w.population.exogenous_balance[lo:hi, m.customer]
        good_day = float(np.percentile(bal, 90))
        ratio = m.amount_due / max(1.0, good_day)
        ev = [f * m.amount_due * float((bal >= f * m.amount_due).mean()) for f in fractions]
        prefers_partial = int(np.argmax(ev)) != 0
        (heavy if ratio >= 0.8 else light if ratio < 0.15 else []).append(prefers_partial)

    assert len(heavy) > 60 and len(light) > 200
    heavy_rate, light_rate = float(np.mean(heavy)), float(np.mean(light))

    # A debit small against the account can never be helped by shrinking it.
    assert light_rate < 0.02, f"partial collection helps small debits ({light_rate:.1%})"
    # A debit comparable to the account balance frequently can be.
    assert heavy_rate > 0.08, f"the lever is dead even on heavy debits ({heavy_rate:.1%})"
    assert heavy_rate > light_rate * 4


# --------------------------------------------------------------------------- #
# The unrecoverable segment
# --------------------------------------------------------------------------- #


def test_a_real_share_of_the_book_cannot_be_recovered(world: World) -> None:
    doomed = [m for m in world.mandates if m.doom is not Doom.NONE]
    share = len(doomed) / len(world.mandates)
    assert 0.08 < share < 0.25, f"unrecoverable share is {share:.2%}"
    assert len({m.doom for m in doomed}) >= 3, "only one kind of doom is modelled"


def test_closed_accounts_never_collect_however_often_you_try(world: World) -> None:
    """If the stop list is empty at the end of a run, the world was too kind."""
    closed = [m for m in world.mandates if m.doom is Doom.ACCOUNT_CLOSED]
    assert closed, "no closed accounts in the book"
    m = closed[0]
    for offset in range(0, 10 * SLOTS_PER_DAY, SLOTS_PER_DAY):
        slot = min(m.due_slot + offset, world.horizon_slots - 1)
        r = world.present(m.mandate_id, world.time_of(slot), m.amount_due)
        assert not r.ok
        assert r.cause is CauseClass.TERMINAL


def test_a_revoked_mandate_reports_revoked_rather_than_insufficient_funds(
    world: World,
) -> None:
    """Cause matters more than outcome downstream: a revoked mandate must never
    look like a balance problem, or the agent will keep retrying it."""
    revoked = [m for m in world.mandates if m.doom is Doom.ALREADY_REVOKED]
    assert revoked
    r = world.present(revoked[0].mandate_id, world.time_of(revoked[0].due_slot), rupees(199))
    assert r.cause is CauseClass.MANDATE_REVOKED
    assert r.error_code == "mandate_revoked"


# --------------------------------------------------------------------------- #
# C9, and the evidence that settled it
# --------------------------------------------------------------------------- #


def test_c9_applies_only_to_newly_registered_mandates() -> None:
    """Calibration rejected the broad reading on evidence.

    Applied to the first presentation of *every* cycle, a book failing ~66% of
    first attempts revokes most of itself monthly — against a market reporting
    roughly 20M revocations on 808M executions. The narrow reading is what the
    data supports, and the switch is a config field so the finding stays visible.
    """
    w = World.generate(51, ORIGIN, WorldConfig(n_mandates=500, days=35))
    established = [
        m for m in w.mandates
        if not m.is_new_registration and m.doom is Doom.NONE
    ]
    assert established
    survived_a_failure = 0
    for m in established[:120]:
        r = w.present(m.mandate_id, w.time_of(m.due_slot), m.amount_due)
        if not r.ok and not r.revoked_mandate and m.status is MandateStatus.LIVE:
            survived_a_failure += 1
    assert survived_a_failure > 0, "an established mandate was killed by one failure"


def test_c9_can_be_switched_off_entirely() -> None:
    """The claim is single-sourced, so the design must degrade rather than break
    if it turns out to be wrong."""
    off = WorldConfig(n_mandates=300, days=35, first_failure_revokes=False)
    w = World.generate(53, ORIGIN, off)
    new = [m for m in w.mandates if m.is_new_registration and m.doom is Doom.NONE]
    assert new
    for m in new[:40]:
        r = w.present(m.mandate_id, w.time_of(m.due_slot), m.amount_due)
        assert not r.revoked_mandate
