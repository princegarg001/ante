"""The agent deciding — one batch, every decision, with its reason.

This is the demonstration the other two do not give you. `crash_demo` proves the
money path survives being killed; `webhook_demo` proves the edge refuses what it
should. Neither shows the agent *thinking*, and that is the part worth watching:
a book of failed mandates, and one justified decision for each.

    python -m demo.agent_demo                 # the default batch
    python -m demo.agent_demo --delay 0.04    # slow it down for a screen capture
    python -m demo.agent_demo --only stop     # just the refusals
    python -m demo.agent_demo --mandates 400  # smaller and faster

Nothing here is staged. The decisions come out of a real run against a real
world, through the same allocator the evaluation measures. The reason strings
are the allocator's own — they are not generated for the demo, and a test
asserts every plan carries one.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from typing import Mapping, Sequence

from mandate_recovery.core.clock import IST
from mandate_recovery.core.money import fmt
from mandate_recovery.eval.harness import run_policy
from mandate_recovery.eval.policy import Calendar
from mandate_recovery.policy.allocator import Plan, SlotAllocator
from mandate_recovery.predict.pipeline import fit_cached
from mandate_recovery.sim.issuer import ISSUERS
from mandate_recovery.sim.world import World, WorldConfig

ORIGIN = datetime(2026, 9, 1, 0, 0, tzinfo=IST)

GREEN = "\033[32m"
RED = "\033[31m"
AMBER = "\033[33m"
DIM = "\033[2m"
BOLD = "\033[1m"
OFF = "\033[0m"

#: How each action prints. The colours carry the argument: committing spends an
#: irreplaceable attempt, refusing protects one, waiting is neither.
STYLE = {
    "commit": (GREEN, "commit"),
    "wait": (AMBER, "wait  "),
    "stop": (RED, "refuse"),
}


class Recorder:
    """Wraps a policy and keeps every book it produces.

    The allocator already stores its most recent book; this keeps all of them,
    so the demo can show the epoch where the most was actually decided rather
    than whichever one happened to be last.
    """

    def __init__(self, inner: SlotAllocator) -> None:
        self.inner = inner
        self.name = inner.name
        self.books: list[tuple[datetime, list[Plan]]] = []

    def plan(self, batch: Sequence[object], now: datetime) -> Mapping[str, object]:
        actions = self.inner.plan(batch, now)  # type: ignore[arg-type]
        if self.inner.last_book:
            self.books.append((now, list(self.inner.last_book)))
        return actions

    def reset(self, seed: int) -> None:
        self.inner.reset(seed)


def rule(title: str) -> None:
    print(f"\n{BOLD}{title}{OFF}")
    print(DIM + "-" * 78 + OFF)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed", type=int, default=100)
    p.add_argument("--mandates", type=int, default=700)
    p.add_argument("--days", type=int, default=35)
    p.add_argument("--delay", type=float, default=0.0,
                   help="seconds between lines; use 0.03-0.05 for a screen capture")
    p.add_argument("--limit", type=int, default=45, help="lines to print")
    p.add_argument("--only", choices=("commit", "wait", "stop"), default=None)
    args = p.parse_args()

    # Windows consoles still default to cp1252, which cannot encode the rupee
    # sign — so the demo dies on its first money column. Ask for UTF-8 and carry
    # on if the stream does not support the request.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

    cfg = WorldConfig(n_mandates=args.mandates, days=args.days)

    print(f"\n{BOLD}The agent deciding{OFF}")
    print(f"{DIM}fitting the survival model on held-out worlds "
          f"(cached after the first run)...{OFF}")
    sys.stdout.flush()
    fitted = fit_cached(range(0, 8), cfg)

    world = World.generate(args.seed, ORIGIN, cfg)
    calendar = Calendar(origin=world.origin, horizon_slots=world.horizon_slots)
    issuer_of = {m.mandate_id: ISSUERS[m.issuer].code for m in world.mandates}
    allocator = SlotAllocator(
        fitted.model, calendar, profile=fitted.profile, issuer_of=issuer_of
    )
    recorder = Recorder(allocator)

    print(f"{DIM}running the batch...{OFF}")
    sys.stdout.flush()
    metrics = run_policy(recorder, world)

    if not recorder.books:
        print("no decisions were taken")
        return 1

    # One line per mandate, folded across the whole run.
    #
    # Showing a single epoch was the first attempt and it misrepresents the
    # agent badly: the work is spread over the clock, so any one epoch is a
    # handful of decisions and the early ones are entirely `wait`. The first
    # cut of this demo showed 173 mandates all waiting and not one judgement,
    # which is correct behaviour and useless footage. This keeps the most
    # decisive thing the allocator ever concluded about each mandate.
    RANK = {"stop": 0, "commit": 1, "wait": 2}
    final: dict[str, Plan] = {}
    for _, plans in recorder.books:
        for pl in plans:
            prev = final.get(pl.mandate_id)
            if prev is None or RANK[pl.action] < RANK.get(prev.action, 3):
                final[pl.mandate_id] = pl
    book = list(final.values())

    rule(f"1 · The book — {len(book)} mandates, one decision each")
    print(f"{DIM}  Every line is the allocator's own justification, not a caption")
    print(f"  written for the demo. Refusals first — they are the half worth watching.{OFF}\n")

    shown = [pl for pl in book if args.only is None or pl.action == args.only]
    # Refusals first: they are the interesting half and the reason this demo
    # exists. Within a group, biggest money first.
    shown.sort(key=lambda pl: (RANK.get(pl.action, 3), -pl.amount_paise))

    for plan in shown[: args.limit]:
        colour, label = STYLE.get(plan.action, (DIM, plan.action))
        # A refusal carries no amount, because nothing is being presented.
        # Printing it as a rupee zero reads as a bug rather than as an absence.
        money = fmt(plan.amount_paise) if plan.amount_paise else "—"
        print(
            f"  {plan.mandate_id:<12} {colour}{label}{OFF}  "
            f"{money:>10}   {DIM}{plan.reason}{OFF}"
        )
        if args.delay:
            sys.stdout.flush()
            time.sleep(args.delay)

    if len(shown) > args.limit:
        print(f"{DIM}  ... and {len(shown) - args.limit} more{OFF}")

    # ------------------------------------------------------------------ #
    counts = {a: sum(1 for pl in book if pl.action == a) for a in STYLE}
    committed = sum(pl.amount_paise for pl in book if pl.action == "commit")

    rule("2 · What it decided")
    print(f"  {GREEN}committed{OFF}   {counts['commit']:>4}   {fmt(committed):>12}"
          f"   {DIM}an attempt spent{OFF}")
    print(f"  {AMBER}waiting{OFF}     {counts['wait']:>4}   {'':>12}"
          f"   {DIM}aperture not open yet — patience, not surrender{OFF}")
    print(f"  {RED}refused{OFF}     {counts['stop']:>4}   {'':>12}"
          f"   {DIM}the attempt is worth less than the mandate it risks{OFF}")

    rule("3 · What the whole run collected")
    print(f"  batch                {metrics.batch_size} failed mandates"
          f"   {fmt(metrics.batch_value_paise)} at risk")
    print(f"  recovered            {fmt(metrics.recovered_paise)}")
    print(f"  net value            {BOLD}{fmt(metrics.net_value_paise)}{OFF}")
    print(f"  presentations        {metrics.presentations}")
    print(f"  regulatory breaches  {GREEN if not metrics.violations else RED}"
          f"{sum(metrics.violations.values()) if metrics.violations else 0}{OFF}")

    print(f"\n{DIM}  Refusing is a decision here, not an absence of one. It is priced,")
    print(f"  it is logged, and it carries a reason a human can argue with.{OFF}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
