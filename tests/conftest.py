"""Shared fixtures. One canonical mandate, deliberately boring, so that a failing
test points at the rule under test rather than at the fixture."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from mandate_recovery.core.clock import IST
from mandate_recovery.core.money import rupees
from mandate_recovery.core.types import (
    CauseClass,
    Category,
    MandateState,
    MandateStatus,
    PDN,
)

#: A Tuesday, chosen so weekday-sensitive logic added later has a stable anchor.
ORIGIN = datetime(2026, 9, 1, 0, 0, tzinfo=IST)


@pytest.fixture
def origin() -> datetime:
    return ORIGIN


def make_state(**overrides: object) -> MandateState:
    base = dict(
        mandate_id="MND_0001",
        status=MandateStatus.LIVE,
        cause=CauseClass.INSUFFICIENT_FUNDS,
        attempts_used=0,
        is_first_presentation=True,
        amount_due_paise=rupees(499),
        max_amount_paise=rupees(1_000),
        category=Category.STANDARD,
        cycle_end=ORIGIN + timedelta(days=30),
        validity_end=ORIGIN + timedelta(days=365),
        pending_pdn=None,
        contacts_used=0,
        issuer_id="HDFC",
        variable_amount_allowed=False,
    )
    base.update(overrides)
    return MandateState(**base)  # type: ignore[arg-type]


@pytest.fixture
def state() -> MandateState:
    return make_state()


@pytest.fixture
def pending_pdn() -> PDN:
    return PDN(
        notified_at=ORIGIN,
        execute_at=ORIGIN + timedelta(hours=30),
        amount_paise=rupees(499),
    )
