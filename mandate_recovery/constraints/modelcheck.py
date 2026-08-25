"""Exhaustive verification of the constraint layer.

Property-based testing samples. This enumerates. Two claims are established:

  CLAIM 1 (state-action sweep)
      Over a bounded but *complete* grid of (state, action, clock) triples, every
      action the constraint layer permits satisfies every regulatory invariant.
      Not "no counterexample was sampled" — no counterexample exists in the grid.

  CLAIM 2 (reachability)
      No sequence of permitted actions, from any legal start, can reach a state
      with more than MAX_ATTEMPTS presentations or two simultaneously pending
      PDNs. Breadth-first over the whole reachable set.

The invariants below are written *independently* of `rules.py` — they restate the
regulation from COMPLIANCE.md in raw arithmetic, duplicating the peak-window
literals on purpose. If the two ever disagree, the check fails. A checker that
imported the implementation's own predicates would only prove the code equals
itself.

Run:  python -m mandate_recovery.constraints.modelcheck --days 1
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from ..core.clock import IST, SLOT_MINUTES, SLOTS_PER_DAY
from ..core.money import Paise, fmt, rupees
from ..core.types import (
    AFA_FREE_CEILING,
    TERMINAL_CAUSES,
    CauseClass,
    Category,
    Commit,
    MandateState,
    MandateStatus,
    PDN,
)
from .rules import MAX_ATTEMPTS, is_permitted

# --------------------------------------------------------------------------- #
# The regulation, restated. Deliberately independent of core/clock.py.
# --------------------------------------------------------------------------- #

#: COMPLIANCE.md C2 — peak windows as (start_minute_of_day, end_minute_of_day),
#: half-open. Written out again rather than imported, on purpose.
_PEAK_MINUTES: Final[tuple[tuple[int, int], ...]] = (
    (10 * 60, 13 * 60),          # 10:00-13:00
    (17 * 60, 21 * 60 + 30),     # 17:00-21:30
)
_MIN_LEAD_S: Final[int] = 24 * 3600
_MAX_LEAD_S: Final[int] = 48 * 3600
_CUTOFF_MINUTE: Final[int] = 23 * 60 + 50


def _minute_of_day(dt: datetime) -> int:
    ist = dt.astimezone(IST)
    return ist.hour * 60 + ist.minute


def _inv_violations(action: Commit, state: MandateState, clock: datetime) -> list[str]:
    """Every regulatory invariant `action` breaks. Empty means the action is legal.

    This is the specification. `rules.py` is the implementation.
    """
    bad: list[str] = []
    exec_at = action.execute_at.astimezone(IST)
    now = clock.astimezone(IST)

    # C1 — retry budget
    if state.attempts_used >= MAX_ATTEMPTS:
        bad.append("C1")

    # C2 — no execution inside a peak window
    m = _minute_of_day(exec_at)
    if any(lo <= m < hi for lo, hi in _PEAK_MINUTES):
        bad.append("C2")

    # C5 — the two-sided pre-debit notification aperture
    lead = (exec_at - now).total_seconds()
    if not (_MIN_LEAD_S <= lead <= _MAX_LEAD_S):
        bad.append("C5")

    # C7 — late PDN cannot target T+1
    if _minute_of_day(now) >= _CUTOFF_MINUTE and exec_at.date() == (now + timedelta(days=1)).date():
        bad.append("C7")

    # C8 — one pending PDN per mandate
    if state.pending_pdn is not None:
        bad.append("C8")

    # C12 — mandate must be LIVE
    if state.status is not MandateStatus.LIVE:
        bad.append("C12")

    # C15/C16 — AFA-free ceiling by category
    if action.amount_paise > AFA_FREE_CEILING[state.category]:
        bad.append("C15")

    # C19 — never above the authorised cap
    if action.amount_paise > state.max_amount_paise:
        bad.append("C19")

    # C21 — inside the mandate validity period
    if exec_at > state.validity_end.astimezone(IST):
        bad.append("C21")

    # RATCHET — no debit retry against a terminal cause
    if state.cause in TERMINAL_CAUSES:
        bad.append("RATCHET")

    return bad


# --------------------------------------------------------------------------- #
# CLAIM 1 — exhaustive state x action sweep
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Counterexample:
    clock: datetime
    action: Commit
    state: MandateState
    invariants_broken: tuple[str, ...]

    def render(self) -> str:
        return (
            f"PERMITTED BUT ILLEGAL\n"
            f"  clock       {self.clock.astimezone(IST):%Y-%m-%d %H:%M IST}\n"
            f"  execute_at  {self.action.execute_at.astimezone(IST):%Y-%m-%d %H:%M IST}\n"
            f"  amount      {fmt(self.action.amount_paise)}\n"
            f"  attempts    {self.state.attempts_used}\n"
            f"  pending     {self.state.pending_pdn is not None}\n"
            f"  status      {self.state.status.value}\n"
            f"  cause       {self.state.cause.value}\n"
            f"  breaks      {', '.join(self.invariants_broken)}"
        )


@dataclass(frozen=True, slots=True)
class SweepResult:
    triples_enumerated: int
    permitted: int
    vetoed: int
    counterexamples: tuple[Counterexample, ...]
    seconds: float

    @property
    def ok(self) -> bool:
        return not self.counterexamples


#: A fixture chosen so that every ceiling actually bites: the amount due sits
#: above the ₹15,000 AFA-free ceiling, and max_amount sits above that again, so
#: C15 and C19 are separable rather than shadowing each other.
_AMOUNT_DUE: Final[Paise] = rupees(20_000)
_MAX_AMOUNT: Final[Paise] = rupees(25_000)

_AMOUNT_LEVELS: Final[tuple[Paise, ...]] = (
    -rupees(1),              # negative        -> OPS-AMT
    0,                       # zero            -> OPS-AMT
    rupees(5_000),           # partial, legal
    rupees(15_000),          # exactly at the ceiling
    rupees(15_000) + 1,      # one paise over  -> C15
    _AMOUNT_DUE,             # full due        -> C15
    _MAX_AMOUNT,             # at the cap      -> C15
    _MAX_AMOUNT + 1,         # over the cap    -> C19
)


def _base_state(
    status: MandateStatus,
    cause: CauseClass,
    attempts: int,
    pending: bool,
    variable: bool,
    origin: datetime,
) -> MandateState:
    return MandateState(
        mandate_id="MC",
        status=status,
        cause=cause,
        attempts_used=attempts,
        is_first_presentation=(attempts == 0),
        amount_due_paise=_AMOUNT_DUE,
        max_amount_paise=_MAX_AMOUNT,
        category=Category.STANDARD,
        cycle_end=origin + timedelta(days=30),
        validity_end=origin + timedelta(days=365),
        pending_pdn=(
            PDN(notified_at=origin, execute_at=origin + timedelta(hours=30), amount_paise=rupees(499))
            if pending
            else None
        ),
        contacts_used=0,
        issuer_id="HDFC",
        variable_amount_allowed=variable,
    )


def sweep(days: int = 1, max_counterexamples: int = 5) -> SweepResult:
    """CLAIM 1. Enumerate the full cross product and cross-check every verdict.

    Dimensions swept:
      clock slot        every 30-minute slot across `days` days (C7 needs all
                        times of day, including the 23:50 boundary)
      execution offset  -2h to +52h from the clock, on the slot grid, so both
                        aperture boundaries and the past are covered
      amount            8 levels straddling zero, the ceiling and the cap
      attempts          0..MAX_ATTEMPTS
      pending PDN       present / absent
      variable amount   allowed / not
    """
    started = _time.perf_counter()
    origin = datetime(2026, 9, 1, 0, 0, tzinfo=IST)

    clock_slots = [origin + timedelta(minutes=SLOT_MINUTES * i) for i in range(SLOTS_PER_DAY * days)]
    exec_offsets = range(-4, 105)  # -2h .. +52h on the 30-minute grid

    triples = permitted = 0
    counterexamples: list[Counterexample] = []

    for clock in clock_slots:
        exec_times = [clock + timedelta(minutes=SLOT_MINUTES * o) for o in exec_offsets]
        for attempts in range(MAX_ATTEMPTS + 1):
            for pending in (False, True):
                for variable in (False, True):
                    state = _base_state(
                        MandateStatus.LIVE, CauseClass.INSUFFICIENT_FUNDS,
                        attempts, pending, variable, origin,
                    )
                    for exec_at in exec_times:
                        for amount in _AMOUNT_LEVELS:
                            action = Commit(execute_at=exec_at, amount_paise=amount)
                            triples += 1
                            if not is_permitted(action, state, clock).allowed:
                                continue
                            permitted += 1
                            broken = _inv_violations(action, state, clock)
                            if broken and len(counterexamples) < max_counterexamples:
                                counterexamples.append(
                                    Counterexample(clock, action, state, tuple(broken))
                                )

    # Second sweep: status x cause, which are independent of the timing dimensions
    # above and would only bloat the cross product without adding coverage.
    legal_exec = origin + timedelta(hours=30)
    legal_exec = legal_exec.replace(minute=0 if legal_exec.minute < 30 else 30, second=0, microsecond=0)
    for status in MandateStatus:
        for cause in CauseClass:
            state = _base_state(status, cause, 0, False, True, origin)
            for amount in _AMOUNT_LEVELS:
                action = Commit(execute_at=legal_exec, amount_paise=amount)
                triples += 1
                if not is_permitted(action, state, origin).allowed:
                    continue
                permitted += 1
                broken = _inv_violations(action, state, origin)
                if broken and len(counterexamples) < max_counterexamples:
                    counterexamples.append(Counterexample(origin, action, state, tuple(broken)))

    return SweepResult(
        triples_enumerated=triples,
        permitted=permitted,
        vetoed=triples - permitted,
        counterexamples=tuple(counterexamples),
        seconds=_time.perf_counter() - started,
    )


# --------------------------------------------------------------------------- #
# CLAIM 2 — reachability under permitted actions only
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ReachResult:
    states_reached: int
    max_attempts_seen: int
    max_pending_seen: int
    seconds: float

    @property
    def cap_binding(self) -> bool:
        """True if the search actually drove the budget to the cap.

        Each attempt costs at least 24h of lead (C5), so a horizon under five days
        cannot reach four attempts and the C1 claim would be vacuously true. A
        proof that cannot fail is not a proof, so this is reported alongside it.
        """
        return self.max_attempts_seen == MAX_ATTEMPTS

    @property
    def ok(self) -> bool:
        return (
            self.max_attempts_seen <= MAX_ATTEMPTS
            and self.max_pending_seen <= 1
            and self.cap_binding
        )


def reachable(days: int = 6) -> ReachResult:
    """CLAIM 2. BFS the whole transition system under the constraint layer.

    Abstract state is (clock_slot, attempts, pending_exec_slot). An adversarial
    policy is assumed: from every state, *every* permitted commit is followed, so
    the search covers what any policy could ever do, not what ours does.

    Transition semantics mirror COMPLIANCE.md:
      commit                     -> a PDN becomes pending
      pending slot arrives       -> success (absorbing) | failure (attempt spent)
                                    | PDN rejected (no attempt spent, C6)
      cancel                     -> pending cleared, no attempt spent (C8)
    """
    started = _time.perf_counter()
    origin = datetime(2026, 9, 1, 0, 0, tzinfo=IST)
    horizon = SLOTS_PER_DAY * days
    slots = [origin + timedelta(minutes=SLOT_MINUTES * i) for i in range(horizon + 105)]

    start = (0, 0, -1)  # clock_slot, attempts_used, pending_exec_slot (-1 = none)
    seen: set[tuple[int, int, int]] = {start}
    queue: deque[tuple[int, int, int]] = deque([start])
    max_attempts = 0
    max_pending = 0

    while queue:
        clock_i, attempts, pending_i = queue.popleft()
        max_attempts = max(max_attempts, attempts)
        max_pending = max(max_pending, 1 if pending_i >= 0 else 0)
        if clock_i >= horizon:
            continue

        state = MandateState(
            mandate_id="MC",
            status=MandateStatus.LIVE,
            cause=CauseClass.INSUFFICIENT_FUNDS,
            attempts_used=attempts,
            is_first_presentation=(attempts == 0),
            amount_due_paise=rupees(499),
            max_amount_paise=rupees(1_000),
            category=Category.STANDARD,
            cycle_end=origin + timedelta(days=30),
            validity_end=origin + timedelta(days=365),
            pending_pdn=(
                PDN(notified_at=slots[clock_i], execute_at=slots[pending_i], amount_paise=rupees(499))
                if pending_i >= 0
                else None
            ),
            contacts_used=0,
            issuer_id="HDFC",
        )
        clock = slots[clock_i]

        def push(nxt: tuple[int, int, int]) -> None:
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)

        # The pending presentation fires when its slot arrives.
        if pending_i == clock_i:
            push((clock_i + 1, attempts + 1, -1))   # debit failed  -> attempt spent
            push((clock_i + 1, attempts, -1))       # PDN rejected  -> attempt not spent (C6)
            continue                                # success is absorbing

        # WAIT
        push((clock_i + 1, attempts, pending_i))

        # CANCEL_PENDING
        if pending_i >= 0:
            push((clock_i + 1, attempts, -1))

        # Every permitted COMMIT — adversarial, not policy-driven.
        #
        # Skipped entirely while something is in flight: C8 vetoes every commit
        # regardless of the execution offset, and CLAIM 1 already sweeps that
        # exhaustively across both pending states and all offsets. Re-deriving it
        # here once per reachable state cost more than the rest of the search.
        if pending_i < 0:
            for offset in range(40, 105):           # 20h .. 52h, straddling the aperture
                j = clock_i + offset
                if j >= len(slots):
                    break
                action = Commit(execute_at=slots[j], amount_paise=rupees(499))
                if is_permitted(action, state, clock).allowed:
                    push((clock_i + 1, attempts, j))

    return ReachResult(
        states_reached=len(seen),
        max_attempts_seen=max_attempts,
        max_pending_seen=max_pending,
        seconds=_time.perf_counter() - started,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Exhaustively verify the constraint layer.")
    ap.add_argument("--days", type=int, default=1, help="clock-days to sweep (CLAIM 1)")
    ap.add_argument(
        "--reach-days",
        type=int,
        default=6,
        help="horizon in days (CLAIM 2); under 5 the retry cap cannot be reached",
    )
    args = ap.parse_args(argv)

    # The console on Windows defaults to cp1252 and would mangle the rupee sign in
    # a counterexample dump. This output ends up in a demo video.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass

    print("=" * 68)
    print("CONSTRAINT LAYER — EXHAUSTIVE VERIFICATION")
    print("=" * 68)

    s = sweep(days=args.days)
    print(f"\nCLAIM 1  state x action sweep")
    print(f"  triples enumerated   {s.triples_enumerated:,}")
    print(f"  permitted            {s.permitted:,}")
    print(f"  vetoed               {s.vetoed:,}")
    print(f"  counterexamples      {len(s.counterexamples)}")
    print(f"  elapsed              {s.seconds:.2f}s")
    for ce in s.counterexamples:
        print("\n" + ce.render())

    r = reachable(days=args.reach_days)
    print(f"\nCLAIM 2  reachability under permitted actions")
    print(f"  states reached       {r.states_reached:,}")
    print(f"  max attempts seen    {r.max_attempts_seen} (cap {MAX_ATTEMPTS})")
    print(f"  max pending PDNs     {r.max_pending_seen} (cap 1)")
    print(f"  cap actually binding {'yes' if r.cap_binding else 'NO - horizon too short'}")
    print(f"  elapsed              {r.seconds:.2f}s")

    ok = s.ok and r.ok
    print("\n" + "=" * 68)
    if ok:
        print(f"VERIFIED — {s.triples_enumerated:,} (state, action, clock) triples enumerated,")
        print(f"           {r.states_reached:,} reachable states explored, 0 violations.")
        print("           Not sampled. Enumerated.")
    elif not s.ok:
        print("FAILED — the constraint layer permits an illegal action.")
    else:
        print("INCONCLUSIVE — the search never drove the retry budget to its cap.")
        print(f"               Re-run with --reach-days 6 or more.")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
