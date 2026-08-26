"""Evaluation: the number, and everything that makes it believable.

The track's bar is "show measured money recovered across a batch". This package
produces that measurement, and it is built so that the measurement can survive
someone trying to break it:

    policy.py     the interface every baseline and the allocator implement, so
                  none of them can reach the world except through the same door
    harness.py    the simulation loop. Runs the original execution, hands the
                  failures to a policy, and enforces the constraint layer on
                  every proposal — including counting the ones it refused
    baselines.py  the opponents, one of which is deliberately the policy a
                  Western recovery product would run here
    oracle.py     a clairvoyant policy that still obeys Indian law, giving the
                  ceiling any lawful policy could reach
    stats.py      paired differences, bootstrap intervals, signed-rank tests
    report.py     the results table

Two commitments about honesty run through all of it. Comparisons are **paired**
on common random numbers, because an unpaired few-percent uplift is
indistinguishable from noise. And results are reported against the oracle rather
than only against a baseline, because "we beat the heuristic by 12%" says nothing
about how much was left on the table.
"""

from .harness import RunMetrics, run_policy
from .oracle import ClairvoyantOracle
from .policy import Candidate, Policy
from .stats import PairedComparison, compare

__all__ = [
    "Candidate",
    "ClairvoyantOracle",
    "PairedComparison",
    "Policy",
    "RunMetrics",
    "compare",
    "run_policy",
]
