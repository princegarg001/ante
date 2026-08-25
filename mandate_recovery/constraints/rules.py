"""`is_permitted(action, state, clock) -> Allow | Veto`.

A pure function. Given the same three arguments it returns the same verdict
forever — no wall-clock reads, no randomness, no network, no mutation. That is
what makes the exhaustive model check in `modelcheck.py` a proof rather than a
sampling exercise.

Two rule kinds, kept separate on purpose:

  REGULATORY  vetoes cite COMPLIANCE.md C1-C24. A violation here is illegal.
  OPERATIONAL vetoes are merchant policy and blast-radius control. A violation
              here is merely expensive.

The headline compliance claim is about REGULATORY only, so it must not be
possible to inflate it by counting operational guards. Keep the split honest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Callable, Final

from ..core.clock import IST, is_non_peak, is_slot_aligned, to_ist
from ..core.money import Paise, fmt
from ..core.types import (
    AFA_FREE_CEILING,
    TERMINAL_CAUSES,
    Action,
    CancelPending,
    Commit,
    EscalateHuman,
    MandateState,
    MandateStatus,
    NotifyOnly,
    RequestAFA,
    RequestRemandate,
    Stop,
    Wait,
)

# --------------------------------------------------------------------------- #
# Regulatory constants. Every one of these traces to a row in COMPLIANCE.md.
# --------------------------------------------------------------------------- #

#: C1 — one original execution plus three retries per mandate per cycle.
MAX_ATTEMPTS: Final[int] = 4

#: C5 — the pre-debit notification aperture, [T-48h, T-24h].
PDN_MIN_LEAD: Final[timedelta] = timedelta(hours=24)
PDN_MAX_LEAD: Final[timedelta] = timedelta(hours=48)

#: C7 — a PDN raised at or after 23:50 IST is rejected for a T+1 execution.
PDN_LATE_CUTOFF: Final[time] = time(23, 50)

#: Operational default: how many times the agent may contact one customer per cycle.
#: Independent of the retry cap, and the guard against the agent discovering that
#: spamming notifications raises recovery.
DEFAULT_CONTACT_CAP: Final[int] = 3


class RuleKind(Enum):
    REGULATORY = "REGULATORY"
    OPERATIONAL = "OPERATIONAL"


@dataclass(frozen=True, slots=True)
class Allow:
    allowed: bool = True

    def __bool__(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class Veto:
    rule_id: str
    reason: str
    kind: RuleKind = RuleKind.REGULATORY
    allowed: bool = False

    def __bool__(self) -> bool:
        return False

    def __str__(self) -> str:
        return f"[{self.rule_id}] {self.reason}"


Verdict = Allow | Veto

ALLOW: Final[Allow] = Allow()

#: rule_id -> (kind, one-line description, source). Rendered into audit entries and
#: into the compliance slide, so it is the registry the pitch reads from.
RULES: Final[dict[str, tuple[RuleKind, str, str]]] = {
    "C1": (RuleKind.REGULATORY, "1 execution + 3 retries per mandate per cycle", "NPCI UPI/API Guidelines 2025"),
    "C2": (RuleKind.REGULATORY, "No execution during peak hours 10:00-13:00 / 17:00-21:30 IST", "NPCI UPI/API Guidelines 2025"),
    "C5": (RuleKind.REGULATORY, "Pre-debit notification must be raised in [T-48h, T-24h]", "NPCI operating guidelines / PSP docs"),
    "C7": (RuleKind.REGULATORY, "PDN raised at/after 23:50 IST is rejected for a T+1 execution", "PSP docs"),
    "C8": (RuleKind.REGULATORY, "At most one pending PDN per mandate", "PSP docs"),
    "C12": (RuleKind.REGULATORY, "Mandate must be LIVE to raise a PDN", "PSP docs"),
    "C15": (RuleKind.REGULATORY, "Amount above the AFA-free ceiling requires AFA", "RBI E-mandate Framework 2026"),
    "C19": (RuleKind.REGULATORY, "Amount must not exceed the mandate's max_amount", "RBI E-mandate Framework 2026"),
    "C21": (RuleKind.REGULATORY, "Execution must fall within the mandate validity period", "RBI E-mandate Framework 2026"),
    "RATCHET": (RuleKind.REGULATORY, "No debit retry against a terminal cause", "RBI 2026 (revocation) / merchant duty"),
    "OPS-ALIGN": (RuleKind.OPERATIONAL, "Execution must sit on the slot grid", "internal"),
    "OPS-AMT": (RuleKind.OPERATIONAL, "Amount must be positive and not exceed the amount due", "internal"),
    "OPS-PARTIAL": (RuleKind.OPERATIONAL, "Partial collection requires a variable-amount mandate", "internal"),
    "OPS-CYCLE": (RuleKind.OPERATIONAL, "Execution must land before the cycle closes", "internal"),
    "OPS-CONTACT": (RuleKind.OPERATIONAL, "Per-customer contact cap for this cycle", "internal"),
    "OPS-NOPEND": (RuleKind.OPERATIONAL, "Nothing pending to cancel", "internal"),
    "OPS-PAST": (RuleKind.OPERATIONAL, "Action scheduled in the past", "internal"),
}


def _veto(rule_id: str, reason: str) -> Veto:
    kind = RULES[rule_id][0]
    return Veto(rule_id=rule_id, reason=reason, kind=kind)


# --------------------------------------------------------------------------- #
# Rules for Commit — the only action that can move money.
# --------------------------------------------------------------------------- #

Rule = Callable[[Action, MandateState, datetime], Veto | None]


def _r_mandate_live(a: Action, s: MandateState, clock: datetime) -> Veto | None:
    """C12 — a PDN may only be raised against a LIVE mandate."""
    if isinstance(a, Commit) and s.status is not MandateStatus.LIVE:
        return _veto("C12", f"mandate status is {s.status.value}, not LIVE")
    return None


def _r_terminal_ratchet(a: Action, s: MandateState, clock: datetime) -> Veto | None:
    """RATCHET — retrying a debit against a revoked or expired mandate is abusive.

    Structurally one-way: the diagnosis layer may move a cause into
    TERMINAL_CAUSES but nothing may move it out, so this veto can never be
    argued away by a downstream component.
    """
    if isinstance(a, Commit) and s.cause in TERMINAL_CAUSES:
        return _veto("RATCHET", f"cause {s.cause.value} is terminal; a debit retry is not legitimate")
    return None


def _r_retry_cap(a: Action, s: MandateState, clock: datetime) -> Veto | None:
    """C1 — the hard budget. Four presentations, then the cycle is dead."""
    if isinstance(a, Commit) and s.attempts_used >= MAX_ATTEMPTS:
        return _veto("C1", f"retry budget exhausted: {s.attempts_used}/{MAX_ATTEMPTS} attempts used")
    return None


def _r_validity(a: Action, s: MandateState, clock: datetime) -> Veto | None:
    """C21 — execution must fall inside the mandate's validity period."""
    if isinstance(a, Commit) and to_ist(a.execute_at) > to_ist(s.validity_end):
        return _veto("C21", f"execution at {_ts(a.execute_at)} is past mandate validity {_ts(s.validity_end)}")
    return None


def _r_non_peak(a: Action, s: MandateState, clock: datetime) -> Veto | None:
    """C2/C3 — no execution inside an NPCI peak window."""
    if isinstance(a, Commit) and not is_non_peak(a.execute_at):
        return _veto("C2", f"execution at {_ts(a.execute_at)} falls in an NPCI peak window")
    return None


def _r_pdn_aperture(a: Action, s: MandateState, clock: datetime) -> Veto | None:
    """C5 — the two-sided notification window.

    Not `execute_at >= clock + 24h`. The commitment must be raised no earlier than
    48h and no later than 24h before execution, so for any candidate execution
    time there is exactly one 24h-wide interval in which it can be chosen.
    """
    if not isinstance(a, Commit):
        return None
    lead = to_ist(a.execute_at) - to_ist(clock)
    if lead < PDN_MIN_LEAD:
        return _veto("C5", f"lead {_dur(lead)} is under the 24h pre-debit notification minimum")
    if lead > PDN_MAX_LEAD:
        return _veto("C5", f"lead {_dur(lead)} exceeds the 48h pre-debit notification maximum")
    return None


def _r_pdn_late_cutoff(a: Action, s: MandateState, clock: datetime) -> Veto | None:
    """C7 — a PDN raised in the last ten minutes of the day is rejected for T+1."""
    if not isinstance(a, Commit):
        return None
    now_ist, exec_ist = to_ist(clock), to_ist(a.execute_at)
    if now_ist.timetz().replace(tzinfo=None) >= PDN_LATE_CUTOFF:
        if exec_ist.date() == (now_ist + timedelta(days=1)).date():
            return _veto("C7", f"PDN raised at {_ts(clock)} cannot target a T+1 execution")
    return None


def _r_one_pending_pdn(a: Action, s: MandateState, clock: datetime) -> Veto | None:
    """C8 — the serialization constraint, and the reason this is not a knapsack.

    A mandate may hold at most one outstanding commitment. To re-plan, the policy
    must emit an explicit CancelPending first, so the cost of abandoning a
    commitment is visible in the audit log instead of being hidden inside a
    silent overwrite.
    """
    if isinstance(a, Commit) and s.pending_pdn is not None:
        p = s.pending_pdn
        return _veto("C8", f"a PDN for {_ts(p.execute_at)} is already pending; cancel it explicitly first")
    return None


def _r_afa_ceiling(a: Action, s: MandateState, clock: datetime) -> Veto | None:
    """C15/C16 — the AFA-free per-transaction ceiling, by mandate category."""
    if not isinstance(a, Commit):
        return None
    ceiling = AFA_FREE_CEILING[s.category]
    if a.amount_paise > ceiling:
        return _veto(
            "C15",
            f"{fmt(a.amount_paise)} exceeds the AFA-free ceiling {fmt(ceiling)} "
            f"for category {s.category.value}; requires AFA",
        )
    return None


def _r_mandate_cap(a: Action, s: MandateState, clock: datetime) -> Veto | None:
    """C19 — never debit above the cap the customer authorised."""
    if isinstance(a, Commit) and a.amount_paise > s.max_amount_paise:
        return _veto("C19", f"{fmt(a.amount_paise)} exceeds mandate max_amount {fmt(s.max_amount_paise)}")
    return None


# --- operational -------------------------------------------------------------


def _r_slot_aligned(a: Action, s: MandateState, clock: datetime) -> Veto | None:
    if isinstance(a, Commit) and not is_slot_aligned(a.execute_at):
        return _veto("OPS-ALIGN", f"execution at {_ts(a.execute_at)} is not on the 30-minute slot grid")
    return None


def _r_amount_sane(a: Action, s: MandateState, clock: datetime) -> Veto | None:
    if not isinstance(a, Commit):
        return None
    if a.amount_paise <= 0:
        return _veto("OPS-AMT", f"non-positive amount {a.amount_paise}")
    if a.amount_paise > s.amount_due_paise:
        return _veto("OPS-AMT", f"{fmt(a.amount_paise)} exceeds amount due {fmt(s.amount_due_paise)}")
    return None


def _r_partial_allowed(a: Action, s: MandateState, clock: datetime) -> Veto | None:
    """Partial collection is an algorithmic lever, but only where the mandate is a
    variable-amount mandate (C19). Gated per-mandate so results can be reported
    both with and without the lever enabled."""
    if (
        isinstance(a, Commit)
        and a.amount_paise < s.amount_due_paise
        and not s.variable_amount_allowed
    ):
        return _veto("OPS-PARTIAL", "partial collection requires a variable-amount mandate")
    return None


def _r_cycle_end(a: Action, s: MandateState, clock: datetime) -> Veto | None:
    if isinstance(a, Commit) and to_ist(a.execute_at) > to_ist(s.cycle_end):
        return _veto("OPS-CYCLE", f"execution at {_ts(a.execute_at)} is past cycle end {_ts(s.cycle_end)}")
    return None


def _r_contact_cap(a: Action, s: MandateState, clock: datetime) -> Veto | None:
    if isinstance(a, NotifyOnly) and s.contacts_used >= DEFAULT_CONTACT_CAP:
        return _veto("OPS-CONTACT", f"contact cap reached: {s.contacts_used}/{DEFAULT_CONTACT_CAP}")
    return None


def _r_nothing_pending(a: Action, s: MandateState, clock: datetime) -> Veto | None:
    if isinstance(a, CancelPending) and s.pending_pdn is None:
        return _veto("OPS-NOPEND", "no pending PDN to cancel")
    return None


def _r_not_in_past(a: Action, s: MandateState, clock: datetime) -> Veto | None:
    if isinstance(a, NotifyOnly) and to_ist(a.at) < to_ist(clock):
        return _veto("OPS-PAST", f"notification at {_ts(a.at)} is before the decision clock {_ts(clock)}")
    return None


#: Evaluation order. Regulatory rules run first so that when a Commit is illegal
#: for several reasons at once, `is_permitted` reports the regulatory one — the
#: veto that would matter to a regulator, not the one that matters to ops.
REGULATORY_RULES: Final[tuple[Rule, ...]] = (
    _r_mandate_live,
    _r_terminal_ratchet,
    _r_retry_cap,
    _r_validity,
    _r_non_peak,
    _r_pdn_aperture,
    _r_pdn_late_cutoff,
    _r_one_pending_pdn,
    _r_afa_ceiling,
    _r_mandate_cap,
)

OPERATIONAL_RULES: Final[tuple[Rule, ...]] = (
    _r_slot_aligned,
    _r_amount_sane,
    _r_partial_allowed,
    _r_cycle_end,
    _r_contact_cap,
    _r_nothing_pending,
    _r_not_in_past,
)

ALL_RULES: Final[tuple[Rule, ...]] = REGULATORY_RULES + OPERATIONAL_RULES


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #


def is_permitted(action: Action, state: MandateState, clock: datetime) -> Verdict:
    """The gate. Returns Allow, or the first Veto — regulatory rules first.

    Pure: no wall-clock read, no I/O, no mutation of `state`.
    """
    for rule in ALL_RULES:
        veto = rule(action, state, clock)
        if veto is not None:
            return veto
    return ALLOW


def all_vetoes(action: Action, state: MandateState, clock: datetime) -> tuple[Veto, ...]:
    """Every rule that fires, not just the first.

    `is_permitted` is the gate; this is what the audit log records, so that a
    reviewer can see an action was illegal for four reasons rather than one.
    """
    return tuple(v for rule in ALL_RULES if (v := rule(action, state, clock)) is not None)


def permitted_actions(
    candidates: tuple[Action, ...], state: MandateState, clock: datetime
) -> tuple[Action, ...]:
    """Filter a proposal set down to the legal ones. The policy's only way in."""
    return tuple(a for a in candidates if is_permitted(a, state, clock).allowed)


# --------------------------------------------------------------------------- #
# Formatting helpers — presentation only, never used in a decision.
# --------------------------------------------------------------------------- #


def _ts(dt: datetime) -> str:
    return to_ist(dt).strftime("%Y-%m-%d %H:%M IST")


def _dur(td: timedelta) -> str:
    total = int(td.total_seconds())
    sign = "-" if total < 0 else ""
    hours, rem = divmod(abs(total), 3600)
    return f"{sign}{hours}h{rem // 60:02d}m"


__all__ = [
    "ALLOW",
    "ALL_RULES",
    "AFA_FREE_CEILING",
    "Allow",
    "DEFAULT_CONTACT_CAP",
    "MAX_ATTEMPTS",
    "OPERATIONAL_RULES",
    "PDN_LATE_CUTOFF",
    "PDN_MAX_LEAD",
    "PDN_MIN_LEAD",
    "REGULATORY_RULES",
    "RULES",
    "RuleKind",
    "Verdict",
    "Veto",
    "all_vetoes",
    "is_permitted",
    "permitted_actions",
]
