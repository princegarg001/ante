"""Boundary tests for the constraint layer.

Each test pins one rule at the exact instant or paisa where it starts to bite.
Interior cases are covered exhaustively by the model checker; what unit tests are
for is the edge, because that is where a regulation gets implemented wrong.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from mandate_recovery.constraints import all_vetoes, is_permitted
from mandate_recovery.constraints.rules import (
    DEFAULT_CONTACT_CAP,
    MAX_ATTEMPTS,
    RULES,
    RuleKind,
)
from mandate_recovery.core.clock import IST
from mandate_recovery.core.money import rupees
from mandate_recovery.core.types import (
    CancelPending,
    CauseClass,
    Category,
    Commit,
    MandateStatus,
    NotifyOnly,
    PDN,
    Stop,
    Wait,
)
from tests.conftest import ORIGIN, make_state


def commit(hours: float, amount: int = rupees(499)) -> Commit:
    return Commit(execute_at=ORIGIN + timedelta(hours=hours), amount_paise=amount)


def vetoed_by(action, state, clock=ORIGIN) -> set[str]:
    return {v.rule_id for v in all_vetoes(action, state, clock)}


# --------------------------------------------------------------------------- #
# The canonical legal action
# --------------------------------------------------------------------------- #


def test_a_lawful_commit_is_permitted(state) -> None:
    """00:00 + 24h lands at 00:00 next day — non-peak, aligned, inside the aperture."""
    verdict = is_permitted(commit(24), state, ORIGIN)
    assert verdict.allowed, verdict


# --------------------------------------------------------------------------- #
# C5 — the two-sided pre-debit notification aperture
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("lead_h", [24, 30, 48])
def test_c5_permits_inside_the_aperture(state, lead_h: int) -> None:
    assert is_permitted(commit(lead_h), state, ORIGIN).allowed


@pytest.mark.parametrize("lead_h", [0, 12, 23.5, 48.5, 72])
def test_c5_vetoes_outside_the_aperture(state, lead_h: float) -> None:
    """Both edges. The 48h ceiling is what the original plan missed: notifying too
    early is as illegal as notifying too late."""
    assert "C5" in vetoed_by(commit(lead_h), state)


def test_c5_boundaries_are_inclusive(state) -> None:
    assert "C5" not in vetoed_by(commit(24), state)
    assert "C5" not in vetoed_by(commit(48), state)
    assert "C5" in vetoed_by(commit(23.5), state)
    assert "C5" in vetoed_by(commit(48.5), state)


# --------------------------------------------------------------------------- #
# C2 — peak windows
# --------------------------------------------------------------------------- #


def test_c2_vetoes_a_peak_hour_execution(state) -> None:
    """34h after midnight is 10:00 the next day — inside the morning peak."""
    assert "C2" in vetoed_by(commit(34), state)


def test_c2_permits_the_instant_the_evening_peak_closes(state) -> None:
    """45.5h is 21:30 — the peak window is half-open, so this is legal."""
    assert "C2" not in vetoed_by(commit(45.5), state)
    assert "C2" in vetoed_by(commit(45), state)  # 21:00, still peak


# --------------------------------------------------------------------------- #
# C1 — the retry budget
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("attempts", [0, 1, 2, 3])
def test_c1_permits_within_budget(attempts: int) -> None:
    s = make_state(attempts_used=attempts)
    assert "C1" not in vetoed_by(commit(24), s)


def test_c1_vetoes_the_fifth_attempt() -> None:
    s = make_state(attempts_used=MAX_ATTEMPTS)
    assert "C1" in vetoed_by(commit(24), s)


# --------------------------------------------------------------------------- #
# C8 — one pending PDN per mandate (the serialization constraint)
# --------------------------------------------------------------------------- #


def test_c8_vetoes_a_second_commitment(pending_pdn) -> None:
    s = make_state(pending_pdn=pending_pdn)
    assert "C8" in vetoed_by(commit(24), s)


def test_c8_requires_an_explicit_cancel_first(pending_pdn) -> None:
    """Re-planning must surface as a decision in the audit log, not as a silent
    overwrite, so the cost of abandoning a commitment stays visible."""
    s = make_state(pending_pdn=pending_pdn)
    assert is_permitted(CancelPending(), s, ORIGIN).allowed
    cleared = s.with_(pending_pdn=None)
    assert is_permitted(commit(24), cleared, ORIGIN).allowed


def test_cancelling_nothing_is_an_operational_veto(state) -> None:
    assert "OPS-NOPEND" in vetoed_by(CancelPending(), state)


# --------------------------------------------------------------------------- #
# C15 / C16 — AFA-free ceilings
# --------------------------------------------------------------------------- #


def test_c15_ceiling_bites_at_one_paisa_over() -> None:
    s = make_state(amount_due_paise=rupees(20_000), max_amount_paise=rupees(25_000))
    assert "C15" not in vetoed_by(commit(24, rupees(15_000)), s)
    assert "C15" in vetoed_by(commit(24, rupees(15_000) + 1), s)


@pytest.mark.parametrize("category", [Category.INSURANCE, Category.MF_SIP, Category.CC_BILL])
def test_c16_raised_ceiling_for_eligible_categories(category: Category) -> None:
    s = make_state(
        category=category,
        amount_due_paise=rupees(1_00_000),
        max_amount_paise=rupees(1_00_000),
    )
    assert "C15" not in vetoed_by(commit(24, rupees(1_00_000)), s)
    assert "C15" in vetoed_by(commit(24, rupees(1_00_000) + 1), s)


def test_c19_never_debits_above_the_authorised_cap() -> None:
    s = make_state(amount_due_paise=rupees(2_000), max_amount_paise=rupees(1_000))
    assert "C19" in vetoed_by(commit(24, rupees(1_500)), s)


# --------------------------------------------------------------------------- #
# C12 / C21 / RATCHET — mandate lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status", [MandateStatus.REVOKED, MandateStatus.EXPIRED, MandateStatus.PAUSED, MandateStatus.COMPLETED]
)
def test_c12_only_a_live_mandate_may_be_debited(status: MandateStatus) -> None:
    assert "C12" in vetoed_by(commit(24), make_state(status=status))


def test_c21_execution_must_be_inside_the_validity_period() -> None:
    s = make_state(validity_end=ORIGIN + timedelta(hours=25))
    assert "C21" not in vetoed_by(commit(24), s)
    assert "C21" in vetoed_by(commit(30), s)


@pytest.mark.parametrize(
    "cause",
    [
        CauseClass.MANDATE_REVOKED,
        CauseClass.MANDATE_EXPIRED,
        CauseClass.AFA_REQUIRED,
        CauseClass.VPA_INVALID,
        CauseClass.TERMINAL,
    ],
)
def test_ratchet_refuses_a_retry_against_a_terminal_cause(cause: CauseClass) -> None:
    """Retrying a revoked mandate is not merely wasteful, it is abusive."""
    assert "RATCHET" in vetoed_by(commit(24), make_state(cause=cause))


# --------------------------------------------------------------------------- #
# C7 — the 23:50 cut-off
# --------------------------------------------------------------------------- #


def test_c7_late_pdn_cannot_target_a_next_day_execution() -> None:
    """Checked against a non-aligned execution time on purpose — see the test below
    for why C7 cannot be provoked on the aligned grid."""
    clock = datetime(2026, 9, 1, 23, 55, tzinfo=IST)
    late = Commit(
        execute_at=datetime(2026, 9, 2, 23, 56, tzinfo=IST), amount_paise=rupees(499)
    )
    assert "C7" in vetoed_by(late, make_state(), clock)


def test_c7_is_unreachable_on_the_thirty_minute_grid() -> None:
    """A documented consequence, not an oversight.

    C5 forces a lead of at least 24h, so a PDN raised at or after 23:50 can only
    target an execution at or after 23:50 the next day — and the slot grid has no
    aligned instant in [23:50, 24:00). C7 is therefore dominated by C5 at this
    granularity. The rule is kept because it binds again the moment the grid gets
    finer, and defence in depth on a money path is cheap.
    """
    clock = datetime(2026, 9, 1, 23, 50, tzinfo=IST)
    s = make_state()
    for minutes in range(24 * 60, 48 * 60 + 1, 30):
        action = Commit(
            execute_at=clock + timedelta(minutes=minutes), amount_paise=rupees(499)
        )
        fired = vetoed_by(action, s, clock)
        if "C7" in fired:
            assert "OPS-ALIGN" in fired, "C7 fired on an aligned slot — grid assumption broken"


# --------------------------------------------------------------------------- #
# Operational guards
# --------------------------------------------------------------------------- #


def test_contact_cap_blocks_notification_spam() -> None:
    """The guard against the agent discovering that spamming raises recovery."""
    s = make_state(contacts_used=DEFAULT_CONTACT_CAP)
    action = NotifyOnly(at=ORIGIN + timedelta(hours=1), template_id="t1")
    assert "OPS-CONTACT" in vetoed_by(action, s)


def test_partial_collection_requires_a_variable_amount_mandate() -> None:
    fixed = make_state(variable_amount_allowed=False)
    variable = make_state(variable_amount_allowed=True)
    assert "OPS-PARTIAL" in vetoed_by(commit(24, rupees(299)), fixed)
    assert "OPS-PARTIAL" not in vetoed_by(commit(24, rupees(299)), variable)


@pytest.mark.parametrize("amount", [-1, 0, rupees(500)])
def test_amount_sanity(state, amount: int) -> None:
    assert "OPS-AMT" in vetoed_by(commit(24, amount), state)


def test_unaligned_execution_is_rejected(state) -> None:
    action = Commit(execute_at=ORIGIN + timedelta(hours=24, minutes=7), amount_paise=rupees(499))
    assert "OPS-ALIGN" in vetoed_by(action, state)


def test_stop_and_wait_are_always_available(state) -> None:
    """The agent must never be cornered into spending. Refusing is always legal."""
    assert is_permitted(Stop(reason="terminal cause"), state, ORIGIN).allowed
    assert is_permitted(Wait(), state, ORIGIN).allowed


# --------------------------------------------------------------------------- #
# Structural properties of the layer itself
# --------------------------------------------------------------------------- #


def test_regulatory_vetoes_are_reported_ahead_of_operational_ones() -> None:
    """When an action is illegal for several reasons, the reported one must be the
    reason a regulator would care about, not the one ops would."""
    s = make_state(status=MandateStatus.REVOKED)
    action = Commit(execute_at=ORIGIN + timedelta(hours=1, minutes=7), amount_paise=-5)
    verdict = is_permitted(action, s, ORIGIN)
    assert not verdict.allowed
    assert verdict.kind is RuleKind.REGULATORY


def test_every_emitted_rule_id_is_registered(state) -> None:
    """A veto with no registry entry cannot be rendered into an audit log or cited
    in the pitch, so it must not be possible to emit one."""
    probes = [
        commit(1), commit(24), commit(34), commit(72), commit(24, -1),
        commit(24, rupees(50_000)), CancelPending(),
        NotifyOnly(at=ORIGIN - timedelta(hours=1), template_id="t"),
    ]
    states = [
        make_state(), make_state(attempts_used=4), make_state(status=MandateStatus.REVOKED),
        make_state(cause=CauseClass.TERMINAL), make_state(contacts_used=9),
        make_state(pending_pdn=PDN(ORIGIN, ORIGIN + timedelta(hours=30), rupees(499))),
    ]
    for s in states:
        for p in probes:
            for v in all_vetoes(p, s, ORIGIN):
                assert v.rule_id in RULES, f"unregistered rule id {v.rule_id}"
                assert v.kind is RULES[v.rule_id][0]
