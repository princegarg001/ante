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

Instruments, as identified on 31 August 2026:

    RBI/DPSS/2026-27/396         Digital Payments - E-mandate Framework, 2026
                                 21 April 2026, ss. 10(2) r/w 18 PSS Act 2007
    NPCI/UPI/OC/215A/2025-26     Guidelines on usage of UPI API
                                 21 May 2025, enforced from 1 August 2025
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


def test_c5a_notification_minimum_is_twenty_four_hours() -> None:
    """NPCI/UPI/OC/215A/2025-26 — NPCI validates the 24h minimum.

    This half is regulation.
    """
    assert PDN_MIN_LEAD == timedelta(hours=24)


def test_c5b_notification_ceiling_is_provider_policy_not_regulation() -> None:
    """The correction from the 31 August verification pass.

    The upper bound is *not* in the circular. It comes from payment providers,
    and they disagree: 24-48h, 36-48h and 48-72h are all quoted. Three different
    ceilings cannot be the same regulation, so the honest reading is that the
    floor is regulatory and the ceiling is the provider's.

    The aperture is still real — a merchant on a given PSP genuinely cannot
    notify earlier than that provider allows — so the two-sided structure the
    allocator plans against is the environment a merchant faces. What changed is
    the claim, not the constant. 48h is the tightest commonly quoted ceiling and
    therefore the conservative choice.

    This test exists so that nobody later cites C5's ceiling as NPCI regulation.
    """
    assert PDN_MAX_LEAD == timedelta(hours=48)
    assert PDN_MAX_LEAD > PDN_MIN_LEAD


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
