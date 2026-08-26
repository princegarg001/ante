"""The world must stay inside its published bands, on every seed.

This is the honesty gate. A simulator that flatters the agent is the most likely
way this project produces a meaningless result, and the failure is silent — the
numbers look better, not broken.

So the bands are checked in CI, across seeds, and this file is deliberately
capable of failing. If it ever goes red the correct response is to fix the world,
or to fix the band *and say why in the source*, not to relax it quietly. There is
one band that was already replaced that way, and the reason is recorded next to
it in `calibrate.py`.
"""

from __future__ import annotations

import pytest

from mandate_recovery.sim.calibrate import BANDS, measure
from mandate_recovery.sim.world import WorldConfig

FAST = WorldConfig(n_mandates=700, days=35)


@pytest.mark.parametrize("seed", [42, 101, 202])
def test_base_rates_hold_on_every_seed(seed: int) -> None:
    report = measure(seed, FAST)
    assert report.ok, "\n".join(
        f"{b.name}: {report.measured[b.name]:.3f} not in [{b.low}, {b.high}] — {b.source}"
        for b in report.failures
    )


def test_the_failure_mix_is_dominated_by_business_declines() -> None:
    """Insufficient funds, not technical failures. A world where outages
    dominate would reward a completely different policy, and would not be this
    market."""
    report = measure(42, FAST)
    assert (
        report.measured["insufficient_funds_share"]
        > 3 * report.measured["technical_failure_share"]
    )


def test_every_band_is_capable_of_failing() -> None:
    """A band that no plausible world could fall outside is decoration.

    Each band must be a proper interval, and none may span the whole unit range
    — which is how a band gets quietly neutered when it becomes inconvenient.
    """
    for band in BANDS:
        assert band.low < band.high, band.name
        assert band.high - band.low < 0.95, f"{band.name} is too wide to bind"
        assert band.source, f"{band.name} has no source recorded"


def test_a_kinder_world_is_detected() -> None:
    """The gate has to catch the thing it exists to catch.

    Removing the unrecoverable segment entirely makes the world easier in a way
    that would flatter any policy. The calibration must notice.
    """
    generous = WorldConfig(n_mandates=700, days=35, unrecoverable_share=0.0)
    report = measure(42, generous)
    assert not report.ok
    assert any(b.name == "unrecoverable_share" for b in report.failures)


@pytest.mark.slow
def test_bands_hold_at_full_book_size() -> None:
    report = measure(42, WorldConfig(n_mandates=3_000, days=35))
    assert report.ok, "\n".join(
        f"{b.name}: {report.measured[b.name]:.3f}" for b in report.failures
    )
