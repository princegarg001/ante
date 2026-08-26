"""A calibrated, monotone conditional survival model.

Three properties, each chosen because the allocator would otherwise be able to
exploit the model rather than the world.

**Monotone in the amount, by construction.** `P(balance ≥ a)` must fall as `a`
rises — that is what a survival function *is*. A gradient-boosted tree fitted
without that constraint will produce small non-monotonicities from sampling
noise, and `a · P(a)` will then have fake local maxima. The dynamic programme
tomorrow searches over amounts and would find them, choosing an odd partial
amount for a reason that exists only in the model. So the constraint is imposed
inside the booster (`monotonic_cst = -1` on both amount features), not patched
on afterwards.

**Calibrated.** The output feeds an expected-value calculation, so 0.3 has to
mean 30%. A model that ranks well but is systematically overconfident produces
confident nonsense downstream and the allocator has no way to know. Isotonic
regression on a held-out split, reported with a reliability diagram, Brier score
and expected calibration error rather than AUC. Isotonic is itself monotone in
its input, so calibration preserves monotonicity in the amount.

**Split three ways, by mandate.** Fit, calibrate, test. Two traps sit here and
both were walked into on the first attempt:

* Splitting *rows* rather than mandates puts attempt 1 in train and attempt 2 in
  calibration. They share a customer, a balance path and an issuer, so the split
  leaks.
* Measuring calibration on the split the calibrator was fitted to reports a
  perfect result every time. Isotonic regression is a step function with enough
  freedom to match the empirical frequencies of its own fitting set exactly —
  the first run of this file reported ECE = 0.0000 and predicted frequencies
  equal to observed ones to three decimals, which is not a calibrated model but
  an in-sample fit. Everything reported here is measured on a third split that
  neither the booster nor the calibrator has seen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Sequence

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler

from ..core.money import Paise
from .dataset import Dataset
from .features import FEATURE_NAMES, N_FEATURES, amount_axis

#: -1 forces the model to be non-increasing in this feature.
#:
#: Only `amount_ratio` gets it, and the distinction matters. The ratio is
#: normalised *within* a mandate, so "demand a larger share of this customer's
#: normal debit" genuinely can only lower the chance of clearing — that is the
#: survival function.
#:
#: `log_reference_amount` is the mandate's scale *across* customers, and
#: constraining it asserts something false: that a customer on a ₹2,499
#: subscription is less likely to pay than one on ₹149. They are not; they have
#: more money. Constraining both scored worse than a plain logistic regression.
#:
#: It is also why that feature is the *reference* amount rather than the
#: attempted one. Sweeping the ratio must move exactly one column, or an
#: unconstrained feature moving alongside the constrained one silently voids the
#: guarantee — which it did, until the monotonicity test caught a curve rising
#: at 0.8 of the debit.
MONOTONIC_CST: Final[tuple[int, ...]] = tuple(
    -1 if name == "amount_ratio" else 0 for name in FEATURE_NAMES
)


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """Every number here is measured on a split neither the booster nor the
    calibrator has seen."""

    n: int
    brier: float
    ece: float
    auc: float
    #: (mean predicted, observed frequency, count) per bin — the reliability diagram.
    bins: tuple[tuple[float, float, int], ...]
    baseline_brier: float
    logistic_brier: float

    @property
    def skill(self) -> float:
        """Brier skill score against always predicting the base rate."""
        if self.baseline_brier <= 0:
            return float("nan")
        return 1.0 - self.brier / self.baseline_brier

    def render(self) -> str:
        lines = [
            f"  rows                 {self.n:,}",
            f"  Brier                {self.brier:.4f}   (base rate {self.baseline_brier:.4f},"
            f" logistic {self.logistic_brier:.4f})",
            f"  Brier skill          {self.skill:>7.1%}",
            f"  expected calib. err  {self.ece:.4f}",
            f"  AUC                  {self.auc:.4f}",
            "",
            "  reliability                predicted   observed   n",
        ]
        for pred, obs, n in self.bins:
            bar = "#" * int(round(obs * 30))
            lines.append(f"    {'':<22}{pred:>9.3f}{obs:>11.3f}{n:>6,}  {bar}")
        return "\n".join(lines)


@dataclass
class TrainedModel:
    booster: HistGradientBoostingClassifier
    calibrator: IsotonicRegression
    report: CalibrationReport
    feature_names: tuple[str, ...] = FEATURE_NAMES

    def predict(self, X: np.ndarray) -> np.ndarray:
        """Calibrated P(success) for each row."""
        if X.size == 0:
            return np.empty(0)
        raw = self.booster.predict_proba(X)[:, 1]
        return np.clip(self.calibrator.predict(raw), 1e-6, 1.0 - 1e-6)

    def survival(self, row: np.ndarray, ratios: Sequence[float]) -> np.ndarray:
        """P(success) across a grid of amount ratios — the survival curve.

        Guaranteed non-increasing: the booster is constrained and isotonic
        calibration is monotone in its input.
        """
        order = np.argsort(ratios)
        grid = np.asarray(ratios, dtype=float)[order]
        probs = self.predict(amount_axis(row, grid.tolist()))
        out = np.empty_like(probs)
        out[order] = probs
        return out

    def expected_value(
        self, row: np.ndarray, ratios: Sequence[float], amount_due: Paise
    ) -> np.ndarray:
        """`a · P(balance ≥ a)` across the grid, in paise."""
        p = self.survival(row, ratios)
        return np.asarray(ratios, dtype=float) * float(amount_due) * p

    def best_amount(
        self, row: np.ndarray, ratios: Sequence[float], amount_due: Paise
    ) -> tuple[float, float]:
        """The amount ratio maximising expected collection, and that value."""
        ev = self.expected_value(row, ratios, amount_due)
        i = int(np.argmax(ev))
        return float(ratios[i]), float(ev[i])


class SurvivalModel:
    """Trainer. Kept separate so a fitted model is an immutable artifact."""

    @staticmethod
    def fit(
        data: Dataset,
        *,
        seed: int = 0,
        calibration_share: float = 0.2,
        test_share: float = 0.25,
    ) -> TrainedModel:
        if data.n < 500:
            raise ValueError(f"not enough training rows: {data.n}")

        fit_idx, cal_idx, test_idx = _grouped_split(
            data.groups, calibration_share, test_share, seed
        )
        X_fit, y_fit = data.X[fit_idx], data.y[fit_idx]
        X_cal, y_cal = data.X[cal_idx], data.y[cal_idx]
        X_test, y_test = data.X[test_idx], data.y[test_idx]

        booster = HistGradientBoostingClassifier(
            max_iter=350,
            learning_rate=0.06,
            max_leaf_nodes=31,
            min_samples_leaf=40,
            l2_regularization=1.0,
            monotonic_cst=list(MONOTONIC_CST),
            early_stopping=True,
            validation_fraction=0.15,
            random_state=seed,
        )
        booster.fit(X_fit, y_fit)

        raw_cal = booster.predict_proba(X_cal)[:, 1]
        calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        calibrator.fit(raw_cal, y_cal)

        # Reported on the untouched third split. Measuring on `X_cal` would
        # report the calibrator's own fit and always look perfect.
        raw_test = booster.predict_proba(X_test)[:, 1]
        calibrated_test = np.clip(calibrator.predict(raw_test), 1e-6, 1 - 1e-6)
        report = _assess(X_fit, y_fit, X_test, y_test, calibrated_test, seed)
        return TrainedModel(booster=booster, calibrator=calibrator, report=report)


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _grouped_split(
    groups: np.ndarray, cal_share: float, test_share: float, seed: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Fit / calibrate / test, split by mandate rather than by row."""
    unique = np.unique(groups)
    rng = np.random.default_rng(seed)
    shuffled = rng.permutation(unique)
    n = len(shuffled)
    n_cal = max(1, int(n * cal_share))
    n_test = max(1, int(n * test_share))

    cal = set(shuffled[:n_cal])
    test = set(shuffled[n_cal : n_cal + n_test])

    which = np.array([0 if g in cal else 1 if g in test else 2 for g in groups])
    return (
        np.flatnonzero(which == 2),   # fit
        np.flatnonzero(which == 0),   # calibrate
        np.flatnonzero(which == 1),   # test
    )


def _assess(
    X_fit: np.ndarray,
    y_fit: np.ndarray,
    X_cal: np.ndarray,
    y_cal: np.ndarray,
    calibrated: np.ndarray,
    seed: int,
) -> CalibrationReport:
    base_rate = float(y_fit.mean())
    baseline_brier = float(np.mean((base_rate - y_cal) ** 2))

    # A plain logistic regression, reported alongside. If the boosted model is
    # not clearly better than a linear one, the extra machinery is not earning
    # its place and should be said so out loud.
    scaler = StandardScaler().fit(X_fit)
    logit = LogisticRegression(max_iter=2000, C=1.0)
    logit.fit(scaler.transform(X_fit), y_fit)
    logistic_p = logit.predict_proba(scaler.transform(X_cal))[:, 1]

    return CalibrationReport(
        n=int(y_cal.size),
        brier=float(brier_score_loss(y_cal, calibrated)),
        ece=_expected_calibration_error(y_cal, calibrated),
        auc=float(roc_auc_score(y_cal, calibrated)) if len(np.unique(y_cal)) > 1 else float("nan"),
        bins=_reliability(y_cal, calibrated),
        baseline_brier=baseline_brier,
        logistic_brier=float(brier_score_loss(y_cal, logistic_p)),
    )


def _reliability(
    y: np.ndarray, p: np.ndarray, n_bins: int = 10
) -> tuple[tuple[float, float, int], ...]:
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    out: list[tuple[float, float, int]] = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = (p >= lo) & (p < hi if hi < 1.0 else p <= hi)
        if sel.sum() < 20:
            continue
        out.append((float(p[sel].mean()), float(y[sel].mean()), int(sel.sum())))
    return tuple(out)


def _expected_calibration_error(y: np.ndarray, p: np.ndarray, n_bins: int = 15) -> float:
    """Weighted mean gap between predicted probability and observed frequency.

    Reported instead of accuracy because the number is consumed by an
    expected-value calculation, where being right *on average within a bucket*
    is what matters.
    """
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    total, err = 0, 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = (p >= lo) & (p < hi if hi < 1.0 else p <= hi)
        n = int(sel.sum())
        if n == 0:
            continue
        err += n * abs(float(p[sel].mean()) - float(y[sel].mean()))
        total += n
    return err / total if total else float("nan")
