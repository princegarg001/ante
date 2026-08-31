"""The webhook edge.

This is the only component that processes bytes an attacker chooses, so the
tests are mostly about what it *refuses*. Accepting a valid webhook is one test;
the rest are the ways a webhook consumer gets broken into or falls over.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from pathlib import Path

import pytest

from mandate_recovery.act.journal import Journal, RecordKind
from mandate_recovery.core.clock import IST
from mandate_recovery.core.types import CauseClass
from mandate_recovery.ingest.events import RAZORPAY_EVENTS, is_failure, normalise
from mandate_recovery.ingest.receiver import Receipt, Receiver, ReceiverConfig
from mandate_recovery.ingest.signature import (
    MAX_BODY_BYTES,
    SignatureError,
    expected_signature,
    verify_signature,
)
from mandate_recovery.ingest.wsgi import make_application

SECRET = "whsec_test_5f3a9c1e"


def sign(body: bytes, secret: str = SECRET) -> dict[str, str]:
    return {
        "X-Razorpay-Signature": expected_signature(body, secret),
        "X-Razorpay-Event-Id": hashlib.sha256(body).hexdigest()[:24],
    }


def payment_failed(
    subscription_id: str = "sub_QxT1abc",
    error_code: str = "insufficient_funds",
    amount: int = 49900,
) -> bytes:
    """A payload shaped like the real thing."""
    return json.dumps(
        {
            "entity": "event",
            "account_id": "acc_Jk8Lm2",
            "event": "payment.failed",
            "contains": ["payment"],
            "created_at": 1_790_000_000,
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_QxT1payment",
                        "entity": "payment",
                        "amount": amount,
                        "currency": "INR",
                        "status": "failed",
                        "order_id": "order_QxT1",
                        "method": "upi",
                        "vpa": "customer@okhdfcbank",
                        "token_id": "token_QxT1",
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_description": "Payment failed",
                        "error_reason": error_code,
                        "acquirer_data": {"rrn": "123456789012"},
                    }
                },
                "subscription": {
                    "entity": {
                        "id": subscription_id,
                        "entity": "subscription",
                        "status": "pending",
                        "paid_count": 3,
                        "remaining_count": 9,
                    }
                },
            },
        }
    ).encode("utf-8")


#: A fixed clock. The receiver takes time as an argument, so its output is a
#: pure function of its input and these tests do not drift with the wall.
FROZEN = datetime(2026, 3, 1, 9, 0, tzinfo=IST)


@pytest.fixture
def receiver(tmp_path: Path) -> Receiver:
    return Receiver(
        ReceiverConfig(
            secret=SECRET,
            journal_path=tmp_path / "hooks.jsonl",
            clock=lambda: FROZEN,
        )
    )


# --------------------------------------------------------------------------- #
# Signature verification — the part that must not be wrong
# --------------------------------------------------------------------------- #


def test_a_correctly_signed_body_verifies() -> None:
    body = payment_failed()
    assert verify_signature(body, sign(body), SECRET)


def test_a_tampered_body_is_rejected() -> None:
    body = payment_failed()
    headers = sign(body)
    with pytest.raises(SignatureError):
        verify_signature(body + b" ", headers, SECRET)


def test_the_wrong_secret_is_rejected() -> None:
    body = payment_failed()
    with pytest.raises(SignatureError):
        verify_signature(body, sign(body, "whsec_someone_elses"), SECRET)


def test_a_missing_signature_is_a_rejection_not_a_skip() -> None:
    """A surprising number of implementations treat "no signature" as "nothing
    to check"."""
    with pytest.raises(SignatureError, match="missing"):
        verify_signature(payment_failed(), {}, SECRET)


def test_an_empty_secret_refuses_to_verify_anything() -> None:
    """Running without a secret would accept everything, silently. Refusing is
    the safer failure."""
    body = payment_failed()
    with pytest.raises(SignatureError, match="not configured"):
        verify_signature(body, sign(body), "")


def test_an_oversized_body_is_rejected_before_anything_reads_it() -> None:
    """A public POST endpoint without a size limit is a memory-exhaustion
    primitive that needs no credentials."""
    huge = b"{" + b"a" * (MAX_BODY_BYTES + 10)
    with pytest.raises(SignatureError, match="too large"):
        verify_signature(huge, sign(huge), SECRET)


def test_a_malformed_signature_is_rejected_cheaply() -> None:
    body = payment_failed()
    with pytest.raises(SignatureError, match="malformed"):
        verify_signature(body, {"X-Razorpay-Signature": "nope"}, SECRET)


def test_headers_are_matched_case_insensitively() -> None:
    """WSGI, ASGI and assorted proxies all disagree about header casing."""
    body = payment_failed()
    sig = expected_signature(body, SECRET)
    for name in ("X-Razorpay-Signature", "x-razorpay-signature", "X-RAZORPAY-SIGNATURE"):
        assert verify_signature(body, {name: sig}, SECRET)


def test_verification_uses_a_constant_time_comparison() -> None:
    """Asserted structurally: `==` on a digest leaks how much of a forged
    signature was correct, and the leak is invisible in behavioural tests."""
    source = Path("mandate_recovery/ingest/signature.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest" in source
    assert "provided == expected" not in source


# --------------------------------------------------------------------------- #
# The handler
# --------------------------------------------------------------------------- #


def test_a_valid_webhook_is_accepted_and_journalled(receiver: Receiver) -> None:
    body = payment_failed()
    receipt = receiver.handle(body, sign(body))

    assert receipt.status == 200
    assert receipt.outcome == "accepted"
    assert receipt.event is not None
    assert receipt.event.mandate_id == "sub_QxT1abc"
    assert receipt.event.amount_paise == 49900
    assert receipt.event.cause is CauseClass.INSUFFICIENT_FUNDS

    records = [r for r in receiver.journal if r.kind is RecordKind.INGEST]
    assert len(records) == 1
    assert records[0].body["event_id"] == receipt.event_id


def test_an_unauthentic_request_is_never_journalled(receiver: Receiver) -> None:
    body = payment_failed()
    receipt = receiver.handle(body, {"X-Razorpay-Signature": "0" * 64})
    assert receipt.status == 401
    assert receipt.outcome == "rejected"
    assert not list(receiver.journal)


def test_the_body_is_not_parsed_until_the_signature_verifies(receiver: Receiver) -> None:
    """Malformed JSON with a bad signature must fail on the signature. If it
    reports a parse error instead, the parser ran on unauthenticated bytes."""
    receipt = receiver.handle(b"{not json at all", {"X-Razorpay-Signature": "0" * 64})
    assert receipt.status == 401, receipt.detail


def test_a_replayed_delivery_is_acknowledged_but_recorded_once(
    receiver: Receiver,
) -> None:
    """Delivery is at-least-once; the ledger has to be exactly-once. A retried
    failure must not become a second failure in the book."""
    body = payment_failed()
    headers = sign(body)

    first = receiver.handle(body, headers)
    second = receiver.handle(body, headers)
    third = receiver.handle(body, headers)

    assert first.outcome == "accepted"
    assert second.outcome == "duplicate"
    assert third.outcome == "duplicate"
    # A duplicate is still a 200: a provider retrying correctly must not be
    # punished with an error that makes it retry again.
    assert second.status == 200
    assert len([r for r in receiver.journal if r.kind is RecordKind.INGEST]) == 1


def test_deduplication_survives_a_restart(tmp_path: Path) -> None:
    """The index is rebuilt from the journal. Otherwise a provider retry after a
    deploy becomes a duplicate failure in the book."""
    path = tmp_path / "hooks.jsonl"
    body = payment_failed()
    headers = sign(body)

    first = Receiver(ReceiverConfig(secret=SECRET, journal_path=path, clock=lambda: FROZEN))
    assert first.handle(body, headers).outcome == "accepted"

    restarted = Receiver(ReceiverConfig(secret=SECRET, journal_path=path, clock=lambda: FROZEN))
    assert restarted.handle(body, headers).outcome == "duplicate"
    assert restarted.accepted_count == 1


def test_valid_json_that_is_not_an_object_is_rejected(receiver: Receiver) -> None:
    body = b"[1, 2, 3]"
    receipt = receiver.handle(body, sign(body))
    assert receipt.status == 400


def test_an_unknown_event_type_is_acknowledged_not_refused(receiver: Receiver) -> None:
    """A consumer that 4xx's on an event it does not care about will be retried
    by a provider that is behaving correctly, forever."""
    body = json.dumps({"event": "payout.processed", "payload": {}}).encode()
    receipt = receiver.handle(body, sign(body))
    assert receipt.status == 200
    assert receipt.outcome == "unhandled"


def test_the_journal_stays_verifiable_across_many_events(receiver: Receiver) -> None:
    for i in range(25):
        body = payment_failed(subscription_id=f"sub_{i:04d}")
        receiver.handle(body, sign(body))
    assert receiver.verify_chain() == 25


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


def test_the_cause_is_inferred_not_taken_from_the_payload() -> None:
    """The provider's own words are evidence, not a verdict."""
    body = json.loads(payment_failed(error_code="mandate_revoked"))
    event = normalise(body, event_id="evt_1", raw_digest="d" * 64, received_at=FROZEN)
    assert event.cause is CauseClass.MANDATE_REVOKED
    # Razorpay puts a coarse class in `error_code` ("BAD_REQUEST_ERROR") and the
    # specific one in `error_reason`. The specific one is what gets classified
    # and what gets carried; the description is kept as written.
    assert event.error_code == "mandate_revoked"
    assert event.error_description == "Payment failed"


def test_an_uninformative_code_becomes_uncertainty() -> None:
    body = json.loads(payment_failed(error_code="technical_decline"))
    event = normalise(body, event_id="evt_2", raw_digest="d" * 64, received_at=FROZEN)
    assert event.cause is CauseClass.UNKNOWN
    assert event.confidence < 0.8


def test_a_payload_missing_everything_still_normalises() -> None:
    """Providers add fields, rename them and send shapes you have not seen. A
    normaliser that raises turns a schema change into an outage."""
    event = normalise({"event": "payment.failed"}, event_id="evt_3", raw_digest="d" * 64, received_at=FROZEN)
    assert event.mandate_id == "unknown"
    assert event.amount_paise == 0
    assert event.cause is CauseClass.UNKNOWN


def test_failure_events_are_distinguished_from_successes() -> None:
    failed = normalise(
        json.loads(payment_failed()),
        event_id="e",
        raw_digest="d",
        received_at=FROZEN,
    )
    charged = normalise(
        {"event": "subscription.charged", "payload": {}},
        event_id="e2",
        raw_digest="d",
        received_at=FROZEN,
    )
    assert is_failure(failed)
    assert not is_failure(charged)
    assert "subscription.charged" in RAZORPAY_EVENTS


# --------------------------------------------------------------------------- #
# The HTTP surface
# --------------------------------------------------------------------------- #


def call(app, method: str, path: str, body: bytes = b"", headers=None):
    import io

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    for key, value in (headers or {}).items():
        environ["HTTP_" + key.upper().replace("-", "_")] = value
    captured: dict = {}

    def start_response(status, response_headers):
        captured["status"] = int(status.split()[0])
        captured["headers"] = dict(response_headers)

    chunks = app(environ, start_response)
    return captured["status"], json.loads(b"".join(chunks)), captured["headers"]


def test_the_endpoint_accepts_a_signed_webhook(receiver: Receiver) -> None:
    app = make_application(receiver)
    body = payment_failed()
    status, payload, _ = call(app, "POST", "/webhooks/razorpay", body, sign(body))
    assert status == 200
    assert payload == {"status": "accepted"}


def test_the_endpoint_rejects_an_unsigned_webhook(receiver: Receiver) -> None:
    app = make_application(receiver)
    body = payment_failed()
    status, payload, _ = call(app, "POST", "/webhooks/razorpay", body)
    assert status == 401
    assert payload == {"status": "rejected"}


def test_the_response_leaks_nothing_about_why_it_failed(receiver: Receiver) -> None:
    """Telling a caller whether the signature was absent, malformed or merely
    wrong is free reconnaissance."""
    app = make_application(receiver)
    body = payment_failed()
    _, missing, _ = call(app, "POST", "/webhooks/razorpay", body)
    _, wrong, _ = call(
        app, "POST", "/webhooks/razorpay", body, {"X-Razorpay-Signature": "0" * 64}
    )
    assert missing == wrong == {"status": "rejected"}


def test_health_reports_a_verified_chain(receiver: Receiver) -> None:
    app = make_application(receiver)
    body = payment_failed()
    call(app, "POST", "/webhooks/razorpay", body, sign(body))
    status, payload, _ = call(app, "GET", "/health")
    assert status == 200
    assert payload["accepted"] == 1
    assert payload["journal_records"] == 1


def test_unknown_routes_and_methods_are_refused(receiver: Receiver) -> None:
    app = make_application(receiver)
    assert call(app, "GET", "/")[0] == 404
    assert call(app, "GET", "/webhooks/razorpay")[0] == 405


def test_responses_carry_hardening_headers(receiver: Receiver) -> None:
    app = make_application(receiver)
    _, _, headers = call(app, "GET", "/health")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"


def test_the_reader_never_trusts_content_length(receiver: Receiver) -> None:
    """A declared length larger than the body must not be believed, and one
    larger than the cap must not be honoured."""
    import io

    app = make_application(receiver)
    body = payment_failed()
    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/webhooks/razorpay",
        "CONTENT_LENGTH": str(10**9),
        "wsgi.input": io.BytesIO(body),
    }
    for key, value in sign(body).items():
        environ["HTTP_" + key.upper().replace("-", "_")] = value
    captured: dict = {}

    def start_response(status, response_headers):
        captured["status"] = int(status.split()[0])

    list(app(environ, start_response))
    assert captured["status"] == 200


# --------------------------------------------------------------------------- #
# The handler is a wall, not a happy path
# --------------------------------------------------------------------------- #


def test_a_signature_of_the_right_length_but_the_wrong_alphabet_is_rejected() -> None:
    """Length alone is not a shape check, and the gap is remotely reachable.

    `hmac.compare_digest` raises `TypeError` on a `str` containing non-ASCII. A
    header of exactly 64 characters with one accented byte therefore sailed past
    the length check and crashed the comparison -- an unauthenticated caller
    turning an authentication failure into an unhandled exception. Checking the
    alphabet keeps that input on the rejection path.
    """
    body = payment_failed()
    for forged in ("e" * 63 + "é", "z" * 64, "e" * 63 + " "):
        with pytest.raises(SignatureError, match="malformed"):
            verify_signature(body, {"X-Razorpay-Signature": forged}, SECRET)


def test_the_handler_returns_a_receipt_for_every_hostile_input(
    receiver: Receiver,
) -> None:
    """`handle` promises it never raises. The promise is what makes it safe to
    expose, so it is tested rather than trusted."""
    hostile: list[tuple[bytes, dict[str, str]]] = [
        (b"", {}),
        (b"", {"X-Razorpay-Signature": "e" * 63 + "é"}),
        (b"\xff\xfe\x00binary", {"X-Razorpay-Signature": "0" * 64}),
        (b"null", sign(b"null")),
        (b"[]", sign(b"[]")),
        (b'"a string"', sign(b'"a string"')),
        (b"{}", sign(b"{}")),
        (b'{"event": null, "payload": 7}', sign(b'{"event": null, "payload": 7}')),
        (b'{"payload": {"payment": "not an object"}}',
         sign(b'{"payload": {"payment": "not an object"}}')),
        (b"{" * 5000, {"X-Razorpay-Signature": "0" * 64}),
    ]
    for raw, headers in hostile:
        receipt = receiver.handle(raw, headers)
        assert isinstance(receipt, Receipt)
        assert receipt.status in (200, 400, 401), (raw[:40], receipt.status)


def test_an_unexpected_internal_error_becomes_a_retryable_refusal(
    receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bug below the verification line must not be a stack trace on the wire.

    It must also not be acknowledged: a 200 would tell the provider the event is
    handled and it would never be re-delivered, so the failure would be silent
    and permanent. The event stays unseen, so the retry can succeed once the bug
    is fixed.
    """
    import mandate_recovery.ingest.receiver as receiver_module

    def exploding(*args, **kwargs):
        raise ValueError("a bug nobody predicted")

    monkeypatch.setattr(receiver_module, "normalise", exploding)

    body = payment_failed()
    receipt = receiver.handle(body, sign(body))

    assert receipt.status == 500
    assert receipt.outcome == "error"
    assert "bug nobody predicted" not in receipt.detail   # no reconnaissance
    assert not list(receiver.journal)
    assert receiver.accepted_count == 0

    # And the retry, once the bug is gone, is accepted rather than swallowed as
    # a duplicate.
    monkeypatch.undo()
    assert receiver.handle(body, sign(body)).outcome == "accepted"
