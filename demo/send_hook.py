"""Fire a correctly-signed webhook at a running receiver.

For poking a live endpoint by hand -- the counterpart to `demo/webhook_demo.py`,
which stands up its own server and tears it down. This one assumes a server is
already listening and lets you drive it.

    python -m demo.send_hook                          # one insufficient_funds
    python -m demo.send_hook --reason mandate_revoked
    python -m demo.send_hook --mandate sub_X --count 3
    python -m demo.send_hook --tamper                 # should be refused

The secret defaults to the one used by the local demos. Point it at a real
endpoint with --url and pass the matching --secret.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

from mandate_recovery.ingest.signature import expected_signature

DEFAULT_URL = "http://127.0.0.1:8080/webhooks/razorpay"
DEFAULT_SECRET = "whsec_live_test_1234"


def build(mandate: str, reason: str, amount: int, event: str) -> bytes:
    return json.dumps(
        {
            "entity": "event",
            "account_id": "acc_LIVE",
            "event": event,
            "contains": ["payment", "subscription"],
            "created_at": int(time.time()),
            "payload": {
                "payment": {
                    "entity": {
                        "id": f"pay_{mandate}_{amount}_{int(time.time() * 1000) % 100000}",
                        "entity": "payment",
                        "amount": amount,
                        "currency": "INR",
                        "status": "failed",
                        "method": "upi",
                        "vpa": "customer@okhdfcbank",
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_description": "Payment failed",
                        "error_reason": reason,
                    }
                },
                "subscription": {
                    "entity": {"id": mandate, "entity": "subscription", "status": "pending"}
                },
            },
        }
    ).encode("utf-8")


def send(url: str, body: bytes, secret: str, event_id: str, tamper: bool) -> None:
    signature = expected_signature(body, secret)
    if tamper:
        # Sign the original, then change a byte. This is the case a receiver
        # that parses before verifying, or re-serialises in between, gets wrong.
        body = body.replace(b'"amount": 49900', b'"amount": 9900000')
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
            "X-Razorpay-Event-Id": event_id,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            print(f"  {event_id:<22} {response.status}  {response.read().decode()}")
    except urllib.error.HTTPError as exc:
        print(f"  {event_id:<22} {exc.code}  {exc.read().decode()}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"could not reach {url}: {exc.reason}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--secret", default=DEFAULT_SECRET)
    p.add_argument("--mandate", default="sub_LIVE_1")
    p.add_argument("--reason", default="insufficient_funds")
    p.add_argument("--amount", type=int, default=49900)
    p.add_argument("--event", default="payment.failed")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--repeat-id", action="store_true",
                   help="reuse one event id, so the receiver should dedupe")
    p.add_argument("--tamper", action="store_true",
                   help="alter the body after signing; expect 401")
    args = p.parse_args()

    print(f"\n-> {args.url}")
    stamp = int(time.time())
    for i in range(args.count):
        event_id = (
            f"evt_{stamp}" if args.repeat_id else f"evt_{stamp}_{i}"
        )
        send(
            args.url,
            build(args.mandate, args.reason, args.amount, args.event),
            args.secret,
            event_id,
            args.tamper,
        )
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
