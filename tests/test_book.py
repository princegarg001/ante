"""The projection from ingested events to a decidable book.

A fold like this is where a payments system quietly breaks. The dangerous bugs
are not crashes; they are a book that looks plausible and is wrong about how
many attempts a mandate has left, or about whether the customer cancelled. Each
test below pins one of those.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path

from mandate_recovery.act.journal import Journal
from mandate_recovery.core.clock import IST
from mandate_recovery.core.types import (
    Category,
    CauseClass,
    MandateStatus,
)
from mandate_recovery.ingest.book import (
    MandateRecord,
    events_from_journal,
    project,
)
from mandate_recovery.ingest.events import FailureEvent
from mandate_recovery.ingest.receiver import Receiver, ReceiverConfig

ORIGIN = datetime(2026, 3, 1, 9, 0, tzinfo=IST)


def record(mandate_id: str = "sub_A", **kw) -> MandateRecord:
    base = dict(
        mandate_id=mandate_id,
        issuer_id="HDFC",
        category=Category.STANDARD,
        max_amount_paise=500_00,
        cycle_end=ORIGIN + timedelta(days=10),
        validity_end=ORIGIN + timedelta(days=365),
        amount_due_paise=499_00,
    )
    base.update(kw)
    return MandateRecord(**base)


def event(
    mandate_id: str = "sub_A",
    event_type: str = "payment.failed",
    cause: CauseClass = CauseClass.INSUFFICIENT_FUNDS,
    minutes: int = 0,
    error_code: str = "insufficient_funds",
    amount: int = 499_00,
    event_id: str | None = None,
) -> FailureEvent:
    return FailureEvent(
        event_id=event_id or f"evt_{mandate_id}_{minutes}",
        event_type=event_type,
        received_at=ORIGIN + timedelta(minutes=minutes),
        mandate_id=mandate_id,
        payment_id="pay_1",
        amount_paise=amount,
        currency="INR",
        error_code=error_code,
        error_description=None,
        method="upi",
        vpa="c@okhdfcbank",
        cause=cause,
        confidence=0.9,
        raw_digest="0" * 64,
    )


# --------------------------------------------------------------------------- #
# Attempt counting -- the number the regulatory cap is enforced against
# --------------------------------------------------------------------------- #


def test_failures_accumulate_into_attempts_used() -> None:
    book = project({"sub_A": record()}, [event(minutes=0), event(minutes=60)])
    assert book["sub_A"].attempts_used == 2
    assert book["sub_A"].is_first_presentation is False


def test_a_mid_cycle_deployment_does_not_undercount_attempts() -> None:
    """The bug this exists to prevent is silent and it is a compliance breach.

    Turn the webhook on halfway through a cycle and the stream has witnessed one
    failure while the mandate has actually used three. Counting from zero puts
    `attempts_used` at 1, the cap check reads three attempts remaining, and the
    system presents a fourth and a fifth debit against a regulation that permits
    one execution plus three retries. It does not error. It just breaks the law.
    """
    book = project({"sub_A": record(attempts_before=3)}, [event(minutes=0)])
    assert book["sub_A"].attempts_used == 4


def test_a_baseline_alone_means_it_is_not_a_first_presentation() -> None:
    """First presentation is a fact about the cycle, not about process uptime."""
    book = project({"sub_A": record(attempts_before=2)}, [])
    assert book["sub_A"].is_first_presentation is False
    assert book["sub_A"].attempts_used == 2


def test_a_successful_charge_resets_the_allowance() -> None:
    """A new cycle is a new allowance. Carrying the old count forward would
    refuse attempts the regulation permits, which costs real money."""
    book = project(
        {"sub_A": record(attempts_before=2)},
        [
            event(minutes=0),
            event(event_type="subscription.charged", minutes=30),
        ],
    )
    assert book["sub_A"].attempts_used == 0
    assert book["sub_A"].is_first_presentation is True
    assert book["sub_A"].cause is CauseClass.UNKNOWN
    assert book["sub_A"].last_error_code is None


# --------------------------------------------------------------------------- #
# Out-of-order delivery -- the transport makes no ordering promise
# --------------------------------------------------------------------------- #


def test_a_late_success_does_not_resurrect_a_cancelled_mandate() -> None:
    """The event that arrives last is not the event that happened last.

    A `subscription.charged` re-delivered after a cancellation is older than the
    cancellation. A naive fold takes the last one it sees, marks the mandate
    live, and the next batch debits someone who cancelled.
    """
    events = [
        event(event_type="subscription.charged", minutes=0),
        event(event_type="subscription.cancelled", minutes=10),
    ]
    book = project({"sub_A": record()}, list(reversed(events)))
    assert book["sub_A"].status is MandateStatus.REVOKED


def test_the_book_does_not_depend_on_delivery_order() -> None:
    """Sorting by arrival makes the projection a function of the event *set*.

    That is what makes it replayable: rebuild the book from the journal in any
    order and get the same answer, which is the difference between an auditable
    system and one that merely logs.
    """
    events = [
        event(minutes=0),
        event(minutes=10, cause=CauseClass.TRANSIENT_ISSUER, error_code="issuer_down"),
        event(event_type="subscription.halted", minutes=20),
        event(minutes=30),
    ]
    expected = project({"sub_A": record()}, events)["sub_A"]

    rng = random.Random(7)
    for _ in range(20):
        shuffled = events[:]
        rng.shuffle(shuffled)
        assert project({"sub_A": record()}, shuffled)["sub_A"] == expected


# --------------------------------------------------------------------------- #
# Status and cause ratchets
# --------------------------------------------------------------------------- #


def test_a_revocation_is_absorbing() -> None:
    book = project(
        {"sub_A": record()},
        [
            event(event_type="subscription.cancelled", minutes=0),
            event(event_type="subscription.charged", minutes=10),
            event(minutes=20, cause=CauseClass.TRANSIENT_ISSUER),
        ],
    )
    assert book["sub_A"].status is MandateStatus.REVOKED


def test_a_pause_is_not_absorbing() -> None:
    """Deliberately asymmetric with revocation. A paused subscription genuinely
    resumes; treating a pause as permanent writes off live revenue."""
    paused = project({"sub_A": record()}, [event(event_type="subscription.paused")])
    assert paused["sub_A"].status is MandateStatus.PAUSED

    # And it is genuinely not absorbing: a later cancellation still lands.
    then_gone = project(
        {"sub_A": record()},
        [
            event(event_type="subscription.paused", minutes=0),
            event(event_type="subscription.cancelled", minutes=10),
        ],
    )
    assert then_gone["sub_A"].status is MandateStatus.REVOKED


def test_halted_is_read_as_paused_not_revoked() -> None:
    """Razorpay's `halted` means retries exhausted, not mandate gone. Reading it
    as a revocation strands recoverable money for no reason."""
    book = project({"sub_A": record()}, [event(event_type="subscription.halted")])
    assert book["sub_A"].status is MandateStatus.PAUSED


def test_a_terminal_cause_survives_a_later_ambiguous_one() -> None:
    book = project(
        {"sub_A": record()},
        [
            event(minutes=0, cause=CauseClass.MANDATE_REVOKED, error_code="mandate_revoked"),
            event(minutes=10, cause=CauseClass.UNKNOWN, error_code="mystery"),
        ],
    )
    assert book["sub_A"].cause is CauseClass.MANDATE_REVOKED


def test_a_revoking_cause_closes_the_mandate_without_a_lifecycle_event() -> None:
    """A failure saying the mandate is revoked is a statement about the mandate,
    not about the attempt. Waiting for `subscription.cancelled` to agree would
    leave the system presenting against a mandate it has been told is gone."""
    book = project(
        {"sub_A": record()},
        [event(cause=CauseClass.MANDATE_REVOKED, error_code="mandate_revoked")],
    )
    assert book["sub_A"].status is MandateStatus.REVOKED


# --------------------------------------------------------------------------- #
# The registry is the authority
# --------------------------------------------------------------------------- #


def test_an_event_for_an_unknown_mandate_is_dropped_not_invented() -> None:
    """Synthesising a mandate from a webhook would mean the system debits an
    account on an identifier it cannot otherwise account for."""
    book = project({"sub_A": record()}, [event(mandate_id="sub_STRANGER")])
    assert set(book) == {"sub_A"}
    assert book["sub_A"].attempts_used == 0


def test_the_ceiling_comes_from_the_registry_not_the_payload() -> None:
    """A regulatory limit inferred from a failure message is not a limit."""
    book = project({"sub_A": record()}, [event(amount=9_999_00)])
    assert book["sub_A"].max_amount_paise == 500_00
    assert book["sub_A"].amount_due_paise == 9_999_00


def test_expiry_is_read_from_the_calendar_not_from_an_event() -> None:
    """Nobody sends a webhook when a mandate expires; the date simply passes."""
    reg = {"sub_A": record(validity_end=ORIGIN + timedelta(days=2))}
    live = project(reg, [], as_of=ORIGIN + timedelta(days=1))
    expired = project(reg, [], as_of=ORIGIN + timedelta(days=3))
    assert live["sub_A"].status is MandateStatus.LIVE
    assert expired["sub_A"].status is MandateStatus.EXPIRED


def test_no_pending_pdn_is_ever_reconstructed_from_an_inbound_event() -> None:
    """The executor's log is the authority for what has been notified. Inferring
    a pending notification from a webhook would let the system believe it had
    given 24 hours notice that it never gave."""
    book = project({"sub_A": record()}, [event(), event(minutes=5)])
    assert book["sub_A"].pending_pdn is None


# --------------------------------------------------------------------------- #
# Round trip through the hash chain
# --------------------------------------------------------------------------- #


def test_the_book_rebuilds_from_the_verified_journal(tmp_path: Path) -> None:
    """Provenance runs from the bytes on the wire to the debit, in one chain."""
    from tests.test_ingest import SECRET, payment_failed, sign

    receiver = Receiver(
        ReceiverConfig(secret=SECRET, journal_path=tmp_path / "hooks.jsonl")
    )
    # Distinct amounts, so each delivery is a genuinely different event rather
    # than a duplicate the receiver is right to collapse.
    for i in range(2):
        body = payment_failed(error_code="insufficient_funds", amount=49900 + i)
        receiver.handle(body, sign(body))

    events = events_from_journal(receiver.journal)
    assert len(events) == 2

    book = project({"sub_QxT1abc": record(mandate_id="sub_QxT1abc")}, events)
    state = book["sub_QxT1abc"]
    assert state.attempts_used == 2
    assert state.cause is CauseClass.INSUFFICIENT_FUNDS
    assert state.last_error_code == "insufficient_funds"


def test_rederiving_a_cause_from_history_can_only_tighten_it(tmp_path: Path) -> None:
    """A rule-table edit is applied to history in one direction only.

    Newly recognising a code as terminal is applied backwards -- the system
    should stop presenting against it. Newly forgiving a code is not: the
    attempt that was refused cannot be un-refused, and the customer who was
    spared should stay spared.
    """
    from mandate_recovery.act.journal import RecordKind

    journal = Journal(tmp_path / "j.jsonl")
    journal.open()
    journal.append(
        RecordKind.INGEST,
        run_id="ingest",
        ts=ORIGIN.isoformat(),
        body={
            "event_id": "e1",
            "event_type": "payment.failed",
            "mandate_id": "sub_A",
            "amount_paise": 49900,
            # Stored as terminal; the code re-derives to something softer.
            "error_code": "insufficient_funds",
            "cause": CauseClass.MANDATE_REVOKED.value,
            "confidence": 0.9,
            "raw_digest": "0" * 64,
            "handled": True,
        },
    )
    (rehydrated,) = events_from_journal(journal)
    assert rehydrated.cause is CauseClass.MANDATE_REVOKED


# --------------------------------------------------------------------------- #
# The edge stays light
# --------------------------------------------------------------------------- #


def test_the_ingest_path_pulls_in_no_scientific_stack() -> None:
    """Asserted, not asserted-in-a-docstring.

    The receiver is claimed to deploy as a small service while the allocator
    stays a batch job. That claim is only true while nothing on the import path
    reaches numpy, scipy or scikit-learn, and a single convenience import in a
    future edit would quietly make it false. Checked in a subprocess, because by
    the time this test runs the test session has already imported everything.
    """
    import subprocess
    import sys

    probe = (
        "import mandate_recovery.ingest, mandate_recovery.ingest.book, sys; "
        "print(','.join(sorted(n for n in sys.modules "
        "if n.split('.')[0] in {'numpy','scipy','sklearn','pandas','matplotlib'})))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", f"heavy imports on the edge: {result.stdout}"
