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

from ..constraints import is_permitted
from ..constraints.rules import RULES, RuleKind
from ..core.clock import SLOTS_PER_DAY, is_non_peak
from ..core.money import Paise, rupees
from ..core.types import (
    Action,
    Commit,
    MandateState,
    MandateStatus,
    NotifyOnly,
    PDN,
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
    alive_at_end: bool = True
    revoked: bool = False
    doom: str = "NONE"


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
) -> RunMetrics:
    """Run one policy against one world. The world is consumed; build a fresh one per policy."""
    policy.reset(world.seed)
    metrics = RunMetrics(policy=policy.name, seed=world.seed)

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
            and m.status is MandateStatus.LIVE
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
            elif isinstance(action, Stop):
                stopped.add(cand.mandate_id)
                out = metrics.per_mandate[cand.mandate_id]
                out.stopped_reason = action.reason
                metrics.stops += 1
                metrics.stopped_value_paise += by_id[cand.mandate_id].amount_due - out.recovered

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


def _next_non_peak(world: World, slot: int) -> int:
    for candidate in range(slot, min(slot + 2 * SLOTS_PER_DAY, world.horizon_slots)):
        if is_non_peak(world.time_of(candidate)):
            return candidate
    return slot
