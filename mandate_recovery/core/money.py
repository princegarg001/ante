"""Money is always an integer count of paise. Never a float.

A float rupee amount is a bug waiting for a rounding difference between what the
policy valued, what the constraint layer checked, and what the ledger recorded.
Every amount that crosses a module boundary in this system is `Paise`.
"""

from __future__ import annotations

from typing import Final

Paise = int

PAISE_PER_RUPEE: Final[int] = 100


def rupees(amount: int | float) -> Paise:
    """Convert a rupee amount to paise, rounding half away from zero.

    Intended for constants and test fixtures, not for arithmetic on live amounts.
    """
    return int(round(amount * PAISE_PER_RUPEE))


def to_rupees(paise: Paise) -> float:
    """Lossy — presentation only. Never feed the result back into a decision."""
    return paise / PAISE_PER_RUPEE


def fmt(paise: Paise) -> str:
    """Render paise in the Indian digit grouping, e.g. 10000000 -> '₹1,00,000.00'."""
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), PAISE_PER_RUPEE)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return f"{sign}₹{digits}.{frac:02d}"
