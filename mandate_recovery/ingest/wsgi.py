"""A WSGI adapter around the receiver. Standard library only.

Deliberately no web framework. The interesting behaviour lives in
`Receiver.handle`, which takes bytes and returns a `Receipt`; everything here is
transport. Keeping it framework-free means the receiver deploys as a small
service with no dependency on numpy, scipy or scikit-learn — which matters,
because the allocator's dependency set has no business on a public endpoint.

Runs anywhere WSGI runs:

    python -m mandate_recovery.ingest.wsgi           # local, stdlib server
    gunicorn mandate_recovery.ingest.wsgi:application

Configuration is environment-only. A secret in a config file is a secret in a
commit sooner or later.

    RAZORPAY_WEBHOOK_SECRET   required; the endpoint refuses to start without it
    ANTE_JOURNAL              path to the hash-chained journal
    ANTE_PORT                 local server port, default 8080
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable

from .receiver import Receipt, Receiver, ReceiverConfig

WEBHOOK_PATH = "/webhooks/razorpay"
HEALTH_PATH = "/health"
RECENT_PATH = "/recent"


def _config_from_env() -> ReceiverConfig:
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
    if not secret:
        # Failing to start is the correct behaviour. An endpoint that runs
        # without a secret accepts anything, and it would do so silently.
        raise RuntimeError(
            "RAZORPAY_WEBHOOK_SECRET is not set; refusing to start an endpoint "
            "that cannot verify signatures"
        )
    return ReceiverConfig(
        secret=secret,
        journal_path=Path(os.environ.get("ANTE_JOURNAL", "runs/ingest/webhooks.jsonl")),
    )


def _headers_from_environ(environ: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            out[key[5:].replace("_", "-").lower()] = value
    return out


def _read_body(environ: dict[str, Any], limit: int) -> bytes:
    """Read at most `limit` bytes, whatever Content-Length claims.

    Trusting the declared length is how a bad one becomes a memory problem, so
    the cap is applied to the read itself rather than to the header.
    """
    try:
        declared = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        declared = 0
    stream = environ.get("wsgi.input")
    if stream is None:
        return b""
    return stream.read(min(declared, limit) if declared > 0 else limit)


def make_application(receiver: Receiver | None = None) -> Callable:
    """Build the WSGI callable. The receiver is injectable for tests."""
    resolved = receiver or Receiver(_config_from_env())

    def application(
        environ: dict[str, Any], start_response: Callable
    ) -> Iterable[bytes]:
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")

        def respond(status: int, payload: dict[str, Any]) -> Iterable[bytes]:
            body = json.dumps(payload).encode("utf-8")
            start_response(
                f"{status} {_reason(status)}",
                [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                    # A webhook endpoint has no business being framed, sniffed
                    # or cached.
                    ("X-Content-Type-Options", "nosniff"),
                    ("Cache-Control", "no-store"),
                ],
            )
            return [body]

        if path == HEALTH_PATH and method == "GET":
            return respond(
                200,
                {
                    "status": "ok",
                    "accepted": resolved.accepted_count,
                    "journal_records": resolved.verify_chain(),
                },
            )

        if path == RECENT_PATH and method == "GET":
            return respond(200, {"events": resolved.recent(20)})

        if path != WEBHOOK_PATH:
            return respond(404, {"status": "not_found"})

        if method != "POST":
            return respond(405, {"status": "method_not_allowed"})

        raw = _read_body(environ, resolved.config.max_body_bytes + 1)
        receipt: Receipt = resolved.handle(raw, _headers_from_environ(environ))
        return respond(receipt.status, {"status": receipt.outcome})

    return application


def _reason(status: int) -> str:
    return {
        200: "OK",
        400: "Bad Request",
        401: "Unauthorized",
        404: "Not Found",
        405: "Method Not Allowed",
        500: "Internal Server Error",
    }.get(status, "Error")


#: Module-level callable for `gunicorn mandate_recovery.ingest.wsgi:application`.
#: Constructed lazily so importing this module in a test does not demand a
#: secret in the environment.
class _Lazy:
    def __init__(self) -> None:
        self._app: Callable | None = None

    def __call__(self, environ: dict[str, Any], start_response: Callable):
        if self._app is None:
            self._app = make_application()
        return self._app(environ, start_response)


application = _Lazy()


def main() -> int:
    from wsgiref.simple_server import make_server

    port = int(os.environ.get("ANTE_PORT", "8080"))
    app = make_application()
    print(f"listening on http://0.0.0.0:{port}{WEBHOOK_PATH}")
    print("health:  http://127.0.0.1:%d%s" % (port, HEALTH_PATH))
    with make_server("0.0.0.0", port, app) as httpd:
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
