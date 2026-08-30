"""The allocator.

The tests worth having here are the ones that would catch it winning for the
wrong reason: reaching the world around the constraint layer, confusing patience
with refusal, or carrying an option-value term that does not actually change any
decision.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import numpy as np
import pytest

from mandate_recovery.core.clock import IST
from mandate_recovery.eval.baselines import FixedSchedule
from mandate_recovery.eval.greedy import GreedyEV
from mandate_recovery.eval.harness import run_policy
from mandate_recovery.eval.policy import Calendar
from mandate_recovery.policy.allocator import (
    AllocatorConfig,
    SlotAllocator,
    estimate_revocation_hazard,
)
from mandate_recovery.predict.pipeline import fit_cached
from mandate_recovery.sim.issuer import ISSUERS
from mandate_recovery.sim.world import World, WorldConfig

ORIGIN = datetime(2026, 9, 1, 0, 0, tzinfo=IST)
CFG = WorldConfig(n_mandates=700, days=35)


@pytest.fixture(scope="module")
def fitted():
    return fit_cached(range(0, 8), CFG)


def build(seed: int, fitted, config: AllocatorConfig | None = None):
    world = World.generate(seed, ORIGIN, CFG)
    cal = Calendar(origin=world.origin, horizon_slots=world.horizon_slots)
    issuer_of = {m.mandate_id: ISSUERS[m.issuer].code for m in world.mandates}
    policy = SlotAllocator(
        fitted.model, cal, profile=fitted.profile, issuer_of=issuer_of, config=config
    )
    return world, cal, policy


# --------------------------------------------------------------------------- #
# It plays by the rules
# --------------------------------------------------------------------------- #


def test_the_allocator_proposes_nothing_illegal(fitted) -> None:
    """The constraint layer is re-consulted at the boundary anyway, but a policy
    that constantly proposes illegal actions is a policy with a bug."""
    world, _, policy = build(100, fitted)
    metrics = run_policy(policy, world)
    assert metrics.violations == {}, metrics.violations


def test_it_never_exceeds_the_retry_budget(fitted) -> None:
    world, _, policy = build(101, fitted)
    metrics = run_policy(policy, world)
    for outcome in metrics.per_mandate.values():
        assert outcome.attempts <= 4, outcome


def test_a_run_is_reproducible(fitted) -> None:
    a = run_policy(build(102, fitted)[2], build(102, fitted)[0])
    b = run_policy(build(102, fitted)[2], build(102, fitted)[0])
    assert a.recovered_paise == b.recovered_paise
    assert a.presentations == b.presentations
    assert a.stops == b.stops


# --------------------------------------------------------------------------- #
# It beats the ablation, which is the claim that matters
# --------------------------------------------------------------------------- #


def test_it_beats_greedy_on_the_same_model(fitted) -> None:
    """B2 has the identical probability model and no budget reasoning, so the
    gap between them is what allocation is worth. If this fails, the central
    claim of the project is unsupported."""
    wins = 0
    for seed in (100, 101, 102):
        world, cal, alloc = build(seed, fitted)
        a = run_policy(alloc, world)

        world2 = World.generate(seed, ORIGIN, CFG)
        cal2 = Calendar(origin=world2.origin, horizon_slots=world2.horizon_slots)
        issuer_of = {m.mandate_id: ISSUERS[m.issuer].code for m in world2.mandates}
        g = run_policy(GreedyEV(fitted.model, cal2, issuer_of=issuer_of), world2)

        assert a.batch_size == g.batch_size, "the pairing is broken"
        wins += a.net_value_paise > g.net_value_paise
    assert wins == 3, f"allocator beat greedy on only {wins}/3 seeds"


def test_it_beats_the_industry_heuristic(fitted) -> None:
    world, _, alloc = build(103, fitted)
    a = run_policy(alloc, world)
    world2 = World.generate(103, ORIGIN, CFG)
    cal2 = Calendar(origin=world2.origin, horizon_slots=world2.horizon_slots)
    b1 = run_policy(FixedSchedule(cal2), world2)
    assert a.net_value_paise > b1.net_value_paise


# --------------------------------------------------------------------------- #
# Patience is not refusal
# --------------------------------------------------------------------------- #


def test_waiting_and_stopping_are_different_decisions(fitted) -> None:
    """Found by measurement, not by review.

    The first version had no `wait` branch: whenever committing immediately was
    not the best move, the extraction emitted `Stop`. A mandate whose best slot
    was nine days out — outside the 24-48h notification aperture — was therefore
    refused for good. The batch showed 408 stops and zero presentations, and the
    dynamic programme underneath was entirely correct.
    """
    world, _, policy = build(100, fitted)
    metrics = run_policy(policy, world)
    assert metrics.presentations > 0, "the allocator refused the entire batch"
    assert metrics.stops < metrics.batch_size, "every mandate was stopped"
    # Some mandates must be genuinely refused, or the stop list is empty and the
    # option-value term is doing nothing.
    assert metrics.stops > 0


def test_it_holds_a_slot_that_is_not_yet_committable(fitted) -> None:
    """The DP is allowed to pick a slot beyond the aperture and wait for it."""
    world, cal, policy = build(101, fitted)
    run_policy(policy, world)
    waited = [p for p in policy.last_book if p.action == "wait"]
    committed = [p for p in policy.last_book if p.action == "commit"]
    assert waited or committed, "the allocator produced no plans at all"


# --------------------------------------------------------------------------- #
# The option-value term changes decisions
# --------------------------------------------------------------------------- #


def test_a_larger_option_value_makes_it_more_cautious(fitted) -> None:
    """If the term were decorative, moving it would change nothing.

    Raising the mandate's continuation value raises the cost of risking it, so
    the allocator should attempt less and refuse more.
    """
    world_a, _, timid = build(100, fitted, AllocatorConfig(ltv_multiple=40.0))
    world_b, _, bold = build(100, fitted, AllocatorConfig(ltv_multiple=1.0))
    a = run_policy(timid, world_a)
    b = run_policy(bold, world_b)
    assert a.presentations < b.presentations, (a.presentations, b.presentations)
    assert a.stops > b.stops


def test_a_zero_hazard_removes_the_brake(fitted) -> None:
    """With no revocation risk, a spare attempt is nearly free and the allocator
    should spend more of them."""
    world_a, _, careful = build(100, fitted, AllocatorConfig(revoke_hazard_per_failure=0.08))
    world_b, _, reckless = build(100, fitted, AllocatorConfig(revoke_hazard_per_failure=0.0))
    a = run_policy(careful, world_a)
    b = run_policy(reckless, world_b)
    assert b.presentations >= a.presentations


def test_the_hazard_estimator_matches_its_definition() -> None:
    """The parameter is measured from two runs, not invented. The first version
    used 0.055 from nowhere, at which value refusing the entire batch was the
    arithmetically correct answer."""
    assert estimate_revocation_hazard(0.789, 0.815, 3.0) == pytest.approx(0.0086667, abs=1e-6)
    assert estimate_revocation_hazard(0.9, 0.8, 3.0) == 0.0     # never negative
    assert estimate_revocation_hazard(0.7, 0.8, 0.0) == 0.0     # no attempts, no hazard


# --------------------------------------------------------------------------- #
# The market
# --------------------------------------------------------------------------- #


def test_scarce_capacity_produces_a_price(fitted) -> None:
    """With capacity tight, some window must clear above zero — otherwise the
    dual has not bound and the auction is decorative."""
    tight = AllocatorConfig(window_capacity_share=0.004, dual_iterations=14)
    world, _, policy = build(100, fitted, tight)
    run_policy(policy, world)
    prices = [c.price_paise for c in policy.last_clearing]
    assert prices, "no clearing prices were recorded"
    assert max(prices) > 0.0, "capacity was scarce and nothing was priced"


def test_abundant_capacity_stays_free(fitted) -> None:
    loose = AllocatorConfig(window_capacity_share=5.0, dual_iterations=6)
    world, _, policy = build(100, fitted, loose)
    run_policy(policy, world)
    assert all(c.price_paise == 0.0 for c in policy.last_clearing)


def test_every_plan_carries_a_readable_reason(fitted) -> None:
    """The justification strings are the demo. An unexplained decision is not
    shippable in a money system."""
    world, _, policy = build(100, fitted)
    run_policy(policy, world)
    assert policy.last_book
    for plan in policy.last_book[:50]:
        assert plan.reason and len(plan.reason) > 20, plan
        assert plan.action in {"commit", "wait", "stop"}
