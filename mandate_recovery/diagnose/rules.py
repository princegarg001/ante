"""The deterministic rule floor.

A lookup table from the codes the rails actually emit to the cause classes the
policy reasons about, plus a confidence. Nothing clever, and that is the point:
this is the layer that must never be wrong in the dangerous direction.

## The one-way ratchet

The rule is stated once and enforced structurally:

> A diagnosis may move a cause **into** `TERMINAL_CAUSES`. Nothing may move one
> out.

Being wrong in the recoverable direction wastes an attempt. Being wrong in the
terminal direction means retrying a mandate the customer has cancelled, which is
not a mistake but an abuse. The two errors are not symmetric and the code should
not treat them as if they were.

`apply_ratchet` is what a language-model adjudicator would be routed through if
one were added: it can lower a cause's recoverability, never raise it. The
adjudicator is cut for time; the ratchet it would need is here and tested.

## Ambiguity is real

Not every failure arrives with a clean code. Banks return generic technical
errors and free text, and roughly one failure in twelve cannot be classified
with confidence from the code alone. Those resolve to `UNKNOWN` at low
confidence, and the policy has to decide what to do with a failure it does not
understand — which is the honest situation rather than a convenient one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Iterable, Mapping

from ..core.types import TERMINAL_CAUSES, CauseClass

#: Error code -> (cause, confidence). Codes are those the rails emit; see
#: Razorpay's UPI error documentation and the NPCI response codes.
RULE_TABLE: Final[dict[str, tuple[CauseClass, float]]] = {
    # Balance. The dominant failure mode in this market.
    "insufficient_funds": (CauseClass.INSUFFICIENT_FUNDS, 0.99),
    "Z9": (CauseClass.INSUFFICIENT_FUNDS, 0.98),
    "U69": (CauseClass.INSUFFICIENT_FUNDS, 0.95),
    # Issuer-side, transient.
    "bank_technical_error": (CauseClass.TRANSIENT_ISSUER, 0.96),
    "gateway_technical_error": (CauseClass.TRANSIENT_ISSUER, 0.94),
    "U30": (CauseClass.TRANSIENT_ISSUER, 0.93),
    "U16": (CauseClass.TRANSIENT_ISSUER, 0.90),
    "payment_timeout": (CauseClass.TRANSIENT_ISSUER, 0.88),
    # Limits.
    "limit_exceeded": (CauseClass.LIMIT_BREACH, 0.97),
    "per_transaction_limit_exceeded": (CauseClass.LIMIT_BREACH, 0.97),
    # Our own compliance failure, which deserves its own class precisely
    # because it is self-inflicted.
    "PRE_DEBIT_NOTIFICATION_NOT_FOUND": (CauseClass.PDN_MISSING, 0.99),
    "PRE_DEBIT_NOTIFICATION_NOT_SENT": (CauseClass.PDN_MISSING, 0.99),
    # Mandate lifecycle — terminal.
    "mandate_revoked": (CauseClass.MANDATE_REVOKED, 0.99),
    "mandate_cancelled": (CauseClass.MANDATE_REVOKED, 0.99),
    "mandate_expired": (CauseClass.MANDATE_EXPIRED, 0.99),
    "mandate_not_live": (CauseClass.MANDATE_REVOKED, 0.90),
    # Customer action required — terminal for a retry.
    "afa_required": (CauseClass.AFA_REQUIRED, 0.98),
    "invalid_vpa": (CauseClass.VPA_INVALID, 0.98),
    "vpa_resolution_failed": (CauseClass.VPA_INVALID, 0.95),
    # Account is gone.
    "account_closed": (CauseClass.TERMINAL, 0.99),
    "account_frozen": (CauseClass.TERMINAL, 0.99),
    "account_blocked": (CauseClass.TERMINAL, 0.97),
}

#: Codes that genuinely do not determine a cause. A bank saying "technical
#: decline" has told you almost nothing, and pretending otherwise would be the
#: kind of confident nonsense this layer exists to avoid.
AMBIGUOUS_CODES: Final[frozenset[str]] = frozenset(
    {
        "bank_declined",
        "technical_decline",
        "transaction_declined",
        "unspecified_failure",
        "U00",
    }
)

#: Below this the policy should treat the cause as not established.
CONFIDENT: Final[float] = 0.80


@dataclass(frozen=True, slots=True)
class Diagnosis:
    cause: CauseClass
    confidence: float
    code: str | None
    rule: str

    @property
    def is_confident(self) -> bool:
        return self.confidence >= CONFIDENT

    @property
    def is_terminal(self) -> bool:
        return self.cause in TERMINAL_CAUSES


def diagnose(error_code: str | None, description: str | None = None) -> Diagnosis:
    """Classify one failure from what the rails returned.

    Unknown and ambiguous codes resolve to `UNKNOWN` at low confidence rather
    than to a guess. A policy that receives `UNKNOWN` has been told the truth:
    the cause is not established.
    """
    if error_code is None:
        return Diagnosis(CauseClass.UNKNOWN, 0.0, None, "no-code")

    if error_code in AMBIGUOUS_CODES:
        return Diagnosis(CauseClass.UNKNOWN, 0.35, error_code, "ambiguous")

    hit = RULE_TABLE.get(error_code)
    if hit is not None:
        cause, confidence = hit
        return Diagnosis(cause, confidence, error_code, "table")

    # An unrecognised code is not a licence to guess. Free text is where an
    # adjudicator would earn its place; the ratchet below is what would contain
    # it.
    lowered = (description or "").lower()
    for needle, cause in (
        ("insufficient", CauseClass.INSUFFICIENT_FUNDS),
        ("balance", CauseClass.INSUFFICIENT_FUNDS),
        ("revoked", CauseClass.MANDATE_REVOKED),
        ("cancelled", CauseClass.MANDATE_REVOKED),
        ("expired", CauseClass.MANDATE_EXPIRED),
        ("closed", CauseClass.TERMINAL),
        ("frozen", CauseClass.TERMINAL),
    ):
        if needle in lowered:
            return Diagnosis(cause, 0.60, error_code, f"text:{needle}")

    return Diagnosis(CauseClass.UNKNOWN, 0.20, error_code, "unrecognised")


def apply_ratchet(rule: Diagnosis, proposed: CauseClass) -> CauseClass:
    """Let a second opinion make the system more cautious, never less.

    The asymmetry is deliberate. Downgrading a terminal cause to a recoverable
    one is how an automated system ends up retrying a mandate the customer has
    cancelled — and that is not a wasted attempt, it is an abuse. Upgrading in
    the other direction merely costs an attempt.
    """
    if rule.is_terminal and rule.is_confident:
        return rule.cause
    if proposed in TERMINAL_CAUSES:
        return proposed
    return rule.cause


def confusion_matrix(
    pairs: Iterable[tuple[CauseClass, CauseClass]],
) -> Mapping[tuple[str, str], int]:
    """(true, inferred) counts, for reporting against simulator ground truth."""
    out: dict[tuple[str, str], int] = {}
    for truth, inferred in pairs:
        key = (truth.value, inferred.value)
        out[key] = out.get(key, 0) + 1
    return out


def accuracy(pairs: Iterable[tuple[CauseClass, CauseClass]]) -> float:
    items = list(pairs)
    if not items:
        return float("nan")
    return sum(1 for t, i in items if t is i) / len(items)


def dangerous_errors(pairs: Iterable[tuple[CauseClass, CauseClass]]) -> int:
    """Terminal truth classified as recoverable — the error that matters.

    Counted separately from overall accuracy because it is the only kind of
    mistake here that produces an abusive action rather than a wasted one.
    """
    return sum(
        1
        for truth, inferred in pairs
        if truth in TERMINAL_CAUSES and inferred not in TERMINAL_CAUSES
    )
