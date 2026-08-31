"""The evaluation harness.

The harness produces the only number the track actually asked for, so the tests
here are mostly about the ways a measurement can be wrong while looking right:
a baseline that is weaker than advertised, a policy that reaches the world
around the constraint layer, a comparison that is not really paired, an oracle
that is not really an upper bound.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping, Sequence

import numpy as np
import pytest

from mandate_recovery.core.clock import IST, SLOTS_PER_DAY
from mandate_recovery.core.types import Action, Commit, MandateStatus, Stop
from mandate_recovery.eval.baselines import (
    FixedSchedule,
    NoRetry,
    StopEverything,
    StripeStyle,
)
from mandate_recovery.eval.harness import run_policy
from mandate_recovery.eval.oracle import ClairvoyantOracle
from mandate_recovery.eval.policy import Calendar, Candidate
from mandate_recovery.eval.stats import compare, recovery_efficiency
from mandate_recovery.sim import World, WorldConfig

ORIGIN = datetime(2026, 9, 1, 0, 0, tzinfo=IST)
TINY = WorldConfig(n_mandates=250, days=35)


def fresh(seed: int = 100) -> tuple[World, Calendar]:
    w = World.generate(seed, ORIGIN, TINY)
    return w, Calendar(origin=w.origin, horizon_slots=w.horizon_slots)


# --------------------------------------------------------------------------- #
# The experiment measures what it claims to measure
# --------------------------------------------------------------------------- #


def test_the_batch_is_the_failures_not_the_whole_book() -> None:
    """Recovered rupees must exclude debits that cleared on their due date —
    those were never at risk, and counting them would mostly measure the world."""
    w, cal = fresh()
    m = run_policy(NoRetry(cal), w)
    assert 0 < m.batch_size < TINY.n_mandates
    assert m.batch_value_paise > 0
    assert m.recovered_paise == 0


def test_the_floor_spends_nothing_and_recovers_nothing() -> None:
    w, cal = fresh()
    m = run_policy(NoRetry(cal), w)
    assert m.recovered_paise == 0
    assert m.presentations == 0
    assert m.cost_paise == 0
    assert m.net_value_paise == 0


def test_net_value_subtracts_the_cost_of_recovering() -> None:
    """Recovered rupees alone would reward a policy for burning attempts and
    customer patience to get them."""
    w, cal = fresh()
    m = run_policy(FixedSchedule(cal), w)
    assert m.presentations > 0
    assert m.cost_paise > 0
    assert m.net_value_paise == m.recovered_paise - m.cost_paise
    assert m.net_value_paise < m.recovered_paise


def test_a_completed_mandate_counts_as_survived() -> None:
    """Survival means "not revoked". Counting only LIVE made the policy that
    collected the most look like the one destroying the most mandates, because
    a fully collected mandate moves to COMPLETED."""
    w, cal = fresh()
    m = run_policy(ClairvoyantOracle(w, cal), w)
    assert m.mandates_recovered > 0
    assert m.survival_rate == (m.batch_size - m.mandates_revoked) / m.batch_size
    assert m.survival_rate > 0.5


# --------------------------------------------------------------------------- #
# Nothing reaches the world around the constraint layer
# --------------------------------------------------------------------------- #


@dataclass
class AlwaysIllegal:
    """Proposes an execution inside the morning peak window, every time."""

    calendar: Calendar
    name: str = "adversary · always illegal"

    def reset(self, seed: int) -> None:
        return None

    def plan(self, batch: Sequence[Candidate], now: datetime) -> Mapping[str, Action]:
        out: dict[str, Action] = {}
        for c in batch:
            target = c.now_slot + 48
            # Force it into 10:00–13:00 IST, which C2 forbids.
            day = target // SLOTS_PER_DAY
            peak = day * SLOTS_PER_DAY + 21          # 10:30
            out[c.mandate_id] = Commit(
                execute_at=self.calendar.time_of(peak),
                amount_paise=c.state.amount_due_paise,
            )
        return out


def test_an_illegal_proposal_never_reaches_the_world() -> None:
    w, cal = fresh()
    m = run_policy(AlwaysIllegal(cal), w)
    assert m.presentations == 0, "an illegal action was executed"
    assert m.recovered_paise == 0
    assert m.violations, "the refusal was not recorded"
    assert "C2" in m.violations
    assert len(m.violating_mandates) > 0


def test_violations_are_reported_per_mandate_as_well_as_per_proposal() -> None:
    """A policy that repeats one bad idea every epoch is not fifty times worse
    than one that has it once; the raw count measures decision cadence."""
    w, cal = fresh()
    m = run_policy(AlwaysIllegal(cal), w)
    assert sum(m.violations.values()) > len(m.violating_mandates)
    assert len(m.violating_mandates) <= m.batch_size


# --------------------------------------------------------------------------- #
# The baselines are the best versions of themselves
# --------------------------------------------------------------------------- #


def test_the_industry_baseline_proposes_nothing_illegal() -> None:
    """B1 is documented as a strong opponent. An earlier version proposed
    thousands of retries against revoked and closed mandates, which would have
    handed the allocator a win it had not earned."""
    w, cal = fresh()
    m = run_policy(FixedSchedule(cal), w)
    assert m.violations == {}, m.violations
    assert m.recovered_paise > 0


def test_the_industry_baseline_stops_on_terminal_causes() -> None:
    w, cal = fresh()
    m = run_policy(FixedSchedule(cal), w)
    assert m.stops > 0
    reasons = {
        o.stopped_reason for o in m.per_mandate.values() if o.stopped_reason
    }
    assert any("terminal cause" in r for r in reasons)


def test_the_transplanted_policy_proposes_illegal_actions() -> None:
    """The B3 slide, asserted rather than narrated."""
    w, cal = fresh()
    m = run_policy(StripeStyle(cal), w)
    assert m.violations, "B3 proposed nothing illegal — the transplant is not being modelled"
    assert "C5" in m.violations, "the aperture violation is the characteristic one"
    assert len(m.violating_mandates) / m.batch_size > 0.3


def test_the_transplanted_policy_also_collects_less() -> None:
    """It does not merely break the law — most of what it wants to do is
    unavailable, so it collects less as well."""
    seed = 100
    w1, c1 = fresh(seed)
    w2, c2 = fresh(seed)
    b1 = run_policy(FixedSchedule(c1), w1)
    b3 = run_policy(StripeStyle(c2), w2)
    assert b3.net_value_paise < b1.net_value_paise


# --------------------------------------------------------------------------- #
# The oracle really is a ceiling
# --------------------------------------------------------------------------- #


def test_the_oracle_beats_the_heuristic_on_every_seed() -> None:
    for seed in (100, 101, 102):
        w1, c1 = fresh(seed)
        w2, c2 = fresh(seed)
        b1 = run_policy(FixedSchedule(c1), w1)
        oracle = run_policy(ClairvoyantOracle(w2, c2), w2)
        assert oracle.net_value_paise > b1.net_value_paise, f"seed {seed}"


def test_the_oracle_obeys_the_same_law_as_everyone_else() -> None:
    """Clairvoyance is not a licence. If the oracle could break constraints its
    headroom number would be meaningless."""
    w, cal = fresh()
    m = run_policy(ClairvoyantOracle(w, cal), w)
    assert m.violations == {}, m.violations


def test_the_oracle_spends_far_fewer_attempts_per_rupee() -> None:
    """Knowing when the money is there means one well-timed presentation rather
    than three speculative ones — which is the whole allocation thesis, visible
    in the ceiling."""
    seed = 101
    w1, c1 = fresh(seed)
    w2, c2 = fresh(seed)
    b1 = run_policy(FixedSchedule(c1), w1)
    oracle = run_policy(ClairvoyantOracle(w2, c2), w2)
    assert oracle.slot_efficiency_paise > 2 * b1.slot_efficiency_paise


# --------------------------------------------------------------------------- #
# Pairing
# --------------------------------------------------------------------------- #


def test_every_policy_meets_the_identical_batch_on_a_seed() -> None:
    """The premise of the paired comparison. If the batches differed, the
    difference between two policies would partly be a difference in the
    customers they happened to be given."""
    seed = 105
    results = []
    for factory in (NoRetry, FixedSchedule, StripeStyle):
        w, cal = fresh(seed)
        results.append(run_policy(factory(cal), w))
    sizes = {r.batch_size for r in results}
    values = {r.batch_value_paise for r in results}
    assert len(sizes) == 1, sizes
    assert len(values) == 1, values


def test_a_run_is_reproducible() -> None:
    a = run_policy(FixedSchedule(fresh(103)[1]), fresh(103)[0])
    b = run_policy(FixedSchedule(fresh(103)[1]), fresh(103)[0])
    assert a.recovered_paise == b.recovered_paise
    assert a.presentations == b.presentations
    assert a.mandates_revoked == b.mandates_revoked


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


def test_paired_comparison_recovers_a_known_difference() -> None:
    a = [10.0, 12.0, 11.0, 13.0, 10.5, 11.5, 12.5, 10.2]
    b = [x - 2.0 for x in a]
    c = compare(a, b, a_name="a", b_name="b")
    assert c.mean_diff == pytest.approx(2.0)
    assert c.ci_low > 0 and c.significant
    assert c.wins == 8


def test_a_null_difference_is_not_reported_as_significant() -> None:
    rng = np.random.default_rng(0)
    a = rng.normal(0, 1, 10).tolist()
    b = rng.normal(0, 1, 10).tolist()
    c = compare(a, b, a_name="a", b_name="b")
    assert not c.significant, "noise was reported as an effect"


def test_recovery_efficiency_is_share_of_available_headroom() -> None:
    assert recovery_efficiency([50.0], [0.0], [100.0]) == pytest.approx(0.5)
    assert recovery_efficiency([100.0], [0.0], [100.0]) == pytest.approx(1.0)
    assert recovery_efficiency([-10.0], [0.0], [100.0]) == pytest.approx(-0.1)
    assert np.isnan(recovery_efficiency([5.0], [10.0], [10.0]))


def test_the_stop_list_accounts_for_the_whole_batch() -> None:
    """Every mandate in the batch must be accounted for exactly once.

    This test previously asserted that some mandates were *unactionable* —
    already revoked or expired before the first epoch, so no policy was ever
    asked about them. That was true, and it was a gap: a mandate sitting in the
    merchant's book with money outstanding is a decision the agent should be
    made to take, even when the only lawful moves are escalations.

    Dead mandates are now offered, the escalation ladder fires on them, and the
    unactionable bucket is empty. The invariant is unchanged and stronger: stops
    plus escalations plus unactionable equal the batch, and no rupee goes
    unaccounted for.
    """
    w, cal = fresh()
    m = run_policy(StopEverything(cal), w)
    assert m.presentations == 0
    escalated = sum(m.escalations.values())
    assert m.stops + escalated + m.unactionable == m.batch_size
    assert m.stopped_value_paise + m.unactionable_value_paise == m.batch_value_paise
    assert len(m.stop_ledger) == m.stops + escalated
