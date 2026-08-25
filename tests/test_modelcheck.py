"""The model checker, wired into CI.

Runs a small horizon by default so it stays a fast gate on every commit. The
headline figure quoted in the README comes from the `slow` variant.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from mandate_recovery.constraints.modelcheck import (
    MAX_ATTEMPTS,
    _inv_violations,
    reachable,
    sweep,
)
from mandate_recovery.core.money import rupees
from mandate_recovery.core.types import Commit, MandateStatus
from tests.conftest import ORIGIN, make_state


# --------------------------------------------------------------------------- #
# The independent specification must be able to say "no".
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "hours,amount,overrides,expected",
    [
        (34, rupees(499), {}, "C2"),                                   # 10:00 next day
        (43, rupees(499), {}, "C2"),                                   # 19:00 next day
        (12, rupees(499), {}, "C5"),                                   # under the aperture
        (72, rupees(499), {}, "C5"),                                   # over the aperture
        (24, rupees(499), {"attempts_used": 4}, "C1"),
        (24, rupees(499), {"status": MandateStatus.REVOKED}, "C12"),
        (24, rupees(50_000), {"amount_due_paise": rupees(60_000),
                              "max_amount_paise": rupees(60_000)}, "C15"),
        (24, rupees(5_000), {"max_amount_paise": rupees(1_000)}, "C19"),
    ],
)
def test_independent_spec_flags_known_illegal_actions(
    hours: float, amount: int, overrides: dict, expected: str
) -> None:
    """Guards the checker against agreeing with the code by construction.

    Mutation testing caught this: zeroing the morning peak window inside
    `_inv_violations` left the whole suite green, because the spec is normally only
    consulted about actions the layer already permitted. Asking it directly about
    illegal actions is what makes it a specification rather than an echo.
    """
    action = Commit(execute_at=ORIGIN + timedelta(hours=hours), amount_paise=amount)
    assert expected in _inv_violations(action, make_state(**overrides), ORIGIN)


def test_independent_spec_accepts_a_lawful_action() -> None:
    """The mirror image — a spec that flags everything would also kill every mutant."""
    action = Commit(execute_at=ORIGIN + timedelta(hours=24), amount_paise=rupees(499))
    assert _inv_violations(action, make_state(), ORIGIN) == []


@pytest.fixture(scope="module")
def swept():
    """One sweep shared across the module — it is deterministic, so re-running it
    per test only buys latency."""
    return sweep(days=1)


def test_sweep_finds_no_permitted_illegal_action(swept) -> None:
    assert swept.ok, "\n\n".join(ce.render() for ce in swept.counterexamples)
    assert swept.triples_enumerated > 500_000


def test_the_sweep_actually_exercises_both_verdicts(swept) -> None:
    """Guards against a degenerate checker. A layer that vetoed everything would
    satisfy the claim above vacuously, so both verdicts must be well represented."""
    assert swept.permitted > 1_000
    assert swept.vetoed > swept.permitted


def test_reachability_cannot_overrun_the_budget() -> None:
    """Five days is the shortest horizon that binds the cap.

    Each attempt costs at least 24h of notification lead (C5), so four attempts
    need four days plus slack. A shorter run would pass vacuously, which is why
    the binding check is an assertion rather than a note.
    """
    result = reachable(days=5)
    assert result.max_attempts_seen == MAX_ATTEMPTS, "horizon too short to bind the cap"
    assert result.max_pending_seen <= 1
    assert result.ok


@pytest.mark.slow
def test_full_sweep_for_the_readme_figure() -> None:
    """The numbers quoted in README.md. Deselect with -m 'not slow'."""
    s = sweep(days=3)
    r = reachable(days=6)
    assert s.ok and r.ok
    print(f"\nenumerated {s.triples_enumerated:,} triples, {r.states_reached:,} states, 0 violations")
