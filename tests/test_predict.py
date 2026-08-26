"""The survival model and the phase filter.

The tests that matter here are not "is accuracy high". They are the ones that
would catch the model being *exploitable* by the allocator: a non-monotone
survival curve with a fake maximum, a calibration number measured on the split
it was fitted to, or a split that leaks a customer across the boundary.
"""

from __future__ import annotations

import numpy as np
import pytest

from mandate_recovery.belief.filter import (
    CYCLE_DAYS,
    PhaseBelief,
    PhaseProfile,
    fit_profile,
)
from mandate_recovery.predict.dataset import collect
from mandate_recovery.predict.features import FEATURE_NAMES, amount_axis
from mandate_recovery.predict.model import MONOTONIC_CST, SurvivalModel
from mandate_recovery.sim.world import WorldConfig

#: Large enough for the skill assertions below to mean something. Exploring
#: delays spreads each mandate's attempts across the month, which is what makes
#: the day-of-month feature learnable — and also means fewer labelled rows per
#: mandate, so the fixture needs more of them than it did before.
SMALL = WorldConfig(n_mandates=1600, days=35)


@pytest.fixture(scope="module")
def data():
    return collect(range(0, 8), SMALL)


@pytest.fixture(scope="module")
def model(data):
    return SurvivalModel.fit(data, seed=0)


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #


def test_exploration_covers_the_amount_axis(data) -> None:
    """A model fitted only on full amounts cannot express the amount lever, and
    would extrapolate confidently into a region it has never seen."""
    ratios = data.X[:, 0]
    assert ratios.min() < 0.5
    assert ratios.max() >= 0.99
    assert len(np.unique(np.round(ratios, 2))) > 8


def test_exploration_covers_the_time_axis(data) -> None:
    """Both axes, and only the legal parts of them.

    Peak windows are 10:00-13:00 and 17:00-21:30, so the hour coverage is
    expected to have holes — exploration proposing an illegal slot would simply
    be vetoed and collect nothing.
    """
    hours = data.X[:, 5]
    assert hours.max() - hours.min() > 12.0
    assert not ((hours >= 10.0) & (hours < 13.0)).any(), "explored a peak hour"
    assert not ((hours >= 17.0) & (hours < 21.5)).any(), "explored a peak hour"
    assert len(np.unique(data.X[:, 2])) > 10


def test_training_uses_only_training_seeds(data) -> None:
    """Evaluation seeds are held out. Asserted, not promised."""
    seeds = {int(g.split(":")[0]) for g in data.groups}
    assert seeds <= set(range(0, 8))
    assert not seeds & set(range(100, 110))


# --------------------------------------------------------------------------- #
# The survival curve
# --------------------------------------------------------------------------- #


def test_only_the_ratio_is_monotonically_constrained() -> None:
    """`amount_ratio` is normalised within a mandate, so demanding a larger share
    can only lower the chance of clearing.

    `log_reference_amount` is the mandate's scale *across* customers:
    constraining it would assert that a customer on a ₹2,499 plan is less likely
    to pay than one on ₹149. They have more money. The first version constrained
    both and scored worse than a plain logistic regression.
    """
    by_name = dict(zip(FEATURE_NAMES, MONOTONIC_CST))
    assert by_name["amount_ratio"] == -1
    assert by_name["log_reference_amount"] == 0
    assert sum(1 for v in MONOTONIC_CST if v != 0) == 1


def test_the_survival_curve_never_increases_with_the_amount(model, data) -> None:
    """If it did, `a · P(a)` would have fake local maxima and the dynamic
    programme would find them — optimising the model rather than the world."""
    ratios = [0.15, 0.3, 0.45, 0.6, 0.8, 1.0]
    rng = np.random.default_rng(0)
    for i in rng.choice(data.n, size=60, replace=False):
        p = model.survival(data.X[i], ratios)
        assert np.all(np.diff(p) <= 1e-9), f"row {i}: {p}"


def test_expected_value_is_computed_over_the_whole_curve(model, data) -> None:
    ev = model.expected_value(data.X[0], [0.5, 1.0], amount_due=100_00)
    p = model.survival(data.X[0], [0.5, 1.0])
    assert ev[0] == pytest.approx(0.5 * 100_00 * p[0])
    assert ev[1] == pytest.approx(1.0 * 100_00 * p[1])


def test_amount_axis_only_moves_the_amount_features(data) -> None:
    row = data.X[3]
    grid = amount_axis(row, [0.25, 0.5, 1.0])
    for col in range(2, row.size):
        assert np.allclose(grid[:, col], row[col]), FEATURE_NAMES[col]
    assert grid[0, 0] < grid[1, 0] < grid[2, 0]


# --------------------------------------------------------------------------- #
# Calibration
# --------------------------------------------------------------------------- #


def test_calibration_is_measured_on_an_unseen_split(model) -> None:
    """The first version fitted the isotonic calibrator and then measured
    calibration on the same split, reporting ECE = 0.0000 and predicted
    frequencies equal to observed ones to three decimals. Isotonic regression is
    a step function with enough freedom to fit its own calibration set exactly;
    that number was in-sample and meaningless.
    """
    r = model.report
    assert r.n > 200
    assert r.ece > 0.0, "a perfect ECE means it is being measured in-sample again"
    assert r.ece < 0.10


def test_the_model_beats_predicting_the_base_rate(model) -> None:
    assert model.report.brier < model.report.baseline_brier
    assert model.report.skill > 0.02


def test_the_boosted_model_is_at_least_competitive_with_a_linear_one(model) -> None:
    """Where the extra machinery earns its place, and where it does not.

    Measured honestly: at this fixture's data volume (~9k rows) the boosted
    model and a plain logistic regression are indistinguishable — Brier 0.2004
    against 0.2000. The gap only opens with more data; at the volume the
    reported results use (~25k rows) it is 0.1906 against 0.1933.

    So the assertion here is competitiveness, not superiority. Claiming the
    tree wins at every scale would be asserting something the numbers do not
    support, and a threshold tuned until it passed would be worse.
    """
    assert model.report.brier <= model.report.logistic_brier * 1.02


def test_probabilities_stay_in_range(model, data) -> None:
    p = model.predict(data.X[:500])
    assert p.min() > 0.0 and p.max() < 1.0


# --------------------------------------------------------------------------- #
# The phase filter
# --------------------------------------------------------------------------- #


def test_em_recovers_a_planted_profile() -> None:
    """Synthetic customers with known phases. Neither the phases nor the profile
    are given to the fitter."""
    rng = np.random.default_rng(7)
    true = np.full(CYCLE_DAYS, 0.05)
    true[0:5] = [0.55, 0.70, 0.65, 0.45, 0.25]

    groups, days, ys = [], [], []
    for c in range(500):
        phase = int(rng.integers(0, CYCLE_DAYS))
        for _ in range(6):
            d = int(rng.integers(0, CYCLE_DAYS))
            p = true[(d - phase) % CYCLE_DAYS]
            groups.append(f"c{c}")
            days.append(d)
            ys.append(int(rng.random() < p))

    fitted = fit_profile(groups, days, ys, seed=0)
    assert fitted.peak_lag in (0, 1, 2, 3), fitted.peak_lag
    assert fitted.contrast > 3.0
    # The shape, not just the peak.
    assert fitted.profile[1] > fitted.profile[10]
    assert fitted.profile[2] > fitted.profile[15]


def test_a_belief_starts_undecided() -> None:
    prof = PhaseProfile(np.full(CYCLE_DAYS, 0.3), iterations=1, log_likelihood=0.0)
    b = PhaseBelief(prof)
    assert b.entropy_bits == pytest.approx(np.log2(CYCLE_DAYS), abs=1e-6)
    assert np.allclose(b.posterior.sum(), 1.0)


def test_observations_sharpen_the_belief() -> None:
    true = np.full(CYCLE_DAYS, 0.05)
    true[0:4] = [0.6, 0.75, 0.6, 0.35]
    prof = PhaseProfile(true, iterations=1, log_likelihood=0.0)

    b = PhaseBelief(prof)
    before = b.entropy_bits
    for day, ok in [(8, True), (9, True), (20, False)]:
        b.update(day, ok)
    assert b.entropy_bits < before - 1.0
    assert np.allclose(b.posterior.sum(), 1.0)


def test_the_belief_finds_a_planted_phase() -> None:
    """Three observations from a customer paid on the 7th should point there."""
    true = np.full(CYCLE_DAYS, 0.04)
    true[0:4] = [0.65, 0.8, 0.6, 0.3]
    prof = PhaseProfile(true, iterations=1, log_likelihood=0.0)

    b = PhaseBelief(prof)
    b.update(7, True)
    b.update(8, True)
    b.update(20, False)
    assert abs(b.map_phase - 7) <= 2, b.map_phase
    assert min(abs(d - 8) for d in b.best_days(3)) <= 2


def test_a_flat_profile_teaches_the_belief_nothing() -> None:
    """Guards against reporting confidence that came from the prior's shape
    rather than from evidence."""
    prof = PhaseProfile(np.full(CYCLE_DAYS, 0.25), iterations=1, log_likelihood=0.0)
    b = PhaseBelief(prof)
    before = b.entropy_bits
    for day in (3, 11, 19):
        b.update(day, False)
    assert b.entropy_bits == pytest.approx(before, abs=1e-9)
