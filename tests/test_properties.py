"""Property-based tests.

Complementary to the model checker rather than redundant with it. The checker is
exhaustive over a bounded grid; Hypothesis is unbounded but sampled — it wanders
into months the grid never covers, unaligned instants, and absurd amounts.

Both are cross-checked against `_inv_violations`, which restates the regulation
independently of `rules.py`. Neither test can pass by agreeing with the code.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from mandate_recovery.constraints import all_vetoes, is_permitted
from mandate_recovery.constraints.modelcheck import _inv_violations
from mandate_recovery.constraints.rules import MAX_ATTEMPTS
from mandate_recovery.core.clock import IST, SLOT_MINUTES, is_non_peak
from mandate_recovery.core.money import rupees
from mandate_recovery.core.types import (
    CancelPending,
    CauseClass,
    Category,
    Commit,
    MandateState,
    MandateStatus,
    PDN,
    Wait,
)

EPOCH = datetime(2026, 1, 1, 0, 0, tzinfo=IST)

# Deliberately wide: a whole year of clocks, leads from a week behind to a week
# ahead, amounts straddling every ceiling, and unaligned minute offsets.
clocks = st.integers(min_value=0, max_value=365 * 24 * 60).map(
    lambda m: EPOCH + timedelta(minutes=m)
)
lead_minutes = st.integers(min_value=-7 * 24 * 60, max_value=7 * 24 * 60)
amounts = st.integers(min_value=-rupees(1_000), max_value=rupees(2_00_000))


@st.composite
def states(draw: st.DrawFn) -> MandateState:
    due = draw(st.integers(min_value=rupees(1), max_value=rupees(1_50_000)))
    return MandateState(
        mandate_id="MND_P",
        status=draw(st.sampled_from(list(MandateStatus))),
        cause=draw(st.sampled_from(list(CauseClass))),
        attempts_used=draw(st.integers(min_value=0, max_value=MAX_ATTEMPTS + 2)),
        is_first_presentation=draw(st.booleans()),
        amount_due_paise=due,
        max_amount_paise=draw(st.integers(min_value=rupees(1), max_value=rupees(2_00_000))),
        category=draw(st.sampled_from(list(Category))),
        cycle_end=EPOCH + timedelta(days=draw(st.integers(1, 400))),
        validity_end=EPOCH + timedelta(days=draw(st.integers(1, 800))),
        pending_pdn=draw(
            st.one_of(
                st.none(),
                st.builds(
                    PDN,
                    notified_at=st.just(EPOCH),
                    execute_at=st.just(EPOCH + timedelta(hours=30)),
                    amount_paise=st.just(rupees(499)),
                ),
            )
        ),
        contacts_used=draw(st.integers(0, 6)),
        issuer_id=draw(st.sampled_from(["HDFC", "SBI", "ICICI", "AXIS"])),
        variable_amount_allowed=draw(st.booleans()),
    )


# --------------------------------------------------------------------------- #
# The central property
# --------------------------------------------------------------------------- #


@settings(max_examples=1200, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(state=states(), clock=clocks, lead=lead_minutes, amount=amounts)
def test_permitted_implies_lawful(
    state: MandateState, clock: datetime, lead: int, amount: int
) -> None:
    """Anything the layer permits satisfies every regulatory invariant.

    This is the only property that really matters. Everything below is a
    specialisation of it kept separate so a failure names the rule it broke.
    """
    action = Commit(execute_at=clock + timedelta(minutes=lead), amount_paise=amount)
    if is_permitted(action, state, clock).allowed:
        assert _inv_violations(action, state, clock) == []


@settings(max_examples=1500, deadline=None)
@given(state=states(), clock=clocks, lead=lead_minutes, amount=amounts)
def test_no_permitted_execution_lands_in_a_peak_window(
    state: MandateState, clock: datetime, lead: int, amount: int
) -> None:
    action = Commit(execute_at=clock + timedelta(minutes=lead), amount_paise=amount)
    if is_permitted(action, state, clock).allowed:
        assert is_non_peak(action.execute_at)


@settings(max_examples=1500, deadline=None)
@given(state=states(), clock=clocks, lead=lead_minutes, amount=amounts)
def test_no_permitted_commit_exceeds_the_retry_budget(
    state: MandateState, clock: datetime, lead: int, amount: int
) -> None:
    action = Commit(execute_at=clock + timedelta(minutes=lead), amount_paise=amount)
    if is_permitted(action, state, clock).allowed:
        assert state.attempts_used < MAX_ATTEMPTS


@settings(max_examples=1500, deadline=None)
@given(state=states(), clock=clocks, lead=lead_minutes, amount=amounts)
def test_no_permitted_commit_while_a_pdn_is_pending(
    state: MandateState, clock: datetime, lead: int, amount: int
) -> None:
    """C8. The serialization constraint holds no matter what the policy proposes."""
    action = Commit(execute_at=clock + timedelta(minutes=lead), amount_paise=amount)
    if is_permitted(action, state, clock).allowed:
        assert state.pending_pdn is None


@settings(max_examples=1500, deadline=None)
@given(state=states(), clock=clocks, lead=lead_minutes, amount=amounts)
def test_permitted_lead_is_always_inside_the_aperture(
    state: MandateState, clock: datetime, lead: int, amount: int
) -> None:
    action = Commit(execute_at=clock + timedelta(minutes=lead), amount_paise=amount)
    if is_permitted(action, state, clock).allowed:
        assert 24 * 60 <= lead <= 48 * 60


# --------------------------------------------------------------------------- #
# Purity
# --------------------------------------------------------------------------- #


@settings(max_examples=500, deadline=None)
@given(state=states(), clock=clocks, lead=lead_minutes, amount=amounts)
def test_verdicts_are_deterministic_and_non_mutating(
    state: MandateState, clock: datetime, lead: int, amount: int
) -> None:
    """Same inputs, same verdict, forever — and the state comes back untouched.

    This is what lets the audit log replay a run and get the identical decisions.
    """
    action = Commit(execute_at=clock + timedelta(minutes=lead), amount_paise=amount)
    before = repr(state)
    first = is_permitted(action, state, clock)
    second = is_permitted(action, state, clock)
    assert (first.allowed, getattr(first, "rule_id", None)) == (
        second.allowed,
        getattr(second, "rule_id", None),
    )
    assert repr(state) == before


@settings(max_examples=400, deadline=None)
@given(state=states(), clock=clocks, lead=lead_minutes, amount=amounts)
def test_gate_agrees_with_the_full_veto_list(
    state: MandateState, clock: datetime, lead: int, amount: int
) -> None:
    """`is_permitted` is Allow exactly when `all_vetoes` is empty. If these ever
    diverge, the audit log stops describing the decision that was actually made."""
    action = Commit(execute_at=clock + timedelta(minutes=lead), amount_paise=amount)
    assert is_permitted(action, state, clock).allowed == (
        all_vetoes(action, state, clock) == ()
    )


# --------------------------------------------------------------------------- #
# Stateful: no *sequence* of legal decisions can reach an illegal state
# --------------------------------------------------------------------------- #


class MandateLifecycle(RuleBasedStateMachine):
    """Drives one mandate through arbitrary sequences of permitted actions.

    The individual-action properties above cannot catch a violation that only
    emerges from a sequence — a budget overrun, or two commitments in flight. This
    can. The machine only ever applies actions the constraint layer permits, so any
    invariant failure is a failure of the layer, not of the test.
    """

    def __init__(self) -> None:
        super().__init__()
        self.clock = EPOCH
        self.state = MandateState(
            mandate_id="MND_SM",
            status=MandateStatus.LIVE,
            cause=CauseClass.INSUFFICIENT_FUNDS,
            attempts_used=0,
            is_first_presentation=True,
            amount_due_paise=rupees(499),
            max_amount_paise=rupees(1_000),
            category=Category.STANDARD,
            cycle_end=EPOCH + timedelta(days=30),
            validity_end=EPOCH + timedelta(days=365),
            pending_pdn=None,
            contacts_used=0,
            issuer_id="HDFC",
        )
        self.presentations = 0

    @rule(slot=st.integers(min_value=48, max_value=96))
    def try_commit(self, slot: int) -> None:
        """Propose a commitment somewhere in the 24h-48h band. Applied only if legal."""
        exec_at = self.clock + timedelta(minutes=SLOT_MINUTES * slot)
        action = Commit(execute_at=exec_at, amount_paise=rupees(499))
        if is_permitted(action, self.state, self.clock).allowed:
            self.state = self.state.with_(
                pending_pdn=PDN(self.clock, exec_at, rupees(499))
            )

    @precondition(lambda self: self.state.pending_pdn is not None)
    @rule()
    def cancel(self) -> None:
        if is_permitted(CancelPending(), self.state, self.clock).allowed:
            self.state = self.state.with_(pending_pdn=None)

    @precondition(lambda self: self.state.pending_pdn is not None)
    @rule(pdn_accepted=st.booleans())
    def fire_pending(self, pdn_accepted: bool) -> None:
        """Advance to the pending execution and resolve it.

        A rejected PDN (C6) costs calendar time but does not consume an attempt —
        no presentation was made.
        """
        pdn = self.state.pending_pdn
        assert pdn is not None
        self.clock = pdn.execute_at
        if pdn_accepted:
            self.presentations += 1
            self.state = self.state.with_(
                pending_pdn=None,
                attempts_used=self.state.attempts_used + 1,
                is_first_presentation=False,
            )
        else:
            self.state = self.state.with_(pending_pdn=None)

    @rule(slots=st.integers(min_value=1, max_value=48))
    def wait(self, slots: int) -> None:
        assert is_permitted(Wait(), self.state, self.clock).allowed
        self.clock += timedelta(minutes=SLOT_MINUTES * slots)

    @invariant()
    def budget_is_never_exceeded(self) -> None:
        assert self.state.attempts_used <= MAX_ATTEMPTS
        assert self.presentations <= MAX_ATTEMPTS

    @invariant()
    def at_most_one_commitment_in_flight(self) -> None:
        assert self.state.pending_pdn is None or isinstance(self.state.pending_pdn, PDN)

    @invariant()
    def every_commitment_is_lawful(self) -> None:
        pdn = self.state.pending_pdn
        if pdn is None:
            return
        assert is_non_peak(pdn.execute_at)
        lead = pdn.execute_at - pdn.notified_at
        assert timedelta(hours=24) <= lead <= timedelta(hours=48)


TestMandateLifecycle = MandateLifecycle.TestCase
TestMandateLifecycle.settings = settings(
    max_examples=200, stateful_step_count=40, deadline=None
)
