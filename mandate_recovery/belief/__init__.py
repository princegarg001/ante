"""What the agent believes about a customer it cannot see.

The balance is hidden. What is visible is a handful of outcomes — this debit
cleared on the 8th, that one failed on the 3rd — and the question is what those
imply about *when* this customer has money.

That makes the recovery problem a partially observed one, and the right object
is a posterior rather than a point estimate. This package maintains one over the
customer's **pay-cycle phase**: the day of the month around which their balance
peaks.

Two things make it honest:

* the phase is never observed, in training or at decision time. The shared
  "days since payday" success profile is recovered by expectation-maximisation
  from outcomes alone
* the agent's phase space is deliberately *not* the simulator's liquidity types.
  Different cardinality, different meaning, no shared constants. Handing the
  agent the generative parameterisation would make it look clever and prove
  nothing
"""

from .filter import PhaseBelief, PhaseProfile, fit_profile

__all__ = ["PhaseBelief", "PhaseProfile", "fit_profile"]
