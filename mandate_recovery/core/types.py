"""Domain model: mandate state, the action space, and the cause taxonomy.

Everything here is frozen. The policy proposes actions; nothing mutates a
MandateState in place. State transitions produce new states, which is what makes
the audit log replayable (BUILD-SPEC.md §5.6).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Final, Union

from .money import Paise, rupees


class MandateStatus(Enum):
    LIVE = "LIVE"
    PAUSED = "PAUSED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"
    COMPLETED = "COMPLETED"


class CauseClass(Enum):
    """Diagnosis output. Cause determines everything downstream (BUILD-SPEC §5.2)."""

    TRANSIENT_ISSUER = "TRANSIENT_ISSUER"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    LIMIT_BREACH = "LIMIT_BREACH"
    PDN_MISSING = "PDN_MISSING"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    AFA_REQUIRED = "AFA_REQUIRED"
    VPA_INVALID = "VPA_INVALID"
    TERMINAL = "TERMINAL"
    UNKNOWN = "UNKNOWN"


#: Causes for which a debit retry can never be legitimate. The diagnosis layer's
#: one-way ratchet may move a cause *into* this set but never out of it.
TERMINAL_CAUSES: Final[frozenset[CauseClass]] = frozenset(
    {
        CauseClass.MANDATE_EXPIRED,
        CauseClass.MANDATE_REVOKED,
        CauseClass.AFA_REQUIRED,
        CauseClass.VPA_INVALID,
        CauseClass.TERMINAL,
    }
)


class Category(Enum):
    """Determines the AFA-free per-transaction ceiling (C15/C16)."""

    STANDARD = "STANDARD"
    INSURANCE = "INSURANCE"
    MF_SIP = "MF_SIP"
    CC_BILL = "CC_BILL"


#: C15: ₹15,000 default. C16: ₹1,00,000 for insurance premiums, mutual-fund SIPs
#: and credit-card bill payments.
AFA_FREE_CEILING: Final[dict[Category, Paise]] = {
    Category.STANDARD: rupees(15_000),
    Category.INSURANCE: rupees(1_00_000),
    Category.MF_SIP: rupees(1_00_000),
    Category.CC_BILL: rupees(1_00_000),
}


@dataclass(frozen=True, slots=True)
class PDN:
    """A pre-debit notification: one irrevocable commitment to debit.

    At most one may be pending per mandate at any instant (C8) — issuing a new one
    cancels the previous. `execute_at` is fixed at notification time and cannot be
    moved, which is why committing is a real decision rather than a schedule entry.
    """

    notified_at: datetime
    execute_at: datetime
    amount_paise: Paise
    sequence_id: str | None = None
    accepted: bool = False


@dataclass(frozen=True, slots=True)
class MandateState:
    """Everything the agent knows about one mandate at a decision epoch.

    Deliberately excludes the customer's balance. The agent never sees it; it
    infers a posterior over liquidity type from its own observations (§5.3).
    """

    mandate_id: str
    status: MandateStatus
    cause: CauseClass
    attempts_used: int
    is_first_presentation: bool
    amount_due_paise: Paise
    max_amount_paise: Paise
    category: Category
    cycle_end: datetime
    validity_end: datetime
    pending_pdn: PDN | None
    contacts_used: int
    issuer_id: str
    variable_amount_allowed: bool = False

    def with_(self, **changes: object) -> "MandateState":
        return replace(self, **changes)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Action space (BUILD-SPEC.md §2.2)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Commit:
    """Raise a PDN now for an execution at `execute_at` of `amount_paise`.

    The only action that can move money, and the only one that consumes a retry
    slot. Both the time and the amount are decision variables.
    """

    execute_at: datetime
    amount_paise: Paise


@dataclass(frozen=True, slots=True)
class CancelPending:
    """Withdraw the in-flight commitment, freeing the mandate to be re-planned.

    Explicit rather than implicit so that C8's cancel-on-replace shows up in the
    audit log as a decision that was taken, with its cost.
    """


@dataclass(frozen=True, slots=True)
class NotifyOnly:
    """A dunning contact that is not a debit. Costs a contact, not a retry slot."""

    at: datetime
    template_id: str


@dataclass(frozen=True, slots=True)
class RequestAFA:
    """Escalate to customer authentication — above ceiling, or AFA_REQUIRED."""


@dataclass(frozen=True, slots=True)
class RequestRemandate:
    """Mandate is dead. Ask the customer to re-register."""


@dataclass(frozen=True, slots=True)
class EscalateHuman:
    """Hand to a collections agent with a generated summary."""

    summary: str = ""


@dataclass(frozen=True, slots=True)
class Stop:
    """Refuse to spend anything further this cycle. Always carries a reason."""

    reason: str


@dataclass(frozen=True, slots=True)
class Wait:
    """Hold the aperture open and buy information.

    An explicit action, not an absence of one: under the two-sided PDN window (C5)
    the reachable set of execution times moves every slot, so declining to commit
    today has a real cost and belongs in the audit log.
    """


Action = Union[
    Commit,
    CancelPending,
    NotifyOnly,
    RequestAFA,
    RequestRemandate,
    EscalateHuman,
    Stop,
    Wait,
]
