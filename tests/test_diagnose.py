"""The diagnosis layer.

The tests worth having are not about accuracy. They are about the *shape* of the
errors: a layer that is 95% accurate but whose mistakes all point in the
dangerous direction is worse than one that is 90% accurate and fails safe.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from mandate_recovery.core.clock import IST
from mandate_recovery.core.types import TERMINAL_CAUSES, CauseClass
from mandate_recovery.diagnose.rules import (
    AMBIGUOUS_CODES,
    CONFIDENT,
    RULE_TABLE,
    Diagnosis,
    accuracy,
    apply_ratchet,
    confusion_matrix,
    dangerous_errors,
    diagnose,
)
from mandate_recovery.sim.world import World, WorldConfig

ORIGIN = datetime(2026, 9, 1, 0, 0, tzinfo=IST)


# --------------------------------------------------------------------------- #
# The rule floor
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "code,expected",
    [
        ("insufficient_funds", CauseClass.INSUFFICIENT_FUNDS),
        ("Z9", CauseClass.INSUFFICIENT_FUNDS),
        ("bank_technical_error", CauseClass.TRANSIENT_ISSUER),
        ("limit_exceeded", CauseClass.LIMIT_BREACH),
        ("mandate_revoked", CauseClass.MANDATE_REVOKED),
        ("mandate_expired", CauseClass.MANDATE_EXPIRED),
        ("account_closed", CauseClass.TERMINAL),
        ("invalid_vpa", CauseClass.VPA_INVALID),
        ("PRE_DEBIT_NOTIFICATION_NOT_FOUND", CauseClass.PDN_MISSING),
    ],
)
def test_known_codes_map_confidently(code: str, expected: CauseClass) -> None:
    d = diagnose(code)
    assert d.cause is expected
    assert d.is_confident, d


def test_an_ambiguous_code_produces_uncertainty_not_a_guess() -> None:
    """A bank saying "technical decline" has told you almost nothing. Inventing
    a cause from it would be exactly the confident nonsense this layer exists to
    avoid."""
    for code in AMBIGUOUS_CODES:
        d = diagnose(code)
        assert d.cause is CauseClass.UNKNOWN
        assert not d.is_confident
        assert d.rule == "ambiguous"


def test_an_unrecognised_code_is_not_a_licence_to_guess() -> None:
    d = diagnose("some_code_nobody_has_seen")
    assert d.cause is CauseClass.UNKNOWN
    assert d.confidence < CONFIDENT


def test_free_text_is_read_only_at_low_confidence() -> None:
    """Where an adjudicator would earn its place. Reading the text is allowed;
    being sure about it is not."""
    d = diagnose("unknown_code", "Customer account has been closed")
    assert d.cause is CauseClass.TERMINAL
    assert not d.is_confident, "free text should never produce a confident verdict"


def test_no_rule_maps_to_unknown() -> None:
    """An entry in the table that resolves to UNKNOWN is a rule that does nothing."""
    for code, (cause, confidence) in RULE_TABLE.items():
        assert cause is not CauseClass.UNKNOWN, code
        assert 0.0 < confidence <= 1.0, code


# --------------------------------------------------------------------------- #
# The one-way ratchet
# --------------------------------------------------------------------------- #


def test_the_ratchet_lets_a_second_opinion_add_caution() -> None:
    rule = diagnose("technical_decline")          # UNKNOWN, low confidence
    assert apply_ratchet(rule, CauseClass.MANDATE_REVOKED) is CauseClass.MANDATE_REVOKED


def test_the_ratchet_refuses_to_remove_caution() -> None:
    """The asymmetry that makes it safe to put a model on top.

    Being wrong in the recoverable direction wastes an attempt. Being wrong in
    the terminal direction means retrying a mandate the customer cancelled,
    which is not a mistake but an abuse.
    """
    rule = diagnose("mandate_revoked")            # terminal, confident
    for proposed in (
        CauseClass.INSUFFICIENT_FUNDS,
        CauseClass.TRANSIENT_ISSUER,
        CauseClass.UNKNOWN,
    ):
        assert apply_ratchet(rule, proposed) is CauseClass.MANDATE_REVOKED


def test_the_ratchet_cannot_be_walked_out_of_by_repetition() -> None:
    rule = diagnose("account_closed")
    cause = rule.cause
    for _ in range(10):
        cause = apply_ratchet(rule, CauseClass.INSUFFICIENT_FUNDS)
    assert cause in TERMINAL_CAUSES


# --------------------------------------------------------------------------- #
# Against the simulator's ground truth
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def pairs():
    world = World.generate(100, ORIGIN, WorldConfig(n_mandates=1200, days=35))
    out = []
    for m in world.mandates:
        r = world.present(m.mandate_id, world.time_of(m.due_slot), m.amount_due)
        if not r.ok:
            out.append((r.cause, diagnose(r.error_code, r.error_description).cause))
    return out


def test_it_classifies_most_failures_correctly(pairs) -> None:
    assert len(pairs) > 300
    assert accuracy(pairs) > 0.85


def test_ambiguity_is_present_and_is_where_the_errors_are(pairs) -> None:
    """If every code mapped cleanly the layer would be free and the reported
    accuracy would mean nothing."""
    wrong = [(t, i) for t, i in pairs if t is not i]
    assert wrong, "no classification error at all — the world has no ambiguity"
    assert all(i is CauseClass.UNKNOWN for _, i in wrong), (
        "an error resolved to a confident wrong cause rather than to UNKNOWN"
    )


def test_mistakes_fail_towards_uncertainty_not_towards_action(pairs) -> None:
    """The shape that matters.

    Every misclassification lands on UNKNOWN. The layer never turns a terminal
    cause into a confidently recoverable one, so the policy is told "I do not
    know" rather than "go ahead".
    """
    dangerous = dangerous_errors(pairs)
    assert dangerous > 0, "no dangerous cases in the sample to reason about"
    # Every one of them is UNKNOWN rather than a confident recoverable cause.
    confident_wrong = [
        (t, i)
        for t, i in pairs
        if t in TERMINAL_CAUSES and i not in TERMINAL_CAUSES and i is not CauseClass.UNKNOWN
    ]
    assert not confident_wrong, confident_wrong


def test_the_confusion_matrix_covers_the_sample(pairs) -> None:
    matrix = confusion_matrix(pairs)
    assert sum(matrix.values()) == len(pairs)
    assert len(matrix) > 3


# --------------------------------------------------------------------------- #
# The agent no longer reads the answer
# --------------------------------------------------------------------------- #


def test_the_agent_sees_an_inferred_cause_not_the_true_one() -> None:
    """Previously `observable()` copied the simulator's ground-truth cause. That
    was defensible — the modelled codes map cleanly — but it left classification
    error out of every reported number, which is not the same as it being zero.
    """
    world = World.generate(101, ORIGIN, WorldConfig(n_mandates=600, days=35))
    mismatches = 0
    seen = 0
    for m in world.mandates:
        r = world.present(m.mandate_id, world.time_of(m.due_slot), m.amount_due)
        if r.ok:
            continue
        seen += 1
        state = world.observable(m)
        assert state.last_error_code == m.last_error_code
        assert state.cause is diagnose(m.last_error_code).cause
        mismatches += state.cause is not m.last_cause
    assert seen > 100
    assert mismatches > 0, "the agent's cause never differs from ground truth"
