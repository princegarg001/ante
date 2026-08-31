"""The edge: real webhooks from the rails, turned into canonical events.

Everything else in this system reasons about a `FailureEvent`. This package is
where one comes from when the source is a real payment provider rather than the
simulator, and it is deliberately the smallest, most paranoid component in the
repository.

It is paranoid because it is the only part that processes bytes an attacker
chooses. Three rules follow from that and are enforced structurally:

**Verify before you parse.** The signature is checked against the *raw* body,
before `json.loads` is ever called on it. Parsing first and verifying second is
the standard way this goes wrong — it hands an attacker your JSON parser.

**Compare in constant time.** `hmac.compare_digest`, never `==`. A byte-by-byte
comparison leaks how much of a forged signature was correct.

**Accept an event once.** Providers retry, and a retried `payment.failed` must
not become a second failure in the book. Delivery is at-least-once; the ledger
has to be exactly-once.

It is also deliberately **light**: nothing on this path imports numpy, scipy or
scikit-learn, so the receiver deploys as a small service while the allocator
stays a batch job. That split is not an optimisation. It reflects the
architecture — ingestion genuinely is event-driven, and a system whose whole
thesis is that decisions are committed 24 to 48 hours in advance should not
pretend to make them in an HTTP handler.
"""

from .book import (
    MandateRecord,
    events_from_journal,
    project,
)
from .events import (
    FailureEvent,
    RAZORPAY_EVENTS,
    normalise,
)
from .receiver import (
    Receipt,
    Receiver,
    ReceiverConfig,
)
from .signature import (
    MAX_BODY_BYTES,
    SignatureError,
    verify_signature,
)

__all__ = [
    "FailureEvent",
    "MandateRecord",
    "MAX_BODY_BYTES",
    "RAZORPAY_EVENTS",
    "Receipt",
    "Receiver",
    "ReceiverConfig",
    "SignatureError",
    "events_from_journal",
    "normalise",
    "project",
    "verify_signature",
]
