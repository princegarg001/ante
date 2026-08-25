"""The money path.

Everything upstream of this package can be wrong and the damage is a bad
decision. Everything here can be wrong and the damage is a double debit against
a real customer.

The guarantees, and where each one lives:

    journal.py    intent is durable before any side effect; the log is
                  hash-chained, and a process killed mid-write recovers by
                  truncating a torn tail rather than by refusing to start
    gateway.py    the effect boundary. Every effect is addressable by its
                  idempotency key, which is what makes crash recovery possible
                  without guessing
    executor.py   two-phase commit, idempotency, blast-radius ceilings, kill
                  switch, dry-run by default
    replay.py     reconstruct any run from the journal, and reconcile the
                  in-doubt window against the gateway
"""

from .executor import (
    BlastRadius,
    CeilingExceeded,
    ExecutionMode,
    Executor,
    KillSwitch,
)
from .gateway import (
    DryRunGateway,
    FakeGateway,
    GatewayResult,
    PaymentGateway,
)
from .journal import (
    Journal,
    Record,
    RecordKind,
    TamperError,
)

__all__ = [
    "BlastRadius",
    "CeilingExceeded",
    "DryRunGateway",
    "ExecutionMode",
    "Executor",
    "FakeGateway",
    "GatewayResult",
    "Journal",
    "KillSwitch",
    "PaymentGateway",
    "Record",
    "RecordKind",
    "TamperError",
]
