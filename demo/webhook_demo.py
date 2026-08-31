"""End-to-end demonstration of the webhook edge, against a live HTTP server.

Not a test. The tests assert; this shows. It starts a real WSGI server on a real
socket, sends real signed HTTP requests at it, and prints what the endpoint did
with each one -- including the ones it refuses, which are the interesting half.

    python -m demo.webhook_demo          # or: make webhook

Nothing here is mocked. The signature is computed the way Razorpay computes it,
the server is the one that would run in production, and the journal it writes is
the same hash-chained log the executor uses. The last step rebuilds the book
from that journal and hands it to the constraint layer, so the run ends by
showing an actual regulatory verdict on a mandate that arrived over the wire.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server

from mandate_recovery.constraints.rules import is_permitted
from mandate_recovery.core.clock import IST, non_peak_slots
from mandate_recovery.core.types import Category, Commit
from mandate_recovery.ingest.book import MandateRecord, events_from_journal, project
from mandate_recovery.ingest.receiver import Receiver, ReceiverConfig
from mandate_recovery.ingest.signature import expected_signature
from mandate_recovery.ingest.wsgi import WEBHOOK_PATH, make_application

SECRET = "whsec_demo_do_not_use_in_production"
HOST = "127.0.0.1"

GREEN = "\033[32m"
RED = "\033[31m"
DIM = "\033[2m"
BOLD = "\033[1m"
OFF = "\033[0m"


class _Quiet(WSGIRequestHandler):
    """The server's own access log would drown the narration."""

    def log_message(self, *args: object) -> None:
        return


def rule(title: str) -> None:
    print(f"\n{BOLD}{title}{OFF}")
    print(DIM + "-" * 72 + OFF)


def payload(
    *,
    subscription_id: str,
    event: str = "payment.failed",
    error_reason: str = "insufficient_funds",
    amount: int = 49900,
) -> bytes:
    """A body shaped the way Razorpay actually shapes one."""
    return json.dumps(
        {
            "entity": "event",
            "account_id": "acc_DEMO",
            "event": event,
            "contains": ["payment", "subscription"],
            "created_at": int(time.time()),
            "payload": {
                "payment": {
                    "entity": {
                        "id": f"pay_{subscription_id}_{amount}",
                        "entity": "payment",
                        "amount": amount,
                        "currency": "INR",
                        "status": "failed",
                        "method": "upi",
                        "vpa": "customer@okhdfcbank",
                        # The coarse class and the specific reason, exactly as
                        # the provider sends them. Classifying on the first of
                        # these instead of the second is the bug this fixture
                        # exists to keep caught.
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_description": "Payment failed",
                        "error_reason": error_reason,
                    }
                },
                "subscription": {
                    "entity": {
                        "id": subscription_id,
                        "entity": "subscription",
                        "status": "pending",
                    }
                },
            },
        }
    ).encode("utf-8")


def post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def signed(body: bytes, event_id: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": expected_signature(body, SECRET),
        "X-Razorpay-Event-Id": event_id,
    }


def show(label: str, status: int, body: str, *, expect: int) -> None:
    ok = status == expect
    mark = f"{GREEN}OK{OFF}" if ok else f"{RED}UNEXPECTED{OFF}"
    print(f"  {label:<44} {status}  {body.strip():<24} {mark}")
    if not ok:
        raise SystemExit(f"demo failed: expected {expect}, got {status}")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="ante-webhook-demo-"))
    journal_path = workdir / "webhooks.jsonl"

    receiver = Receiver(ReceiverConfig(secret=SECRET, journal_path=journal_path))
    # Port 0: the OS picks a free one, so the demo cannot collide with whatever
    # else is listening on this machine.
    server = make_server(HOST, 0, make_application(receiver), handler_class=_Quiet)
    port = server.server_port
    url = f"http://{HOST}:{port}{WEBHOOK_PATH}"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        print(f"\n{BOLD}The webhook edge, end to end{OFF}")
        print(f"{DIM}live server on {HOST}:{port}   journal {journal_path}{OFF}")

        # ------------------------------------------------------------------ #
        rule("1 · What it refuses")
        print(f"{DIM}  The interesting half. Every one of these is a way a webhook")
        print(f"  consumer gets broken into or falls over.{OFF}\n")

        body = payload(subscription_id="sub_DEMO_1")

        status, text = post(url, body, {"Content-Type": "application/json"})
        show("no signature at all", status, text, expect=401)

        status, text = post(url, body, {"X-Razorpay-Signature": "0" * 64})
        show("a signature that is simply wrong", status, text, expect=401)

        forged = expected_signature(body, "whsec_someone_elses_secret")
        status, text = post(url, body, {"X-Razorpay-Signature": forged})
        show("correctly signed, with the wrong secret", status, text, expect=401)

        # One byte changed after signing. This is the case that a system which
        # parses before verifying, or re-serialises in between, gets wrong.
        tampered = body.replace(b'"amount": 49900', b'"amount": 99900')
        status, text = post(url, tampered, signed(body, "evt_tampered"))
        show("body altered after signing", status, text, expect=401)

        # A 64-character header that is not hex. Length alone is not a shape
        # check, and this input used to crash the comparison.
        status, text = post(url, body, {"X-Razorpay-Signature": "e" * 63 + "z"})
        show("right length, wrong alphabet", status, text, expect=401)

        status, text = post(
            url, b"{not json at all", {"X-Razorpay-Signature": "0" * 64}
        )
        show("malformed JSON, bad signature", status, text, expect=401)
        print(f"{DIM}    ^ fails on the signature, not the parse: the parser never ran{OFF}")

        # ------------------------------------------------------------------ #
        rule("2 · What it accepts, once")
        print(f"{DIM}  Delivery is at-least-once. The ledger has to be exactly-once.{OFF}\n")

        status, text = post(url, body, signed(body, "evt_001"))
        show("a genuine payment.failed", status, text, expect=200)

        for attempt in (2, 3):
            status, text = post(url, body, signed(body, "evt_001"))
            show(f"the same event, re-delivered (#{attempt})", status, text, expect=200)
        print(f"{DIM}    ^ acknowledged so the provider stops retrying, recorded once{OFF}")

        second = payload(subscription_id="sub_DEMO_1", amount=49900)
        status, text = post(url, second, signed(second, "evt_002"))
        show("a second, genuinely different failure", status, text, expect=200)

        revoked = payload(
            subscription_id="sub_DEMO_2", error_reason="mandate_revoked"
        )
        status, text = post(url, revoked, signed(revoked, "evt_003"))
        show("a failure on a revoked mandate", status, text, expect=200)

        transient = payload(
            subscription_id="sub_DEMO_3", error_reason="bank_technical_error"
        )
        status, text = post(url, transient, signed(transient, "evt_005"))
        show("an issuer that was simply down", status, text, expect=200)

        unknown = payload(subscription_id="sub_DEMO_1", event="subscription.updated")
        status, text = post(url, unknown, signed(unknown, "evt_004"))
        show("an event type we do not handle", status, text, expect=200)
        print(f"{DIM}    ^ recorded as unhandled, not 4xx'd: refusing it would have{OFF}")
        print(f"{DIM}      a correctly-behaving provider retry it forever{OFF}")

        # ------------------------------------------------------------------ #
        rule("3 · The chain")

        records = list(receiver.journal)
        verified = receiver.verify_chain()
        print(f"  hash-chained records written        {verified}")
        print(f"  distinct events accepted            {receiver.accepted_count}")
        print(f"{DIM}  Iterating the journal verifies it. These receipts live in the")
        print(f"  same chain as decisions and effects, so provenance runs from the")
        print(f"  bytes on the wire to the debit in one line.{OFF}\n")
        print(DIM + json.dumps(records[0].body, indent=2)[:420] + OFF)

        # ------------------------------------------------------------------ #
        rule("4 · From the wire to a regulatory verdict")

        events = events_from_journal(receiver.journal)
        now = datetime.now(IST)
        registry = {
            "sub_DEMO_1": MandateRecord(
                mandate_id="sub_DEMO_1",
                issuer_id="HDFC",
                category=Category.STANDARD,
                max_amount_paise=100_000,
                cycle_end=now + timedelta(days=9),
                validity_end=now + timedelta(days=300),
                # Deployed mid-cycle: the merchant already knows about two
                # attempts this stream never witnessed. Counting from zero here
                # is not a reporting error, it is a breach of the attempt cap
                # that looks like a working system.
                attempts_before=2,
            ),
            "sub_DEMO_2": MandateRecord(
                mandate_id="sub_DEMO_2",
                issuer_id="ICICI",
                category=Category.STANDARD,
                max_amount_paise=100_000,
                cycle_end=now + timedelta(days=9),
                validity_end=now + timedelta(days=300),
            ),
            "sub_DEMO_3": MandateRecord(
                mandate_id="sub_DEMO_3",
                issuer_id="SBIN",
                category=Category.STANDARD,
                max_amount_paise=100_000,
                cycle_end=now + timedelta(days=9),
                validity_end=now + timedelta(days=300),
            ),
        }
        book = project(registry, events, as_of=now)

        print(f"  {'mandate':<14}{'status':<10}{'cause':<22}{'attempts':>9}")
        for mandate_id, state in book.items():
            print(
                f"  {mandate_id:<14}{state.status.value:<10}"
                f"{state.cause.value:<22}{state.attempts_used:>9}"
            )

        # A lawful execution slot. The PDN aperture is two-sided -- [T-48h,
        # T-24h] -- so the earliest thing that could legally be committed now is
        # a little over 24 hours out. It is drawn from the non-peak grid rather
        # than computed by arithmetic, because the peak windows are not ours to
        # choose and a slot that lands inside one is not a slot.
        target = next(
            non_peak_slots(now + timedelta(hours=26), now + timedelta(hours=47))
        )
        print(f"\n{DIM}  Now ask the constraint layer whether a debit may be committed")
        print(f"  at {target:%a %d %b %H:%M} IST — a non-peak slot inside the aperture.{OFF}\n")

        for mandate_id, state in book.items():
            verdict = is_permitted(
                Commit(execute_at=target, amount_paise=state.amount_due_paise),
                state,
                now,
            )
            if verdict:
                print(f"  {mandate_id:<14}{GREEN}permitted{OFF}")
            else:
                print(
                    f"  {mandate_id:<14}{RED}refused{OFF}   "
                    f"[{verdict.rule_id}] {verdict.reason}"
                )

        print(
            f"\n{DIM}  sub_DEMO_1 used 2 attempts before ingestion began and 2 more"
            f"\n  witnessed here. Counted from zero it would read 2/4 and this debit"
            f"\n  would have been permitted -- a breach of the cap, with nothing in"
            f"\n  the logs to show for it."
            f"\n"
            f"\n  sub_DEMO_2 was closed on a failure code alone. No cancellation"
            f"\n  event was ever sent; the system stopped presenting anyway."
            f"\n"
            f"\n  sub_DEMO_3 failed on an issuer outage, which is nobody's fault and"
            f"\n  worth retrying. It is the one the allocator gets to price."
            f"{OFF}"
        )

        print(f"\n{GREEN}{BOLD}Nothing was mocked.{OFF} Real socket, real HMAC, real journal.\n")
        return 0
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
