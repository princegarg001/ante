"""Two-phase execution with idempotency, ceilings, and a kill switch.

The ordering is not negotiable, and it is dictated by two constraints working
together. C6 makes the pre-debit notification a prerequisite for presenting a
debit. C8 makes a *second* notification destroy the first. So the sequence is:

    WAL(INTENT) → fsync → effect → WAL(EFFECT) → fsync

and the interesting failure is a crash between the effect and the second write.
The effect happened; the log does not know. That is the **in-doubt window**, and
the only safe way out of it is to ask the gateway what it already did. Retrying
would raise a second notification, cancel the first, and push the execution out
by a day — a compliance-shaped bug that looks like a network blip in the logs.

Three refusals are hard-wired here rather than left to callers:

* an action the constraint layer does not permit is never executed, even if a
  caller asks — the check is repeated at the boundary as defence in depth
* an action that would breach the blast radius stops the run rather than
  emitting a warning
* execution is dry-run unless the caller explicitly says otherwise
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Literal

from ..constraints import is_permitted
from ..core.clock import to_ist
from ..core.money import Paise, fmt
from ..core.types import Commit, MandateState
from .gateway import GatewayResult, PaymentGateway
from .journal import Journal, RecordKind


class ExecutionMode(Enum):
    DRY_RUN = "DRY_RUN"
    LIVE = "LIVE"


class CeilingExceeded(RuntimeError):
    """The run hit its blast radius. Deliberately an exception, not a return
    value — a ceiling that can be ignored by a caller is a warning."""


@dataclass(frozen=True, slots=True)
class BlastRadius:
    """Hard limits on what one run may do, regardless of what the policy wants."""

    max_executions: int = 500
    max_paise_attempted: Paise = 50_00_000  # ₹5,00,000

    @staticmethod
    def unlimited() -> "BlastRadius":
        return BlastRadius(max_executions=10**9, max_paise_attempted=10**15)


class KillSwitch:
    """A flag an operator can set without touching the process.

    File-backed on purpose: halting a misbehaving agent should not require a
    deploy, a restart, or access to the code. Checked before every effect, so
    engaging it stops new work while whatever is in flight completes.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._engaged = False

    def engage(self) -> None:
        self._engaged = True
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("engaged\n", encoding="utf-8")

    def release(self) -> None:
        self._engaged = False
        if self.path is not None and self.path.exists():
            self.path.unlink()

    def is_set(self) -> bool:
        if self._engaged:
            return True
        return self.path is not None and self.path.exists()


Status = Literal[
    "APPLIED", "DUPLICATE", "RECOVERED", "VETOED", "KILLED", "CEILING", "FAILED"
]


@dataclass(frozen=True, slots=True)
class Outcome:
    status: Status
    idem_key: str
    detail: str
    result: GatewayResult | None = None

    @property
    def moved_money(self) -> bool:
        return self.status == "APPLIED"


@dataclass(frozen=True, slots=True)
class DecisionContext:
    """Everything needed to reconstruct *why* a decision was taken.

    Fields the allocator will populate default to None rather than to invented
    values, so a receipt never implies a computation that did not happen.
    """

    mandate_id: str
    cycle_id: str
    attempt_index: int
    justification: str
    policy_version: str = "unset"
    model_version: str = "unset"
    expected_value_paise: int | None = None
    bid_paise: int | None = None
    clearing_price_paise: int | None = None
    belief: tuple[float, ...] | None = None
    constraint_verdicts: tuple[str, ...] = ()

    def to_body(self) -> dict[str, object]:
        return {
            "mandate_id": self.mandate_id,
            "cycle_id": self.cycle_id,
            "attempt_index": self.attempt_index,
            "justification": self.justification,
            "policy_version": self.policy_version,
            "model_version": self.model_version,
            "expected_value_paise": self.expected_value_paise,
            "bid_paise": self.bid_paise,
            "clearing_price_paise": self.clearing_price_paise,
            "belief": list(self.belief) if self.belief is not None else None,
            "constraint_verdicts": list(self.constraint_verdicts),
        }


def idempotency_key(
    mandate_id: str, cycle_id: str, attempt_index: int, action: Commit
) -> str:
    """Identity of a decision, not of an attempt to perform it.

    Deliberately excludes the run id and the wall clock. Two runs that reach the
    same decision produce the same key and the second is a no-op — which is
    exactly what must happen when a crashed run is restarted.
    """
    material = "|".join(
        [
            "pdn",
            mandate_id,
            cycle_id,
            str(attempt_index),
            to_ist(action.execute_at).isoformat(),
            str(action.amount_paise),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class Ledger:
    """Durable state, derived from the journal rather than stored separately.

    A second source of truth would be a second thing to get out of sync, so the
    journal is the only one. `applied` keys are effects the gateway performed —
    successful *or* declined, because a declined presentation still consumed an
    attempt under C1 and must never be repeated.
    """

    applied: dict[str, dict] = field(default_factory=dict)
    #: idem_key -> the run that recorded the intent. Keeping the run id, rather
    #: than just the key, is what lets recovery file the outcome against the run
    #: that meant to do it instead of against a synthetic recovery run — so a
    #: replay of that run shows a resolved intent rather than a dangling one.
    in_doubt: dict[str, str] = field(default_factory=dict)
    executions: dict[str, int] = field(default_factory=dict)
    paise_attempted: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_journal(cls, journal: Journal) -> "Ledger":
        ledger = cls()
        for rec in journal:
            body = rec.body
            if rec.kind is RecordKind.INTENT:
                ledger.in_doubt[body["idem_key"]] = rec.run_id
            elif rec.kind is RecordKind.EFFECT:
                key = body["idem_key"]
                ledger.in_doubt.pop(key, None)
                if body.get("performed"):
                    ledger.applied[key] = body
                    run = rec.run_id
                    ledger.executions[run] = ledger.executions.get(run, 0) + 1
                    ledger.paise_attempted[run] = ledger.paise_attempted.get(
                        run, 0
                    ) + int(body.get("amount_paise", 0))
        return ledger


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    in_doubt_found: int
    adopted: int
    never_performed: int
    torn_bytes_discarded: int

    @property
    def clean(self) -> bool:
        return self.in_doubt_found == 0 and self.torn_bytes_discarded == 0


class Executor:
    """Applies permitted actions durably, exactly once."""

    def __init__(
        self,
        journal: Journal,
        gateway: PaymentGateway,
        now: Callable[[], datetime],
        *,
        mode: ExecutionMode = ExecutionMode.DRY_RUN,
        blast_radius: BlastRadius | None = None,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self.journal = journal
        self.gateway = gateway
        self._now = now
        self.mode = mode
        self.blast_radius = blast_radius or BlastRadius()
        self.kill_switch = kill_switch or KillSwitch()
        self.ledger = Ledger()
        self._run_id: str | None = None

    # -- lifecycle ---------------------------------------------------------

    def recover(self) -> ReconciliationReport:
        """Open the journal and resolve every in-doubt effect. Call before any run.

        Resolution is by interrogation, never by retry. For each intent with no
        recorded outcome, the gateway is asked whether it performed the effect:

        * yes → the outcome is written late and the effect is adopted
        * no  → the intent is closed as never performed, and the decision is
                free to be taken again

        The path that does not exist is "assume it failed and do it again".
        """
        wal_report = self.journal.open()
        self.ledger = Ledger.from_journal(self.journal)

        adopted = never = 0
        for key, origin_run in sorted(self.ledger.in_doubt.items()):
            found = self.gateway.lookup(key)
            ts = self._ts()
            if found is not None:
                self._append_effect(
                    run_id=origin_run,
                    ts=ts,
                    idem_key=key,
                    performed=True,
                    result=found,
                    amount_paise=int(self._intent_amount(key)),
                    resolution="recovered",
                )
                adopted += 1
            else:
                self._append_effect(
                    run_id=origin_run,
                    ts=ts,
                    idem_key=key,
                    performed=False,
                    result=None,
                    amount_paise=0,
                    resolution="not_performed",
                )
                never += 1

        report = ReconciliationReport(
            in_doubt_found=adopted + never,
            adopted=adopted,
            never_performed=never,
            torn_bytes_discarded=wal_report.torn_bytes_discarded,
        )
        self.ledger = Ledger.from_journal(self.journal)
        return report

    def begin_run(self, run_id: str) -> None:
        self._run_id = run_id
        self.journal.append(
            RecordKind.RUN_START,
            run_id,
            self._ts(),
            {
                "mode": self.mode.value,
                "max_executions": self.blast_radius.max_executions,
                "max_paise_attempted": self.blast_radius.max_paise_attempted,
            },
        )

    def end_run(self, reason: str = "completed") -> None:
        run_id = self._require_run()
        self.journal.append(
            RecordKind.RUN_END,
            run_id,
            self._ts(),
            {
                "reason": reason,
                "executions": self.ledger.executions.get(run_id, 0),
                "paise_attempted": self.ledger.paise_attempted.get(run_id, 0),
            },
        )
        self._run_id = None

    # -- the money path ----------------------------------------------------

    def submit(
        self, action: Commit, state: MandateState, clock: datetime, ctx: DecisionContext
    ) -> Outcome:
        """Execute one permitted commit, exactly once, durably."""
        run_id = self._require_run()
        key = idempotency_key(ctx.mandate_id, ctx.cycle_id, ctx.attempt_index, action)

        # 1. The constraint layer is re-consulted at the boundary. The policy is
        #    supposed to have checked; this is defence in depth, and it means a
        #    bug in the policy cannot become an illegal debit.
        verdict = is_permitted(action, state, clock)
        if not verdict.allowed:
            self._skip(run_id, key, "VETOED", f"[{verdict.rule_id}] {verdict.reason}")
            return Outcome("VETOED", key, f"[{verdict.rule_id}] {verdict.reason}")

        # 2. Kill switch, checked before the effect so in-flight work drains.
        if self.kill_switch.is_set():
            self.journal.append(
                RecordKind.KILL, run_id, self._ts(), {"idem_key": key}
            )
            self._skip(run_id, key, "KILLED", "kill switch engaged")
            return Outcome("KILLED", key, "kill switch engaged")

        # 3. Already done. The restart path.
        if key in self.ledger.applied:
            prior = self.ledger.applied[key]
            self._skip(run_id, key, "DUPLICATE", "idempotency key already applied")
            return Outcome(
                "DUPLICATE",
                key,
                f"already applied as {prior.get('external_ref')}",
                GatewayResult(
                    ok=bool(prior.get("ok")),
                    idem_key=key,
                    external_ref=prior.get("external_ref"),
                    sequence_id=prior.get("sequence_id"),
                    replayed=True,
                ),
            )

        # 4. Still in doubt means recover() was not called. Refuse rather than
        #    guess; guessing here is what raises a second notification.
        if key in self.ledger.in_doubt:
            raise RuntimeError(
                f"idem_key {key[:12]} is in doubt — call recover() before running"
            )

        # 5. Blast radius. Stops the run; does not warn.
        self._check_ceilings(run_id, key, action.amount_paise)

        # 6. Intent, durable, before anything happens outside this process.
        self.journal.append(RecordKind.DECISION, run_id, self._ts(), ctx.to_body())
        self.journal.append(
            RecordKind.INTENT,
            run_id,
            self._ts(),
            {
                "idem_key": key,
                "op": "raise_pdn",
                "mandate_id": ctx.mandate_id,
                "execute_at": to_ist(action.execute_at).isoformat(),
                "amount_paise": action.amount_paise,
                "mode": self.mode.value,
            },
        )

        # 7. The effect. Everything from here to step 8 is the in-doubt window.
        try:
            result = self.gateway.raise_pdn(
                key, ctx.mandate_id, action.execute_at, action.amount_paise
            )
        except Exception:
            # Deliberately not writing an EFFECT record. The effect may well have
            # landed, and claiming otherwise would be a lie the log cannot take
            # back. It stays in doubt until recover() asks the gateway.
            raise

        # 8. Outcome, durable.
        self._append_effect(
            run_id=run_id,
            ts=self._ts(),
            idem_key=key,
            performed=True,
            result=result,
            amount_paise=action.amount_paise,
            resolution="fresh",
        )
        self.ledger.applied[key] = {
            "idem_key": key,
            "ok": result.ok,
            "external_ref": result.external_ref,
            "sequence_id": result.sequence_id,
            "amount_paise": action.amount_paise,
        }
        self.ledger.executions[run_id] = self.ledger.executions.get(run_id, 0) + 1
        self.ledger.paise_attempted[run_id] = (
            self.ledger.paise_attempted.get(run_id, 0) + action.amount_paise
        )

        status: Status = "APPLIED" if result.ok else "FAILED"
        return Outcome(status, key, result.error_description or "ok", result)

    # -- internals ---------------------------------------------------------

    def _check_ceilings(self, run_id: str, key: str, amount: Paise) -> None:
        used = self.ledger.executions.get(run_id, 0)
        spent = self.ledger.paise_attempted.get(run_id, 0)
        if used + 1 > self.blast_radius.max_executions:
            self._skip(run_id, key, "CEILING", f"execution cap {used} reached")
            raise CeilingExceeded(
                f"run {run_id}: execution cap {self.blast_radius.max_executions} reached"
            )
        if spent + amount > self.blast_radius.max_paise_attempted:
            self._skip(
                run_id,
                key,
                "CEILING",
                f"value cap would be breached at {fmt(spent + amount)}",
            )
            raise CeilingExceeded(
                f"run {run_id}: attempted value cap "
                f"{fmt(self.blast_radius.max_paise_attempted)} would be breached"
            )

    def _skip(self, run_id: str, key: str, reason: str, detail: str) -> None:
        self.journal.append(
            RecordKind.SKIPPED,
            run_id,
            self._ts(),
            {"idem_key": key, "reason": reason, "detail": detail},
        )

    def _append_effect(
        self,
        *,
        run_id: str,
        ts: str,
        idem_key: str,
        performed: bool,
        result: GatewayResult | None,
        amount_paise: Paise,
        resolution: str,
    ) -> None:
        self.journal.append(
            RecordKind.EFFECT,
            run_id,
            ts,
            {
                "idem_key": idem_key,
                "performed": performed,
                "ok": bool(result.ok) if result else False,
                "external_ref": result.external_ref if result else None,
                "sequence_id": result.sequence_id if result else None,
                "error_code": result.error_code if result else None,
                "amount_paise": amount_paise,
                "resolution": resolution,
            },
        )

    def _intent_amount(self, idem_key: str) -> Paise:
        """The value the original intent recorded.

        An adopted effect has to carry its amount, or the blast-radius counters
        would under-count exactly the spend that a crash made hardest to see.
        """
        for rec in self.journal:
            if rec.kind is RecordKind.INTENT and rec.body.get("idem_key") == idem_key:
                return int(rec.body.get("amount_paise", 0))
        return 0

    def _require_run(self) -> str:
        if self._run_id is None:
            raise RuntimeError("no run in progress — call begin_run() first")
        return self._run_id

    def _ts(self) -> str:
        return to_ist(self._now()).isoformat()
