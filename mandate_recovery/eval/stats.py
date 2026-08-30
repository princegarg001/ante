"""Paired comparison, done properly.

Ten seeds is a small sample, and between-seed variance in a mandate book is
large — different customers, different salary dates, different outages. Compare
two policies by their unpaired means and a three-percent difference vanishes
into that variance no matter how carefully it was earned.

Because the world is built on common random numbers, every policy meets the
*identical* book on a given seed. The right statistic is therefore the
per-seed **difference**, whose variance is far smaller than either policy's own.

Reported as a mean difference with a bootstrap interval and a signed-rank test.
The interval is the honest part: an uplift whose interval straddles zero has not
been demonstrated, however good the point estimate looks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
from scipy import stats as _scipy_stats


@dataclass(frozen=True, slots=True)
class PairedComparison:
    a: str
    b: str
    n: int
    mean_diff: float
    ci_low: float
    ci_high: float
    p_value: float
    wins: int

    @property
    def significant(self) -> bool:
        """Both the interval and the signed-rank test, where both exist.

        They can disagree: a bootstrap interval on ten paired differences
        excluded zero while Wilcoxon returned p = 0.084 on the same data. Taking
        whichever agrees would be picking the answer, so a result counts only
        when both do.
        """
        interval = (self.ci_low > 0.0) or (self.ci_high < 0.0)
        if np.isnan(self.p_value):
            return interval
        return interval and self.p_value < 0.05

    def render(self, unit: str = "") -> str:
        star = "*" if self.significant else " "
        return (
            f"{self.mean_diff:>+12,.0f}{unit}  "
            f"[{self.ci_low:>+11,.0f}, {self.ci_high:>+11,.0f}]  "
            f"p={self.p_value:<6.4f} {star}  {self.wins}/{self.n} seeds"
        )


def compare(
    a_values: Sequence[float],
    b_values: Sequence[float],
    *,
    a_name: str,
    b_name: str,
    n_boot: int = 10_000,
    seed: int = 0,
) -> PairedComparison:
    """Paired difference `a - b`, with a bootstrap interval and a signed-rank test.

    Both sequences must be indexed by the same seeds in the same order, which is
    what makes the pairing valid.
    """
    a = np.asarray(a_values, dtype=float)
    b = np.asarray(b_values, dtype=float)
    if a.shape != b.shape:
        raise ValueError("paired comparison requires equal-length sequences")
    diffs = a - b
    n = diffs.size

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_boot, n))
    boot_means = diffs[idx].mean(axis=1)
    ci_low, ci_high = np.percentile(boot_means, [2.5, 97.5])

    if n >= 6 and np.any(diffs != 0):
        # Non-parametric: n is small and the differences are not assumed normal.
        p = float(_scipy_stats.wilcoxon(diffs, zero_method="zsplit").pvalue)
    else:
        p = float("nan")

    return PairedComparison(
        a=a_name,
        b=b_name,
        n=n,
        mean_diff=float(diffs.mean()),
        ci_low=float(ci_low),
        ci_high=float(ci_high),
        p_value=p,
        wins=int((diffs > 0).sum()),
    )


def recovery_efficiency(
    policy: Sequence[float], baseline: Sequence[float], oracle: Sequence[float]
) -> float:
    """Share of the lawfully achievable improvement that was actually captured.

        (policy − baseline) / (oracle − baseline)

    A far more defensible statement than a raw uplift, because it bounds what is
    left rather than implying there is more.
    """
    p = np.asarray(policy, dtype=float).mean()
    b = np.asarray(baseline, dtype=float).mean()
    o = np.asarray(oracle, dtype=float).mean()
    headroom = o - b
    if headroom <= 0:
        return float("nan")
    return float((p - b) / headroom)
