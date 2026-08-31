"""The simulation loop.

The experiment is defined so that it measures the thing the track asks for —
*money recovered* — rather than total collection, which would mostly measure how
many debits happened to clear on their due date:

1. Every mandate's original execution fires on its due date. That is the
   merchant's scheduled debit, not the agent's decision.
2. The failures become the recovery batch.
3. From there the agent has at most three further presentations per mandate,
   under every constraint in `COMPLIANCE.md`.
4. Recovered rupees are what the agent collected from that batch. The original
   successes are excluded — they were never at risk.

Every proposal passes through `is_permitted`. Actions the constraint layer
refuses are **recorded and not executed**, which is how a policy written for
another market gets measured here without being quietly given a pass: it loses
both on legality and on collection, because most of what it wants to do is not
available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Mapping

from pathlib import Path

from ..act.executor import (
    BlastRadius,
    DecisionContext,
    ExecutionMode,
    Executor,
)
from ..act.journal import Journal
from ..constraints import is_permitted
from ..constraints.rules import RULES, RuleKind
from ..core.clock import SLOTS_PER_DAY, is_non_peak
from ..core.money import Paise, fmt, rupees
from ..core.types import (
    Action,
    Commit,
    EscalateHuman,
    MandateState,
    MandateStatus,
    NotifyOnly,
    PDN,
    RequestAFA,
    RequestRemandate,
    Stop,
    Wait,
)
from ..sim.world import Doom, MandateTruth, World
from .policy import Candidate, Policy

#: A modelling choice, stated rather than buried. The per-presentation cost
#: stands for the PSP fee plus the operational load of an attempt; the contact
#: cost stands for the customer's patience, which is finite and is the resource
#: an unconstrained agent would spend first.
COST_PER_PRESENTATION: Final[Paise] = rupees(2)
COST_PER_CONTACT: Final[Paise] = rupees(0.5)

#: Decide every four hours. The notification aperture is 24–48h wide, so a finer
#: epoch buys nothing but time.
EPOCH_SLOTS: Final[int] = 8

#: Minimum and maximum lead in slots, from COMPLIANCE.md C5.
MIN_LEAD_SLOTS: Final[int] = 48
MAX_LEAD_SLOTS: Final[int] = 96


@dataclass(slots=True)
class MandateOutcome:
    mandate_id: str
    amount_due: Paise
    recovered: Paise = 0
    attempts: int = 0
    contacts: int = 0
    stopped_reason: str | None = None
    terminal_action: str = "Stop"
    alive_at_end: bool = True
    revoked: bool = False
    doom: str = "NONE"


@dataclass(slots=True)
class StopRecord:
    """A refusal, and what it actually cost.

    `recoverable_paise` comes from the simulator's ground truth and is computed
    *after* the run: the best a clairvoyant could have collected from the slots
    that remained. It is never visible to the policy. It exists so the stop list
    can be scored rather than merely listed — a refusal of a mandate that would
    never have paid is correct, and one of a mandate that would have paid is the
    price of caution.
    """

    mandate_id: str
    action: str
    reason: str
    outstanding_paise: Paise
    recoverable_paise: Paise
    doom: str

    @property
    def was_right(self) -> bool:
        return self.recoverable_paise == 0


@dataclass(slots=True)
class RunMetrics:
    policy: str
    seed: int

    batch_size: int = 0
    batch_value_paise: Paise = 0

    recovered_paise: Paise = 0
    presentations: int = 0
    contacts: int = 0

    mandates_recovered: int = 0
    mandates_alive_at_end: int = 0
    mandates_revoked: int = 0

    stops: int = 0
    stopped_value_paise: Paise = 0
    #: Escalations by kind. The brief asks for compliant *escalation*, not
    #: only for stopping: a mandate above its AFA ceiling needs
    #: authentication rather than abandonment.
    escalations: dict[str, int] = field(default_factory=dict)
    #: One row per refusal, scored against ground truth after the run.
    stop_ledger: list[StopRecord] = field(default_factory=list)
    #: Mandates the policy was never offered — already revoked or expired when
    #: the first epoch ran, or the cycle closed before the notification aperture
    #: could open on them. They are not refusals, and folding them into the stop
    #: list would credit a policy with a judgement it never made.
    unactionable: int = 0
    unactionable_value_paise: Paise = 0

    #: Actions the constraint layer refused, by rule id. Non-zero is not a bug in
    #: the harness — it is a measurement of the policy.
    violations: dict[str, int] = field(default_factory=dict)
    #: Mandates on which at least one illegal action was proposed. Reported
    #: alongside the raw count because a policy that repeats one bad idea every
    #: epoch is not fifty times worse than one that has it once — the raw count
    #: measures the decision cadence as much as the policy.
    violating_mandates: set[str] = field(default_factory=set)

    per_mandate: dict[str, MandateOutcome] = field(default_factory=dict)

    #: Set when the run was driven through the audited money path. Every
    #: presentation then has a hash-chained receipt and the run replays.
    journal_path: str | None = None
    journal_records: int = 0

    # -- derived -----------------------------------------------------------

    @property
    def cost_paise(self) -> Paise:
        return self.presentations * COST_PER_PRESENTATION + self.contacts * COST_PER_CONTACT

    @property
    def net_value_paise(self) -> Paise:
        return self.recovered_paise - self.cost_paise

    @property
    def recovery_rate(self) -> float:
        return self.recovered_paise / self.batch_value_paise if self.batch_value_paise else 0.0

    @property
    def slot_efficiency_paise(self) -> float:
        """Rupees recovered per presentation spent — the allocation thesis, measured."""
        return self.recovered_paise / self.presentations if self.presentations else 0.0

    @property
    def regulatory_violations(self) -> int:
        return sum(
            n for rule, n in self.violations.items()
            if RULES.get(rule, (RuleKind.OPERATIONAL,))[0] is RuleKind.REGULATORY
        )

    @property
    def survival_rate(self) -> float:
        """Mandates that were not revoked — the option-value metric.

        Deliberately not "still LIVE": a fully collected mandate moves to
        COMPLETED, and counting only LIVE made the best-performing policy look
        like the one destroying the most mandates.
        """
        if not self.batch_size:
            return 0.0
        return (self.batch_size - self.mandates_revoked) / self.batch_size


def run_policy(
    policy: Policy,
    world: World,
    *,
    enforce: bool = True,
    audit_dir: "Path | str | None" = None,
) -> RunMetrics:
    """Run one policy against one world. The world is consumed; build a fresh one per policy.

    With `audit_dir`, every presentation goes through the real `Executor`:
    intent fsynced to a hash-chained journal before the effect, outcome fsynced
    after, each decision addressable by its idempotency key, and the whole run
    reconstructible with `--replay`. Without it the same decisions are taken
    against the simulator directly, which is faster for the test suite.

    The audited and unaudited paths must produce identical numbers. The audit
    layer records the run; it does not alter it, and there is a test asserting
    exactly that.
    """
    policy.reset(world.seed)
    metrics = RunMetrics(policy=policy.name, seed=world.seed)

    audit: Executor | None = None
    gateway = None
    if audit_dir is not None:
        from .gateway import WorldGateway

        journal = Journal(Path(audit_dir) / f"{world.seed}-{_slug(policy.name)}.jsonl")
        gateway = WorldGateway(world)
        audit = Executor(
            journal,
            gateway,
            now=lambda: world.origin,
            mode=ExecutionMode.LIVE,
            blast_radius=BlastRadius.unlimited(),
        )
        audit.recover()
        audit.begin_run(f"{policy.name}::{world.seed}")
        metrics.journal_path = str(journal.path)

    # -- 1. the original execution, which is not the agent's decision -------
    batch: list[MandateTruth] = []
    for m in world.mandates:
        slot = _next_non_peak(world, m.due_slot)
        result = world.present(m.mandate_id, world.time_of(slot), m.amount_due)
        if not result.ok:
            batch.append(m)
            metrics.per_mandate[m.mandate_id] = MandateOutcome(
                mandate_id=m.mandate_id,
                amount_due=m.amount_due,
                attempts=1,
                doom=m.doom.value,
            )

    metrics.batch_size = len(batch)
    metrics.batch_value_paise = sum(m.amount_due for m in batch)
    if not batch:
        return metrics

    by_id = {m.mandate_id: m for m in batch}
    failure_slot = {m.mandate_id: _next_non_peak(world, m.due_slot) for m in batch}
    pending: dict[str, tuple[int, Paise]] = {}
    stopped: set[str] = set()
    ever_offered: set[str] = set()

    start = min(failure_slot.values())
    end = min(world.horizon_slots - 1, max(m.cycle_end_slot for m in batch))

    # -- 2. walk the clock -------------------------------------------------
    for slot in range(start, end + 1):
        now = world.time_of(slot)

        # 2a. fire commitments that come due
        for mid, (exec_slot, amount) in list(pending.items()):
            if exec_slot != slot:
                continue
            del pending[mid]
            if audit is not None and gateway is not None:
                # `attempts_used` increments inside the presentation, so the key
                # must come back from the executor rather than be recomputed
                # afterwards from state that has already moved.
                executed = audit.present(
                    mid,
                    None,
                    now,
                    amount,
                    DecisionContext(
                        mandate_id=mid,
                        cycle_id="2026-09",
                        attempt_index=by_id[mid].attempts_used,
                        justification=f"presenting {fmt(amount)} at the committed slot",
                        policy_version=policy.name,
                    ),
                )
                result = gateway.outcomes[executed.idem_key]
            else:
                result = world.present(mid, now, amount)
            # Policies may learn from their own outcomes. Optional, so a policy
            # that does not care never has to know the hook exists.
            observe = getattr(policy, "observe", None)
            if observe is not None:
                observe(mid, now, amount, result.ok)
            out = metrics.per_mandate[mid]
            out.attempts += 1
            metrics.presentations += 1
            if result.ok:
                out.recovered += result.collected_paise
                metrics.recovered_paise += result.collected_paise
            else:
                failure_slot[mid] = slot

        # 2b. decision epoch
        if (slot - start) % EPOCH_SLOTS:
            continue

        # Deliberately not filtered to LIVE mandates.
        #
        # A revoked or lapsed mandate is still sitting in the merchant's book
        # with money outstanding, and deciding what to do about it — request
        # re-registration, escalate to a human, or write it off — is a real
        # decision the agent should be made to take. Filtering them out meant
        # they were silently counted as "unactionable" and the escalation ladder
        # could never fire, which is exactly the clause the brief asks for.
        #
        # Nothing unsafe follows: the constraint layer vetoes any debit against
        # them (C12, RATCHET), so the only actions available are escalations,
        # and a mandate leaves the pool as soon as one is taken.
        candidates = [
            Candidate(
                mandate_id=m.mandate_id,
                state=_state_with_pending(world, m, pending.get(m.mandate_id), slot),
                last_failure_slot=failure_slot[m.mandate_id],
                now_slot=slot,
            )
            for m in batch
            if m.mandate_id not in stopped
            and m.mandate_id not in pending
            and m.collected < m.amount_due
            and m.attempts_used < 4
            and slot + MIN_LEAD_SLOTS <= m.cycle_end_slot
        ]
        if not candidates:
            if not pending:
                break
            continue

        ever_offered.update(c.mandate_id for c in candidates)
        proposals = policy.plan(candidates, now)
        for cand in candidates:
            action = proposals.get(cand.mandate_id)
            if action is None or isinstance(action, Wait):
                continue

            verdict = is_permitted(action, cand.state, now)
            if not verdict.allowed:
                rule = getattr(verdict, "rule_id", "?")
                metrics.violations[rule] = metrics.violations.get(rule, 0) + 1
                metrics.violating_mandates.add(cand.mandate_id)
                if enforce:
                    continue

            if isinstance(action, Commit):
                pending[cand.mandate_id] = (
                    world.slot_of(action.execute_at),
                    action.amount_paise,
                )
            elif isinstance(action, NotifyOnly):
                world.notify(cand.mandate_id, action.at)
                metrics.contacts += 1
                metrics.per_mandate[cand.mandate_id].contacts += 1
            elif isinstance(action, (Stop, RequestAFA, RequestRemandate, EscalateHuman)):
                stopped.add(cand.mandate_id)
                out = metrics.per_mandate[cand.mandate_id]
                kind = type(action).__name__
                out.terminal_action = kind
                out.stopped_reason = (
                    getattr(action, "reason", None)
                    or getattr(action, "summary", None)
                    or kind
                )
                if isinstance(action, Stop):
                    metrics.stops += 1
                else:
                    metrics.escalations[kind] = metrics.escalations.get(kind, 0) + 1
                metrics.stopped_value_paise += (
                    by_id[cand.mandate_id].amount_due - out.recovered
                )

    # -- the stop ledger, scored against ground truth ----------------------
    for mid in sorted(stopped):
        truth = by_id[mid]
        out = metrics.per_mandate[mid]
        outstanding = truth.amount_due - truth.collected
        metrics.stop_ledger.append(
            StopRecord(
                mandate_id=mid,
                action=out.terminal_action,
                reason=out.stopped_reason or "",
                outstanding_paise=outstanding,
                recoverable_paise=_best_achievable(world, truth, outstanding),
                doom=truth.doom.value,
            )
        )

    if audit is not None:
        audit.end_run("completed")
        metrics.journal_records = audit.journal.verify()

    # -- 3. tally ----------------------------------------------------------
    for m in batch:
        if m.mandate_id not in ever_offered:
            metrics.unactionable += 1
            metrics.unactionable_value_paise += m.amount_due
        out = metrics.per_mandate[m.mandate_id]
        out.alive_at_end = m.status is MandateStatus.LIVE
        out.revoked = m.status is MandateStatus.REVOKED
        if m.collected >= m.amount_due:
            metrics.mandates_recovered += 1
        if out.alive_at_end:
            metrics.mandates_alive_at_end += 1
        if out.revoked:
            metrics.mandates_revoked += 1

    return metrics


def _state_with_pending(
    world: World, m: MandateTruth, pend: tuple[int, Paise] | None, slot: int
) -> MandateState:
    """The agent's view, with any in-flight commitment attached.

    Attaching it is what makes C8 bind through the constraint layer rather than
    through harness bookkeeping — the serialization constraint is enforced by
    the same code that will enforce it in production.
    """
    state = world.observable(m)
    if pend is None:
        return state
    exec_slot, amount = pend
    return state.with_(
        pending_pdn=PDN(
            notified_at=world.time_of(slot),
            execute_at=world.time_of(exec_slot),
            amount_paise=amount,
        )
    )


def _best_achievable(world: World, truth: MandateTruth, outstanding: Paise) -> Paise:
    """What a clairvoyant could still have collected from this mandate.

    Ground truth, used only after the run to score refusals. The stop list is
    only defensible if the cost of caution is measured rather than assumed to
    be zero.
    """
    # A dead mandate collects nothing however much money is in the account.
    # An earlier version scored only the balance and therefore reported regret
    # on closed accounts — making a correct refusal look like a costly one and
    # the stop list look far worse than it was.
    if outstanding <= 0:
        return 0
    if truth.doom in (Doom.ACCOUNT_CLOSED, Doom.ALREADY_REVOKED, Doom.VALIDITY_LAPSED):
        return 0
    if truth.status in (MandateStatus.REVOKED, MandateStatus.EXPIRED):
        return 0
    lo = min(truth.due_slot + 48, world.horizon_slots - 1)
    hi = min(truth.cycle_end_slot, truth.validity_end_slot, world.horizon_slots - 1)
    best = 0
    for slot in range(lo, hi + 1, 4):
        if not is_non_peak(world.time_of(slot)):
            continue
        balance = world.balance_at(truth.customer, slot)
        take = (
            min(outstanding, balance)
            if truth.variable_amount_allowed
            else (outstanding if balance >= outstanding else 0)
        )
        if take > best:
            best = take
            if best >= outstanding:
                break
    return int(best)


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name)[:48].strip("-")


def _next_non_peak(world: World, slot: int) -> int:
    for candidate in range(slot, min(slot + 2 * SLOTS_PER_DAY, world.horizon_slots)):
        if is_non_peak(world.time_of(candidate)):
            return candidate
    return slot
