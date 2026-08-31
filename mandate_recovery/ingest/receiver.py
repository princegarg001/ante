"""The receiver: verify, deduplicate, normalise, record.

Framework-agnostic on purpose. `Receiver.handle` takes raw bytes and headers and
returns a `Receipt`; the WSGI adapter in `wsgi.py` is thirty lines around it.
That means the interesting behaviour is testable without a server, and the same
code runs unchanged locally, behind a tunnel, or on a host.

## Exactly-once, over an at-least-once transport

Providers retry. Razorpay will re-deliver a `payment.failed` on timeout, on a
5xx, and sometimes for no visible reason at all. A retried failure must not
become a second failure in the book, so every event is keyed on its
`X-Razorpay-Event-Id` and a repeat is acknowledged without being re-recorded.

The deduplication index is rebuilt from the journal rather than held separately,
for the same reason the executor's ledger is: a second source of truth is a
second thing to get out of sync.

## Acknowledge fast, decide later

The handler verifies, records, and returns. It does **not** run the allocator.

That is not a performance decision. This system's entire thesis is that a debit
must be committed 24 to 48 hours in advance and executed blind — deciding inside
an HTTP handler would contradict the constraint the design is built around. The
webhook is how work *arrives*; the allocator is a batch job that runs against the
book. Blurring the two would make the architecture worse and the story weaker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Final, Mapping

from ..act.journal import Journal, RecordKind
from .events import FailureEvent, normalise
from .signature import (
    EVENT_ID_HEADER,
    MAX_BODY_BYTES,
    SignatureError,
    body_digest,
    header,
    verify_signature,
)

#: Events older than this are refused even with a valid signature. A correctly
#: signed request replayed weeks later is not a webhook, it is a replay.
MAX_EVENT_AGE_SECONDS: Final[int] = 7 * 24 * 3600


@dataclass(frozen=True, slots=True)
class Receipt:
    """What the handler decided, and what the caller should be told."""

    status: int
    outcome: str  # accepted | duplicate | unhandled | rejected | error
    event_id: str | None
    detail: str
    event: FailureEvent | None = None

    def to_json(self) -> str:
        # Deliberately terse. A webhook response is read by a machine, and
        # anything more would be free reconnaissance for anyone probing it.
        return json.dumps({"status": self.outcome})


@dataclass(frozen=True, slots=True)
class ReceiverConfig:
    secret: str
    journal_path: Path
    #: Where wall-clock time enters. Injected rather than read, because nothing
    #: in this package reads the clock -- that is what makes a run replayable,
    #: and `tests/test_purity.py` enforces it over the AST. The WSGI adapter
    #: supplies the real one; a test supplies a fixed one and gets a receiver
    #: whose output is a pure function of its input.
    clock: Callable[[], datetime]
    max_body_bytes: int = MAX_BODY_BYTES
    max_event_age_seconds: int = MAX_EVENT_AGE_SECONDS
    #: Reject events whose `created_at` is far in the past. Off by default
    #: because Razorpay's test console can replay old fixtures and refusing them
    #: makes the endpoint confusing to demo.
    enforce_event_age: bool = False


class Receiver:
    """Stateless per-request; durable through the journal."""

    def __init__(self, config: ReceiverConfig) -> None:
        self.config = config
        self.journal = Journal(config.journal_path)
        self.journal.open()
        self._seen: set[str] = self._replay_seen()

    def _replay_seen(self) -> set[str]:
        """Rebuild the dedupe index from the journal.

        Restarting the receiver must not make it forget which events it has
        already accepted, or a provider retry after a deploy becomes a duplicate
        failure in the book.
        """
        seen: set[str] = set()
        for record in self.journal:
            if record.kind is RecordKind.INGEST:
                event_id = record.body.get("event_id")
                if event_id:
                    seen.add(str(event_id))
        return seen

    # -- the handler -------------------------------------------------------

    def handle(self, raw_body: bytes, headers: Mapping[str, str]) -> Receipt:
        """Process one delivery. Never raises; every path returns a Receipt.

        The guarantee is load-bearing rather than decorative. This is the only
        entry point reachable by an unauthenticated caller, and an exception
        escaping it is a remote crash — so the promise is enforced here rather
        than assumed to hold because every path below happens to be careful
        today. A bug becomes a 500 the provider will retry, not a stack trace.
        """
        try:
            return self._handle(raw_body, headers)
        except Exception:  # noqa: BLE001 -- see the docstring; this is the wall
            # Deliberately no detail in the receipt: a 500 that describes the
            # exception is free reconnaissance. The event is not marked seen, so
            # the provider's retry can still succeed once the bug is fixed.
            return Receipt(500, "error", None, "internal error")

    def _handle(self, raw_body: bytes, headers: Mapping[str, str]) -> Receipt:
        now = self.config.clock()

        # 1. Authenticity, before the body is parsed or even decoded.
        try:
            verify_signature(
                raw_body,
                headers,
                self.config.secret,
                max_bytes=self.config.max_body_bytes,
            )
        except SignatureError as exc:
            # 401 rather than 400: the request is not authentic, and the reason
            # stays in the log rather than in the response.
            return Receipt(401, "rejected", None, str(exc))
        except Exception:  # noqa: BLE001
            # Anything malformed enough to break the verifier is, by definition,
            # not authentic. Reporting it as an internal error instead would let
            # a caller distinguish "crashed your verifier" from "wrong secret",
            # which is exactly the signal not to give them.
            return Receipt(401, "rejected", None, "verification failed")

        digest = body_digest(raw_body)
        event_id = (header(headers, EVENT_ID_HEADER) or "").strip() or f"sha:{digest[:32]}"

        # 2. Exactly-once. Checked before parsing, because a duplicate needs no
        #    further work and the cheapest response is the right one.
        if event_id in self._seen:
            return Receipt(200, "duplicate", event_id, "already recorded")

        # 3. Now, and only now, parse.
        try:
            body = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return Receipt(400, "rejected", event_id, "body is not valid JSON")
        if not isinstance(body, dict):
            return Receipt(400, "rejected", event_id, "body is not a JSON object")

        if self.config.enforce_event_age and not self._is_fresh(body, now):
            return Receipt(400, "rejected", event_id, "event is too old")

        event = normalise(
            body, event_id=event_id, raw_digest=digest, received_at=now
        )

        # 4. Record. An unhandled event type is still recorded and still
        #    acknowledged — refusing it would have the provider retry forever.
        self._record(event)
        self._seen.add(event_id)

        if not event.handled:
            return Receipt(200, "unhandled", event_id, event.note, event)
        return Receipt(
            200,
            "accepted",
            event_id,
            f"{event.event_type} → {event.cause.value}",
            event,
        )

    # -- internals ---------------------------------------------------------

    def _is_fresh(self, body: Mapping[str, Any], now: datetime) -> bool:
        created = body.get("created_at")
        if not isinstance(created, (int, float)):
            return True                     # nothing to check against
        age = now.timestamp() - float(created)
        return age <= self.config.max_event_age_seconds

    def _record(self, event: FailureEvent) -> None:
        self.journal.append(
            RecordKind.INGEST,
            run_id="ingest",
            ts=event.received_at.isoformat(),
            body=event.to_body(),
        )

    # -- inspection --------------------------------------------------------

    @property
    def accepted_count(self) -> int:
        return len(self._seen)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """The last few accepted events, for a health page or a demo."""
        rows = [
            record.body
            for record in self.journal
            if record.kind is RecordKind.INGEST
        ]
        return rows[-limit:]

    def verify_chain(self) -> int:
        """Walk the hash chain. Raises `TamperError` if the log was edited."""
        return self.journal.verify()
