"""A pay-cycle phase posterior, fitted by expectation-maximisation.

The model in `predict/` conditions on the day of the month, so it learns the
*population's* seasonality: the average customer is likelier to pay early in the
month. What it cannot express is that **this** customer is paid on the 7th.

That is a per-mandate latent variable with very few observations attached to it
— often two or three — which is exactly the situation a posterior is for. A
point estimate from three coin flips is not an estimate.

## The model

Each customer has a hidden phase `φ ∈ {0…29}`: the day of the month their
balance peaks. There is one shared profile `f(k)` giving the probability a debit
clears `k` days after that peak, and it is the same curve for everyone — what
differs between customers is only where they sit on it.

    P(success | φ, day d) = f((d − φ) mod 30)

Neither `φ` nor `f` is observed. Both are recovered together by EM over the
training logs:

    E-step   posterior over φ for each mandate, given the current f
    M-step   re-estimate f as the posterior-weighted success rate at each lag

The profile is smoothed circularly at each M-step. Without it the curve chases
noise in lags that few mandates land on, and the posterior then sharpens on
evidence that is not there.

## Why this is worth having

At decision time the agent holds a distribution over phase and can score any
candidate day by integrating over it. After two failures on the 3rd and a
success on the 9th, the posterior concentrates on a phase near the 7th, and the
agent starts preferring the 8th. That is a genuine inference from three
observations, and it is visible — which makes it the part of the system worth
putting on screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Sequence

import numpy as np

#: A billing month, in days. Phases and lags share this modulus.
CYCLE_DAYS: Final[int] = 30

#: Guards against a lag with two observations reaching probability 0 or 1.
_PSEUDO_SUCCESS: Final[float] = 1.5
_PSEUDO_TOTAL: Final[float] = 12.0


@dataclass(frozen=True, slots=True)
class PhaseProfile:
    """`f(k)` — probability a debit clears `k` days after the customer's peak."""

    profile: np.ndarray            # (CYCLE_DAYS,)
    iterations: int
    log_likelihood: float

    def likelihood(self, phase: int, day: int) -> float:
        return float(self.profile[(day - phase) % CYCLE_DAYS])

    def curve(self) -> np.ndarray:
        return self.profile.copy()

    @property
    def peak_lag(self) -> int:
        """Days after payday at which collection is most likely.

        Expected to be small. If it is not, either the profile has not converged
        or the world does not have the structure the design assumes.
        """
        return int(np.argmax(self.profile))

    @property
    def contrast(self) -> float:
        """Best lag over worst. A profile near 1.0 carries no information, and a
        posterior built on it will never move."""
        lo = float(self.profile.min())
        return float(self.profile.max()) / lo if lo > 0 else float("inf")


@dataclass
class PhaseBelief:
    """One mandate's posterior over phase, updated from its own outcomes."""

    profile: PhaseProfile
    posterior: np.ndarray = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.posterior is None:
            self.posterior = np.full(CYCLE_DAYS, 1.0 / CYCLE_DAYS)

    def update(self, day_of_month: int, success: bool) -> None:
        """Exact Bayes. One observation, one multiplication."""
        lags = (day_of_month - np.arange(CYCLE_DAYS)) % CYCLE_DAYS
        p = self.profile.profile[lags]
        like = p if success else (1.0 - p)
        post = self.posterior * like
        total = post.sum()
        if total > 0:
            self.posterior = post / total

    def probability(self, day_of_month: int) -> float:
        """P(success on this day), integrating over the phase posterior."""
        lags = (day_of_month - np.arange(CYCLE_DAYS)) % CYCLE_DAYS
        return float(np.dot(self.posterior, self.profile.profile[lags]))

    def best_days(self, k: int = 3) -> list[int]:
        scores = [self.probability(d) for d in range(1, CYCLE_DAYS + 1)]
        return [int(d) for d in np.argsort(scores)[::-1][:k] + 1]

    @property
    def entropy_bits(self) -> float:
        """How undecided the belief is. Starts at log2(30) ≈ 4.91 and falls as
        evidence arrives — the number to put on screen."""
        p = self.posterior[self.posterior > 0]
        return float(-np.sum(p * np.log2(p)))

    @property
    def map_phase(self) -> int:
        return int(np.argmax(self.posterior))


def fit_profile(
    groups: Sequence[str],
    days: Sequence[int],
    successes: Sequence[int],
    *,
    max_iterations: int = 60,
    tolerance: float = 1e-5,
    smoothing: float = 1.0,
    seed: int = 0,
) -> PhaseProfile:
    """Recover the shared profile and the per-mandate phases together, by EM.

    Nothing here sees a phase label, because none exists — that is the point.
    """
    g = np.asarray(groups)
    d = np.asarray(days, dtype=int) % CYCLE_DAYS
    y = np.asarray(successes, dtype=float)

    order = np.argsort(g, kind="stable")
    g, d, y = g[order], d[order], y[order]
    boundaries = np.flatnonzero(np.r_[True, g[1:] != g[:-1], True])
    spans = list(zip(boundaries[:-1], boundaries[1:]))

    rng = np.random.default_rng(seed)
    # A gently peaked start. Exactly uniform is a stationary point of EM.
    lags = np.arange(CYCLE_DAYS)
    profile = 0.12 + 0.10 * np.exp(-((lags % CYCLE_DAYS) ** 2) / 18.0)
    profile += rng.normal(0.0, 0.005, CYCLE_DAYS)
    profile = np.clip(profile, 0.02, 0.95)

    phase_grid = np.arange(CYCLE_DAYS)
    previous_ll = -np.inf
    iterations = 0

    for iterations in range(1, max_iterations + 1):
        num = np.full(CYCLE_DAYS, _PSEUDO_SUCCESS)
        den = np.full(CYCLE_DAYS, _PSEUDO_TOTAL)
        total_ll = 0.0

        for lo, hi in spans:
            dd, yy = d[lo:hi], y[lo:hi]
            lag = (dd[None, :] - phase_grid[:, None]) % CYCLE_DAYS   # (phase, obs)
            p = profile[lag]
            log_like = np.sum(yy * np.log(p) + (1 - yy) * np.log1p(-p), axis=1)

            m = log_like.max()
            w = np.exp(log_like - m)
            total = w.sum()
            total_ll += m + np.log(total)
            w /= total

            np.add.at(num, lag.ravel(), np.repeat(w, dd.size) * np.tile(yy, CYCLE_DAYS))
            np.add.at(den, lag.ravel(), np.repeat(w, dd.size))

        profile = np.clip(num / den, 0.01, 0.99)
        if smoothing > 0:
            profile = _circular_smooth(profile, smoothing)

        if abs(total_ll - previous_ll) < tolerance * max(1.0, abs(previous_ll)):
            previous_ll = total_ll
            break
        previous_ll = total_ll

    return PhaseProfile(
        profile=profile, iterations=iterations, log_likelihood=float(previous_ll)
    )


def _circular_smooth(values: np.ndarray, strength: float) -> np.ndarray:
    """Wrap-around three-tap smoothing. The 30th is adjacent to the 1st."""
    w = np.array([strength, 2.0, strength])
    w = w / w.sum()
    padded = np.r_[values[-1], values, values[0]]
    return np.convolve(padded, w, mode="valid")
