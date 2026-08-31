# The edge

::: tip Status
Built and tested — 49 tests across the receiver and the projection. Stdlib only: nothing on this path imports numpy, scipy or scikit-learn, and a test enforces it.
:::

This is the only component in the system that processes bytes an attacker chooses. Everything
else consumes objects that some part of this repository constructed. So the tests here are
mostly about what it **refuses**: accepting a valid webhook is one test, and the rest are the
ways a webhook consumer gets broken into or falls over.

<div class="stat-grid">
  <div class="stat ok"><span class="v">0</span><span class="k">heavy imports on the edge</span></div>
  <div class="stat"><span class="v">49</span><span class="k">tests</span></div>
  <div class="stat"><span class="v">512 KB</span><span class="k">body cap, applied to the read</span></div>
  <div class="stat ok"><span class="v">exactly-once</span><span class="k">over an at-least-once transport</span></div>
</div>

## Four modules

<div class="table-scroll">

| Module | Responsibility |
| --- | --- |
| `signature.py` | HMAC-SHA256 over the **raw** body, constant-time compare |
| `events.py` | Razorpay's payload shape → one canonical `FailureEvent` |
| `receiver.py` | Verify → deduplicate → parse → normalise → journal |
| `book.py` | Fold the event stream over the registry into `MandateState` |
| `wsgi.py` | Transport. Thirty lines, stdlib server, no framework |

</div>

## Verify before you parse

The signature is checked against the raw body before `json.loads` is ever called on it.
Parsing first and verifying second is the standard way this goes wrong — it hands an attacker
your JSON parser, which is a much larger surface than your HMAC.

The order inside `verify_signature` is itself the design: bound the body, require a signature,
check its shape, then compare. Each step is cheaper than the next, and each one that fails
means the expensive ones never run.

```python
if len(raw_body) > max_bytes:            # a public POST with no size limit is a
    raise SignatureError("body too large")   # memory-exhaustion primitive that
                                             # needs no credentials
provided = header(headers, SIGNATURE_HEADER)
if not provided:
    raise SignatureError("missing signature")   # "no signature" is a rejection,
                                                # never "nothing to check"
if len(provided) != 64 or not _HEX_ALPHABET.issuperset(provided):
    raise SignatureError("malformed signature")     # length is not a shape
                                                    # check -- see below
if not hmac.compare_digest(provided, expected):  # never ==; byte-by-byte
    raise SignatureError("signature mismatch")   # comparison leaks how much of
                                                 # a forgery was correct
```

An empty secret refuses to verify anything rather than verifying everything, and the WSGI
adapter refuses to **start** without `RAZORPAY_WEBHOOK_SECRET`. An endpoint that runs without
a secret accepts the whole internet, and it does so silently.

## Length is not a shape check

Two defects found by reading this file after it was written and passing, both remotely
reachable, neither visible in a happy-path test.

The shape check originally verified the signature's **length** and nothing else. But
`hmac.compare_digest` raises `TypeError` on a `str` containing non-ASCII — so a header of
exactly 64 characters with one accented byte passed the length check and crashed the
comparison. An unauthenticated caller could turn an authentication failure into an unhandled
exception with a single header. Checking the alphabet as well as the length keeps that input on
the rejection path where it belongs.

The second is the reason the first one mattered so much. `Receiver.handle` documented that it
never raises, and that promise was load-bearing — it is the only entry point an unauthenticated
caller can reach — but it was merely true rather than enforced. It now is enforced, with a
deliberate asymmetry:

<div class="table-scroll">

| Where it breaks | Response | Why |
| --- | --- | --- |
| Inside verification | `401 rejected` | Anything malformed enough to break the verifier is not authentic. A 500 here would let a caller distinguish "crashed your verifier" from "wrong secret" |
| After verification | `500 error` | A genuine bug. The provider retries, and the event is **not** marked seen, so the retry can succeed once it is fixed |

</div>

Neither response carries the exception. A 500 that describes what broke is free reconnaissance.
Acknowledging with a 200 would be worse than either: the provider would never re-deliver, and
the failure would be silent and permanent.

## Exactly-once, over an at-least-once transport

Razorpay re-delivers on timeout, on a 5xx, and sometimes for no visible reason. A retried
`payment.failed` must not become a second failure in the book — the count it inflates is the
one the regulatory attempt cap is enforced against.

Every delivery is keyed on `X-Razorpay-Event-Id`, and the dedupe index is **rebuilt from the
journal** on startup rather than held separately. A second source of truth is a second thing to
get out of sync, and the failure it produces — a provider retry after a deploy becoming a
duplicate failure — is invisible until it has already happened.

## What the payload actually says

Razorpay carries two different things under similar names:

<div class="table-scroll">

| Field | Contents | Example |
| --- | --- | --- |
| `error_code` | a coarse class | `BAD_REQUEST_ERROR` |
| `error_reason` | the specific one | `insufficient_funds` |

</div>

Classifying on `error_code` maps every failure in the book to the same handful of useless
buckets. The first version of this file did exactly that, and it was caught only because the
test fixture was built from a real payload rather than from what the code expected.

The provider's own words are evidence, never a verdict. The reason is handed to the diagnosis
layer, which applies the rule table and the one-way ratchet. A provider saying
`error_reason: "payment_failed"` is not a diagnosis.

## Acknowledge fast, decide later

The handler verifies, records, and returns. It does **not** run the allocator.

That is not a performance decision. This system's entire thesis is that a debit must be
committed 24 to 48 hours in advance and executed blind — deciding inside an HTTP handler would
contradict the constraint the whole design is built around. The webhook is how work *arrives*;
the allocator is a batch job that runs against the book.

That split is also why the edge is light. Nothing on this import path reaches the scientific
stack, so the receiver deploys as a small service while the allocator stays a batch job — and
a test asserts it in a subprocess, because a single convenience import in a future edit would
quietly make the claim false.

## The projection

The receiver records failures. The allocator reasons about `MandateState`. `book.py` is the
fold between them, and it takes two inputs, because a webhook cannot tell you everything:

<div class="table-scroll">

| Input | Authority for |
| --- | --- |
| **registry** | What the mandate *is* — ceiling, category, validity end, issuer |
| **events** | What has *happened* — failures, charges, lifecycle changes |

</div>

A `payment.failed` says what happened. It does not say what the mandate permits, because that
was fixed at registration and is not news. Deriving a mandate's ceiling from a failure message
would mean inferring a regulatory limit from an error string.

Two things the fold gets right that a naive one does not.

**Attempts count from a stated baseline.** Counting failures in the stream and calling that
`attempts_used` is only correct if the webhook has seen the whole cycle. Deploy mid-cycle and
it undercounts — and that number is what the cap (one execution plus three retries) is enforced
against. It does not error. It just breaks the law, while looking like a working system.
`MandateRecord.attempts_before` carries what the merchant already knows, and the fold adds to
it.

**Status ratchets one way.** Webhooks arrive out of order; the transport makes no promise. A
`subscription.charged` re-delivered after a cancellation is *older* than the cancellation, and
a naive fold takes the last one it sees, marks the mandate live, and debits someone who
cancelled. Terminal statuses absorb — for the same reason terminal *causes* do. Being wrongly
cautious costs an attempt; being wrongly confident costs a customer.

`halted` is deliberately **not** read as a revocation. In Razorpay it means retries exhausted,
subscription stopped — the mandate survives. Reading it as revoked strands recoverable money;
reading a revocation as a pause debits someone who left. The asymmetry is why that mapping is a
table and not an inference.

Because the fold sorts by arrival, the book is a function of the event *set*, not of the
delivery sequence: replay it in any order and the answer is the same. A test shuffles the
stream twenty times and asserts the projection is unchanged.

## One chain, wire to debit

Webhook receipts land in the **same hash-chained journal** as decisions and effects, as
`INGEST` records. Iterating that journal verifies it, so a book rebuilt with
`events_from_journal` is built from a history that has been proved unedited.

That is the point of not giving ingestion its own log: provenance runs from the bytes on the
wire, through the diagnosis, to the debit, in one verifiable line.

## Seeing it work

`make webhook` starts a real WSGI server on a real socket, sends real signed HTTP requests at
it, and prints what the endpoint did with each. Nothing is mocked — the signature is computed
the way Razorpay computes it, and the journal it writes is the same hash-chained log the
executor uses.

```
1 · What it refuses
  no signature at all                          401  {"status": "rejected"}   OK
  a signature that is simply wrong             401  {"status": "rejected"}   OK
  correctly signed, with the wrong secret      401  {"status": "rejected"}   OK
  body altered after signing                   401  {"status": "rejected"}   OK
  right length, wrong alphabet                 401  {"status": "rejected"}   OK
  malformed JSON, bad signature                401  {"status": "rejected"}   OK
    ^ fails on the signature, not the parse: the parser never ran

2 · What it accepts, once
  a genuine payment.failed                     200  {"status": "accepted"}   OK
  the same event, re-delivered (#2)            200  {"status": "duplicate"}  OK
  the same event, re-delivered (#3)            200  {"status": "duplicate"}  OK
  a second, genuinely different failure        200  {"status": "accepted"}   OK
  a failure on a revoked mandate               200  {"status": "accepted"}   OK
  an issuer that was simply down               200  {"status": "accepted"}   OK
  an event type we do not handle               200  {"status": "unhandled"}  OK
```

The run ends by rebuilding the book from that journal and asking the constraint layer for a
verdict on each mandate — the whole path, from bytes on the wire to a cited rule:

```
  mandate       status    cause                  attempts
  sub_DEMO_1    LIVE      INSUFFICIENT_FUNDS            4
  sub_DEMO_2    REVOKED   MANDATE_REVOKED               1
  sub_DEMO_3    LIVE      TRANSIENT_ISSUER              1

  sub_DEMO_1    refused   [C1] retry budget exhausted: 4/4 attempts used
  sub_DEMO_2    refused   [C12] mandate status is REVOKED, not LIVE
  sub_DEMO_3    permitted
```

`sub_DEMO_1` is the case the projection exists for. Two attempts were used before ingestion
began and two more were witnessed here. Counted from zero it would read 2/4, this debit would
have been permitted, and the cap would have been breached with nothing in the logs to show for
it. `sub_DEMO_2` was closed on a failure code alone — no cancellation event was ever sent.

The demo asserts its own expectations and exits non-zero if any of them changes, so it runs in
CI rather than only in a screen recording.

## Running it

```bash
export RAZORPAY_WEBHOOK_SECRET=whsec_...
python -m mandate_recovery.ingest.wsgi          # stdlib server, port 8080
gunicorn mandate_recovery.ingest.wsgi:application
```

<div class="table-scroll">

| Route | Purpose |
| --- | --- |
| `POST /webhooks/razorpay` | The endpoint |
| `GET /health` | Accepted count, and a full chain verification |
| `GET /recent` | The last twenty accepted events |

</div>

Configuration is environment-only. A secret in a config file is a secret in a commit sooner or
later.
