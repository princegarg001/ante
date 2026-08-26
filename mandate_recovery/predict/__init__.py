"""Predicting whether a debit will clear — as a distribution, not a coin flip.

The results table showed where the value in this system is: a clairvoyant
policy recovers ₹530 per attempt where the industry heuristic recovers ₹90, and
the whole of that gap is knowing when the balance covers the debit.

So the object to estimate is not `P(this retry succeeds)`. The action is a pair
— *when* and *how much* — and

    EV(t, a) = a · P(balance(t) ≥ a)

which is the **survival function of the balance**, evaluated at `a`. Modelling
it directly has three consequences that a binary classifier does not give:

* every `(t, a)` pair is available from one model, instead of ~2,300 separate
  queries per mandate for the dynamic programme tomorrow
* the amount lever is expressible at all — a classifier trained only on full
  amounts has nothing to say about collecting less
* the curve can be made **monotone in the amount**, which a raw classifier is
  not, and non-monotonicity there would produce an expected-value curve with
  fake local maxima that the allocator would happily exploit

Everything here is trained on logged transactions only. The simulator's latent
state is never a feature, and the agent's belief space is deliberately given a
different structure from the world's generative parameters — otherwise the
evaluation grades a model against features it was handed.
"""

from .features import FEATURE_NAMES, FeatureContext, IssuerTracker, extract
from .model import SurvivalModel, TrainedModel

__all__ = [
    "FEATURE_NAMES",
    "FeatureContext",
    "IssuerTracker",
    "SurvivalModel",
    "TrainedModel",
    "extract",
]
