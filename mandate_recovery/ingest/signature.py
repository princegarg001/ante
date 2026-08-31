"""Webhook signature verification.

Razorpay signs the **raw request body** with the webhook secret using
HMAC-SHA256 and sends the hex digest in `X-Razorpay-Signature`. Verifying it is
four lines. Getting it wrong is also four lines, and the wrong version passes
every happy-path test.

The failure modes this file exists to prevent, each of which has shipped in real
systems:

**Parsing before verifying.** Convenient, because frameworks hand you a decoded
body. It also means an unauthenticated attacker reaches your JSON parser, and
any re-serialisation between parse and verify silently changes the bytes the
signature covers. The signature is over the bytes that arrived, so those are the
bytes that must be checked.

**Comparing with `==`.** String equality short-circuits on the first differing
byte, so the time it takes reveals how much of a guess was right. That is enough
to forge a signature given enough attempts. `hmac.compare_digest` does not
short-circuit.

**Unbounded bodies.** A webhook endpoint is a public POST target. Without a size
limit it is a memory-exhaustion primitive that requires no credentials at all —
the signature check happens *after* you have already read the body.

**Trusting a missing signature.** An absent header must be a rejection, never a
skip. A surprising number of implementations treat "no signature" as "nothing to
check".
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Final, Mapping

#: Razorpay's own header. Compared case-insensitively, because WSGI, ASGI and
#: various proxies all disagree about header casing.
SIGNATURE_HEADER: Final[str] = "x-razorpay-signature"
EVENT_ID_HEADER: Final[str] = "x-razorpay-event-id"

#: A webhook body is a few kilobytes. Anything approaching this is not a webhook,
#: and the limit applies before verification because reading the body is what
#: costs memory.
MAX_BODY_BYTES: Final[int] = 512 * 1024

#: SHA-256 hex is exactly 64 characters. Checking the shape first turns a class
#: of malformed input into a cheap rejection.
_DIGEST_HEX_LEN: Final[int] = 64

#: The only characters a hex digest can contain. The shape check is not
#: cosmetic: `hmac.compare_digest` raises `TypeError` on a `str` containing
#: non-ASCII, so a header of the right *length* but the wrong *alphabet* turns
#: an authentication failure into an unhandled exception — reachable by anyone,
#: with no credentials. Rejecting on the alphabet keeps that input on the
#: rejection path where it belongs.
_HEX_ALPHABET: Final[frozenset[str]] = frozenset("0123456789abcdefABCDEF")


class SignatureError(Exception):
    """The request is not authentic. Deliberately carries no detail about *why*.

    Telling a caller whether the signature was malformed, absent, or merely
    wrong is free information for someone probing the endpoint. The server logs
    the reason; the response does not.
    """


def header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup, because every transport disagrees."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def expected_signature(raw_body: bytes, secret: str) -> str:
    """HMAC-SHA256 of the raw body, hex-encoded."""
    return hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()


def verify_signature(
    raw_body: bytes,
    headers: Mapping[str, str],
    secret: str,
    *,
    max_bytes: int = MAX_BODY_BYTES,
) -> str:
    """Authenticate a webhook. Returns the verified signature, or raises.

    Order matters and is the whole point of the function:

        1. bound the body            — before anything else touches it
        2. require a signature       — absence is rejection, not a skip
        3. check its shape           — cheap, and rejects obvious junk
        4. constant-time compare     — never `==`

    Only after this returns may the body be parsed.
    """
    if not secret:
        # Refusing to run without a secret is safer than running without
        # verification. A misconfigured endpoint that accepts everything is
        # worse than one that accepts nothing.
        raise SignatureError("webhook secret is not configured")

    if len(raw_body) > max_bytes:
        raise SignatureError("body too large")

    provided = header(headers, SIGNATURE_HEADER)
    if not provided:
        raise SignatureError("missing signature")

    provided = provided.strip()
    if len(provided) != _DIGEST_HEX_LEN or not _HEX_ALPHABET.issuperset(provided):
        raise SignatureError("malformed signature")

    expected = expected_signature(raw_body, secret)
    if not hmac.compare_digest(provided, expected):
        raise SignatureError("signature mismatch")

    return provided


def body_digest(raw_body: bytes) -> str:
    """SHA-256 of the raw body, for the audit trail.

    Recorded so a journal entry can be tied back to the exact bytes that
    produced it, without the journal having to store a payload that may contain
    customer detail.
    """
    return hashlib.sha256(raw_body).hexdigest()
