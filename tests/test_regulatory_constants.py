"""The constants *are* the regulation. Pin them to literals.

Found by mutation testing: changing `MAX_ATTEMPTS` from 4 to 5 left the whole
suite green. Every test that referenced the cap imported the symbol, so the
assertions moved with the mutation — including the model checker's supposedly
independent specification. Nothing anywhere pinned the number 4 to the circular
it comes from.

This file is the fix, and the pattern is worth stating: a regulatory constant must
be asserted against a literal with its source next to it, so that changing it
requires deliberately editing a test that cites a circular. That is the difference
between a value being configured and a value being *claimed*.

Sources are tracked per-row in COMPLIANCE.md. If a row there is corrected during
primary-source verification, this file is where the change has to be made, and the
diff will show a regulation being restated rather than a constant being tweaked.
"""

from __future__ import annotations

from datetime import time, timedelta

from mandate_recovery.constraints.modelcheck import (
    _CUTOFF_MINUTE,
    _MAX_LEAD_S,
    _MIN_LEAD_S,
    _PEAK_MINUTES,
)
from mandate_recovery.constraints.rules import (
    MAX_ATTEMPTS,
    PDN_LATE_CUTOFF,
    PDN_MAX_LEAD,
    PDN_MIN_LEAD,
)
from mandate_recovery.core.clock import PEAK_WINDOWS, SLOT_MINUTES
from mandate_recovery.core.money import rupees
from mandate_recovery.core.types import AFA_FREE_CEILING, Category


def test_c1_retry_budget_is_one_execution_plus_three_retries() -> None:
    """NPCI UPI/API Guidelines 2025 — 1 original + 3 retries per mandate."""
    assert MAX_ATTEMPTS == 4


def test_c2_peak_windows_are_1000_1300_and_1700_2130_ist() -> None:
    """NPCI UPI/API Guidelines 2025 — Autopay execution barred in peak hours."""
    assert PEAK_WINDOWS == (
        (time(10, 0), time(13, 0)),
        (time(17, 0), time(21, 30)),
    )


def test_c3_non_peak_capacity_is_sixteen_and_a_half_hours_a_day() -> None:
    """Derived from C2, and the number the slot grid is sized against."""
    peak_minutes = sum(
        (hi.hour * 60 + hi.minute) - (lo.hour * 60 + lo.minute) for lo, hi in PEAK_WINDOWS
    )
    assert peak_minutes == 7 * 60 + 30
    assert (24 * 60 - peak_minutes) == 16 * 60 + 30
    assert SLOT_MINUTES == 30


def test_c5_notification_aperture_is_two_sided_24h_to_48h() -> None:
    """The constraint the first draft of the build plan got wrong: it modelled the
    rule as `execute_at >= now + 24h`, a floor with no ceiling. Notifying too early
    is as non-compliant as notifying too late."""
    assert PDN_MIN_LEAD == timedelta(hours=24)
    assert PDN_MAX_LEAD == timedelta(hours=48)


def test_c7_late_notification_cutoff_is_2350_ist() -> None:
    assert PDN_LATE_CUTOFF == time(23, 50)


def test_c15_afa_free_ceiling_is_fifteen_thousand_rupees() -> None:
    """RBI E-mandate Framework 2026 — ₹15,000 per recurring transaction."""
    assert AFA_FREE_CEILING[Category.STANDARD] == rupees(15_000) == 1_500_000


def test_c16_raised_ceiling_is_one_lakh_for_eligible_categories() -> None:
    """RBI 2026 — insurance premiums, mutual-fund SIPs, credit-card bills."""
    for category in (Category.INSURANCE, Category.MF_SIP, Category.CC_BILL):
        assert AFA_FREE_CEILING[category] == rupees(1_00_000) == 10_000_000


# --------------------------------------------------------------------------- #
# The model checker's independent specification must itself be pinned.
# --------------------------------------------------------------------------- #


def test_the_independent_spec_restates_the_same_regulation() -> None:
    """`modelcheck._inv_violations` deliberately duplicates the regulation instead
    of importing the implementation's predicates, so that the two can disagree.

    That only works if the duplicate is *correct*. Mutation testing showed it could
    silently drift — zeroing the morning peak window in the checker left the suite
    green, because the checker is only ever asked whether permitted actions are
    legal, and permitted actions were legal for other reasons.

    So the duplicate is pinned against the same literals as the original. The two
    are still computed independently at runtime; they are now anchored to the same
    published values at test time.
    """
    assert _PEAK_MINUTES == ((10 * 60, 13 * 60), (17 * 60, 21 * 60 + 30))
    assert _MIN_LEAD_S == 24 * 3600
    assert _MAX_LEAD_S == 48 * 3600
    assert _CUTOFF_MINUTE == 23 * 60 + 50
