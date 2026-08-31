"""Razorpay webhook payloads, turned into one canonical event.

The rest of the system reasons about a `FailureEvent`. This module is the only
place that knows what a Razorpay payload looks like, which is what keeps the
provider's shape from leaking into the policy.

Two things it deliberately does *not* do.

It does not trust the payload's own idea of what went wrong. The error code is
carried through and handed to `diagnose/`, which applies the rule table and the
one-way ratchet. A provider saying `error_reason: "payment_failed"` is not a
diagnosis.

It does not fail loudly on unknown shapes. Providers add fields, rename them,
and send event types you have not seen. A webhook consumer that raises on an
unrecognised payload turns a schema change into an outage, and the provider will
retry it at you until you fix the code. Unknown events are acknowledged and
recorded as unhandled — visible, not fatal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Mapping

from ..core.clock import IST
from ..core.money import Paise
from ..core.types import CauseClass
from ..diagnose.rules import diagnose

#: Events this system acts on. Everything else is acknowledged and ignored —
#: a webhook endpoint that 4xx's on an event it does not care about will be
#: retried forever by a provider that is behaving correctly.
RAZORPAY_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "payment.failed",
        "subscription.charged",
        "subscription.pending",
        "subscription.halted",
        "subscription.cancelled",
        "subscription.paused",
        "subscription.completed",
    }
)

#: Events that represent a failed collection — the ones that create work.
FAILURE_EVENTS: Final[frozenset[str]] = frozenset(
    {"payment.failed", "subscription.pending", "subscription.halted"}
)


@dataclass(frozen=True, slots=True)
class FailureEvent:
    """One canonical failed collection, whatever produced it.

    The simulator emits these and so does the webhook receiver, which is what
    lets the same policy code run against either without knowing the difference.
    """

    event_id: str
    event_type: str
    received_at: datetime
    mandate_id: str
    payment_id: str | None
    amount_paise: Paise
    currency: str
    error_code: str | None
    error_description: str | None
    method: str | None
    vpa: str | None
    #: Inferred, never taken from the payload.
    cause: CauseClass
    confidence: float
    #: SHA-256 of the raw body. Ties a journal entry to the exact bytes that
    #: produced it without storing a payload that may carry customer detail.
    raw_digest: str
    handled: bool = True
    note: str = ""

    def to_body(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "received_at": self.received_at.isoformat(),
            "mandate_id": self.mandate_id,
            "payment_id": self.payment_id,
            "amount_paise": self.amount_paise,
            "currency": self.currency,
            "error_code": self.error_code,
            "cause": self.cause.value,
            "confidence": round(self.confidence, 3),
            "method": self.method,
            "raw_digest": self.raw_digest,
            "handled": self.handled,
            "note": self.note,
        }


def _entity(payload: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    node = payload.get(name)
    if isinstance(node, Mapping):
        entity = node.get("entity")
        if isinstance(entity, Mapping):
            return entity
    return {}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalise(
    body: Mapping[str, Any],
    *,
    event_id: str,
    raw_digest: str,
    received_at: datetime | None = None,
) -> FailureEvent:
    """Turn a verified Razorpay webhook body into a `FailureEvent`.

    Called only after the signature has been checked. Nothing here validates
    authenticity — by this point that question is already settled.
    """
    received_at = received_at or datetime.now(timezone.utc).astimezone(IST)
    event_type = str(body.get("event", "")) or "unknown"

    payload = body.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    payment = _entity(payload, "payment")
    subscription = _entity(payload, "subscription")

    # The mandate is whichever identifier the event actually carries. A
    # subscription id is the natural mandate key; a bare payment falls back to
    # its token, then its order.
    mandate_id = (
        str(subscription.get("id") or "")
        or str(payment.get("token_id") or "")
        or str(payment.get("order_id") or "")
        or str(payment.get("id") or "")
        or "unknown"
    )

    # Razorpay carries two different things under similar names, and getting
    # them the wrong way round quietly destroys the diagnosis:
    #
    #   error_code    a coarse class -- "BAD_REQUEST_ERROR", "GATEWAY_ERROR"
    #   error_reason  the specific one -- "insufficient_funds", "payment_timeout"
    #
    # The reason is what carries the information, so it is what gets classified.
    # Classifying on `error_code` maps every failure in the book to the same
    # handful of useless buckets, which is what the first version of this file
    # did until a fixture built from a real payload caught it.
    error_reason = payment.get("error_reason") or None
    error_class = payment.get("error_code") or None
    error_description = payment.get("error_description") or None

    # The provider's own words are evidence, not a verdict.
    verdict = diagnose(
        str(error_reason or error_class) if (error_reason or error_class) else None,
        str(error_description or error_reason or "") or None,
    )
    error_code = error_reason or error_class

    acquirer = payment.get("acquirer_data")
    vpa = payment.get("vpa") or (
        acquirer.get("vpa") if isinstance(acquirer, Mapping) else None
    )

    handled = event_type in RAZORPAY_EVENTS
    note = "" if handled else f"unhandled event type {event_type!r}"

    return FailureEvent(
        event_id=event_id,
        event_type=event_type,
        received_at=received_at,
        mandate_id=mandate_id,
        payment_id=str(payment.get("id")) if payment.get("id") else None,
        amount_paise=_as_int(payment.get("amount")),
        currency=str(payment.get("currency") or "INR"),
        error_code=str(error_code) if error_code else None,
        error_description=str(error_description) if error_description else None,
        method=str(payment.get("method")) if payment.get("method") else None,
        vpa=str(vpa) if vpa else None,
        cause=verdict.cause,
        confidence=verdict.confidence,
        raw_digest=raw_digest,
        handled=handled,
        note=note,
    )


def is_failure(event: FailureEvent) -> bool:
    """Whether this event creates recovery work."""
    return event.event_type in FAILURE_EVENTS
