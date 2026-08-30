"""Training, in two passes, and why it has to be two.

The pay-cycle posterior is a useful feature, and it cannot exist on the first
pass: fitting the shared phase profile needs outcome data, and computing the
belief feature needs the profile. So:

    pass 1   explore, with the belief features at their uninformative defaults
             → fit the phase profile by EM over the outcomes

    pass 2   explore again, now carrying a live posterior per mandate
             → fit the survival model, with the belief score and its entropy
               among the features

Passing the **entropy** alongside the score is the part that matters. Measured
against ground truth, the posterior is worth nothing on the first decision and
six to nine points from the second onward. Rather than encode that as a rule —
"trust the belief once you have two observations" — both quantities go in as
features and the model learns the relationship. A diffuse belief and a sharp one
are genuinely different inputs, not the same input with more noise.

Everything runs on the training seeds. Evaluation seeds are never touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Sequence

import numpy as np

from ..belief.filter import PhaseProfile, fit_profile
from ..sim.world import WorldConfig
from .dataset import Dataset, collect
from .model import SurvivalModel, TrainedModel

TRAIN_SEEDS: Final[tuple[int, ...]] = tuple(range(0, 8))

#: Index of `day_of_month` in the feature vector, used to fit the profile.
_DAY_COLUMN: Final[int] = 2


@dataclass(frozen=True, slots=True)
class Fitted:
    profile: PhaseProfile
    model: TrainedModel
    pass_one: Dataset
    pass_two: Dataset

    def summary(self) -> str:
        return "\n".join(
            [
                "  pass 1 (no belief)     "
                f"{self.pass_one.n:,} rows, {self.pass_one.positive_rate:.1%} positive",
                f"  phase profile          EM {self.profile.iterations} iterations, "
                f"peak lag {self.profile.peak_lag}d, contrast {self.profile.contrast:.1f}x",
                "  pass 2 (with belief)   "
                f"{self.pass_two.n:,} rows, {self.pass_two.positive_rate:.1%} positive",
                "",
                self.model.report.render(),
            ]
        )


def fit(
    seeds: Sequence[int] = TRAIN_SEEDS,
    config: WorldConfig | None = None,
    *,
    seed: int = 0,
) -> Fitted:
    cfg = config or WorldConfig(n_mandates=2500, days=35)

    first = collect(seeds, cfg)
    profile = fit_profile(
        first.groups, first.X[:, _DAY_COLUMN].astype(int), first.y, seed=seed
    )

    second = collect(seeds, cfg, profile=profile)
    model = SurvivalModel.fit(second, seed=seed)

    return Fitted(profile=profile, model=model, pass_one=first, pass_two=second)


_CACHE: dict[tuple, Fitted] = {}


def fit_cached(seeds: Sequence[int], config: WorldConfig, *, seed: int = 0) -> Fitted:
    """Training is deterministic, so one fit per process is enough."""
    key = (tuple(seeds), config.n_mandates, config.days, seed)
    if key not in _CACHE:
        _CACHE[key] = fit(seeds, config, seed=seed)
    return _CACHE[key]
