"""Events in, book out: the projection from what arrived to what is decided on.

The receiver records failures. The allocator reasons about `MandateState`. This
module is the fold between them, and it exists because those two things are not
the same kind of object and pretending otherwise is how the seam rots.

## What a webhook can and cannot tell you

A `payment.failed` says *what happened*. It does not say what the mandate **is**
-- its validity end, its registered ceiling, its category, which issuer holds it.
None of that is in the payload, because none of it is news; it was fixed at
registration. So the projection takes two inputs:

    registry   the merchant's own record of each mandate -- static facts
    events     the ingested stream -- what has happened since

Deriving a mandate's ceiling from a webhook would mean inferring a regulatory
limit from a failure message. The registry is the authority for what the mandate
permits, and this module never overrides it.

## Two things the fold gets right that a naive one does not

**Attempts are counted from a stated baseline, not from zero.** Counting
failures in the ingest stream and calling that `attempts_used` is only correct
if the webhook has seen the whole cycle. Deploy this mid-cycle and it undercounts
-- and `attempts_used` is the number the regulatory cap (C4: one execution plus
three retries) is enforced against, so undercounting it is not a reporting error,
it is a compliance breach that looks like a working system. `MandateRecord`
therefore carries `attempts_before`, the count the merchant already knows about,
and the projection adds to it rather than replacing it.

**Status ratchets one way.** Webhooks arrive out of order -- a retry of an
earlier delivery can land after a later event, and Razorpay makes no ordering
promise. Folding naively means a stale `subscription.charged` can resurrect a
mandate the customer cancelled, and the next thing the system does is debit a
revoked mandate. Terminal statuses are therefore absorbing, in the same way and
for the same reason that `diagnose.apply_ratchet` makes terminal *causes*
absorbing: being wrongly cautious costs an attempt, being wrongly confident
costs a customer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Iterable, Mapping, Sequence

from ..act.journal import Journal, RecordKind
from ..core.money import Paise
from ..core.types import (
    TERMINAL_CAUSES,
    Category,
    CauseClass,
    MandateState,
    MandateStatus,
)
from ..diagnose.rules import diagnose
from .events import FAILURE_EVENTS, FailureEvent

#: Statuses from which there is no return. Reached, they absorb: no later event,
#: however it is ordered, moves a mandate back out.
TERMINAL_STATUSES: Final[frozenset[MandateStatus]] = frozenset(
    {MandateStatus.REVOKED, MandateStatus.EXPIRED, MandateStatus.COMPLETED}
)

#: Razorpay subscription lifecycle events, mapped to the status they imply.
#: `halted` is Razorpay's "retries exhausted, subscription stopped" -- the
#: mandate itself survives, so it is PAUSED rather than REVOKED. Reading it as a
#: revocation would strand recoverable money; reading a revocation as a pause
#: would debit someone who cancelled. The asymmetry is the whole reason this is
#: a table and not an inference.
STATUS_EVENTS: Final[Mapping[str, MandateStatus]] = {
    "subscription.cancelled": MandateStatus.REVOKED,
    "subscription.completed": MandateStatus.COMPLETED,
    "subscription.halted": MandateStatus.PAUSED,
    "subscription.paused": MandateStatus.PAUSED,
}

#: Causes that are statements about the mandate's existence, not about one
#: attempt. A revoked mandate is revoked whatever the subscription record says.
CAUSE_IMPLIES_STATUS: Final[Mapping[CauseClass, MandateStatus]] = {
    CauseClass.MANDATE_REVOKED: MandateStatus.REVOKED,
    CauseClass.MANDATE_EXPIRED: MandateStatus.EXPIRED,
}


@dataclass(frozen=True, slots=True)
class MandateRecord:
    """The merchant's registry row: what the mandate is, fixed at registration.

    Everything here comes from the merchant's own system, never from a webhook.
    """

    mandate_id: str
    issuer_id: str
    category: Category
    max_amount_paise: Paise
    cycle_end: datetime
    validity_end: datetime
    #: Attempts already consumed in the current cycle before ingestion began.
    #: Non-zero whenever this is deployed mid-cycle, and the cap is enforced
    #: against the sum, not against what the stream happened to witness.
    attempts_before: int = 0
    contacts_used: int = 0
    variable_amount_allowed: bool = False
    #: Amount the mandate is presently collecting. Overwritten by a failure
    #: event, which carries the amount actually attempted.
    amount_due_paise: Paise = 0
    status: MandateStatus = MandateStatus.LIVE


def _rank(status: MandateStatus) -> int:
    """Order statuses by how closed they are, so the fold can take a maximum."""
    return {
        MandateStatus.LIVE: 0,
        MandateStatus.PAUSED: 1,
        MandateStatus.COMPLETED: 2,
        MandateStatus.EXPIRED: 3,
        MandateStatus.REVOKED: 4,
    }[status]


def _absorb(current: MandateStatus, proposed: MandateStatus) -> MandateStatus:
    """Move toward the more closed of the two, and never back.

    PAUSED is *not* absorbing -- a paused subscription genuinely resumes, and
    treating a pause as permanent would write off live revenue. Only the three
    in `TERMINAL_STATUSES` absorb.
    """
    if current in TERMINAL_STATUSES:
        return current
    return proposed if _rank(proposed) > _rank(current) else current


def _ratchet_cause(current: CauseClass, proposed: CauseClass) -> CauseClass:
    """Same asymmetry as `diagnose.apply_ratchet`, applied across events.

    Within one event the ratchet arbitrates between a rule and a second opinion.
    Here it arbitrates between an old event and a new one, and the answer is the
    same: a terminal cause once seen is not un-seen by a later ambiguous code.
    """
    if current in TERMINAL_CAUSES:
        return current
    return proposed


def _prefer_terminal(stored: CauseClass, rederived: CauseClass) -> CauseClass:
    """Reconcile a journalled cause with one re-derived from the stored code.

    Strictly one-way: re-derivation may make the book more cautious, never less.
    A rule-table edit that newly recognises a code as terminal is applied to
    history; one that newly forgives a code is not, because the attempt that was
    refused on the old reading cannot be un-refused and the customer who was
    spared should stay spared.
    """
    return rederived if rederived in TERMINAL_CAUSES else stored


def project(
    registry: Mapping[str, MandateRecord],
    events: Iterable[FailureEvent],
    *,
    as_of: datetime | None = None,
) -> dict[str, MandateState]:
    """Fold the ingested stream over the registry into a decidable book.

    Events for mandates not in the registry are dropped rather than invented.
    A webhook naming an unknown subscription is either a misrouted delivery or a
    registry that is behind, and synthesising a mandate from it would mean the
    system debits an account on an identifier it cannot otherwise account for.
    """
    # Ordered by arrival, because the fold's ratchets are order-sensitive and
    # the transport is not ordered. Sorting here is what makes the projection a
    # function of the *set* of events rather than of their delivery sequence:
    # replay it in any order and the book is the same.
    ordered: Sequence[FailureEvent] = sorted(
        events, key=lambda e: (e.received_at, e.event_id)
    )

    attempts: dict[str, int] = {k: r.attempts_before for k, r in registry.items()}
    status: dict[str, MandateStatus] = {k: r.status for k, r in registry.items()}
    cause: dict[str, CauseClass] = {k: CauseClass.UNKNOWN for k in registry}
    amount: dict[str, Paise] = {k: r.amount_due_paise for k, r in registry.items()}
    last_code: dict[str, str | None] = {k: None for k in registry}

    for event in ordered:
        mid = event.mandate_id
        if mid not in registry:
            continue

        if event.event_type == "subscription.charged":
            # The cycle closed. Attempts reset, and the baseline resets with
            # them -- a new cycle is a new allowance, and carrying the old count
            # forward would refuse attempts the regulation permits.
            attempts[mid] = 0
            cause[mid] = CauseClass.UNKNOWN
            last_code[mid] = None
            continue

        if event.event_type in STATUS_EVENTS:
            status[mid] = _absorb(status[mid], STATUS_EVENTS[event.event_type])

        if event.event_type in FAILURE_EVENTS:
            attempts[mid] += 1
            cause[mid] = _ratchet_cause(cause[mid], event.cause)
            last_code[mid] = event.error_code
            if event.amount_paise > 0:
                amount[mid] = event.amount_paise
            implied = CAUSE_IMPLIES_STATUS.get(cause[mid])
            if implied is not None:
                status[mid] = _absorb(status[mid], implied)

    book: dict[str, MandateState] = {}
    for mid, record in registry.items():
        resolved = status[mid]
        # Expiry is a fact about the calendar, not an event anyone sends. A
        # mandate past its validity end is expired whether or not a webhook
        # ever said so.
        if as_of is not None and as_of >= record.validity_end:
            resolved = _absorb(resolved, MandateStatus.EXPIRED)

        book[mid] = MandateState(
            mandate_id=mid,
            status=resolved,
            cause=cause[mid],
            attempts_used=attempts[mid],
            # First presentation is about the *cycle*, not about this process's
            # uptime: a mandate with a stated baseline has already been
            # presented, whatever this stream has witnessed.
            is_first_presentation=(attempts[mid] == 0),
            amount_due_paise=amount[mid],
            max_amount_paise=record.max_amount_paise,
            category=record.category,
            cycle_end=record.cycle_end,
            validity_end=record.validity_end,
            # A PDN is something this system issues and journals; it is never
            # reconstructed from an inbound webhook. The executor's log is the
            # authority for what has been notified.
            pending_pdn=None,
            contacts_used=record.contacts_used,
            issuer_id=record.issuer_id,
            variable_amount_allowed=record.variable_amount_allowed,
            last_error_code=last_code[mid],
        )
    return book


def events_from_journal(journal: Journal) -> list[FailureEvent]:
    """Rehydrate ingested events from the hash-chained log.

    Iterating the journal verifies it, so a book built this way is built from a
    history that has been proved unedited. That is the point of putting webhook
    receipts in the same chain as decisions and effects: provenance runs from
    the bytes on the wire to the debit, in one verifiable line.
    """
    out: list[FailureEvent] = []
    for record in journal:
        if record.kind is not RecordKind.INGEST:
            continue
        body = record.body
        code = body.get("error_code")
        stored = CauseClass(body.get("cause", CauseClass.UNKNOWN.value))
        rederived = diagnose(str(code) if code else None).cause
        out.append(
            FailureEvent(
                event_id=str(body.get("event_id", "")),
                event_type=str(body.get("event_type", "unknown")),
                received_at=datetime.fromisoformat(record.ts),
                mandate_id=str(body.get("mandate_id", "unknown")),
                payment_id=body.get("payment_id"),
                amount_paise=int(body.get("amount_paise", 0)),
                currency=str(body.get("currency", "INR")),
                error_code=code,
                error_description=None,
                method=body.get("method"),
                vpa=None,
                cause=_prefer_terminal(stored, rederived),
                confidence=float(body.get("confidence", 0.0)),
                raw_digest=str(body.get("raw_digest", "")),
                handled=bool(body.get("handled", True)),
                note=str(body.get("note", "")),
            )
        )
    return out
