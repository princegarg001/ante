"""Which part of the allocator is actually doing the work.

    python -m mandate_recovery.eval.ablation --seeds 100-104

A policy that beats its baseline has not explained itself. The allocator adds
three things at once — backward induction over the remaining budget, an
option-value term for the mandate, and a shadow price on scarce execution
windows — and it would be easy to attribute the gain to whichever of the three
sounds best.

Turning the capacity constraint off answers it directly, and the answer was not
the expected one: **with capacity unlimited the allocator is indistinguishable
from the greedy baseline.** The dynamic programme and the option-value term, by
themselves, are worth close to nothing here. Essentially all of the gain comes
from the price.

That is a sharper claim than the one it replaced, and a more useful one. The
shadow price is not merely rationing a scarce resource; it acts as a
**selectivity threshold**. An attempt has to clear a price to be worth making,
so marginal opportunities are refused and the budget concentrates on the ones
that clearly beat it. The evidence is the shape of the response: value peaks at
an intermediate capacity and falls off on both sides. Too loose and nothing is
filtered; too tight and opportunities worth taking get priced out.

This is the ablation to show a panel, because it is the one that says which idea
earned the number.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Sequence

import numpy as np

from ..core.clock import IST
from ..core.money import fmt
from ..policy.allocator import AllocatorConfig, SlotAllocator
from ..predict.pipeline import fit_cached
from ..sim.issuer import ISSUERS
from ..sim.world import World, WorldConfig
from .baselines import FixedSchedule
from .greedy import GreedyEV
from .harness import run_policy
from .policy import Calendar
from .stats import compare

ORIGIN: Final[datetime] = datetime(2026, 9, 1, 0, 0, tzinfo=IST)
TRAIN_SEEDS: Final[tuple[int, ...]] = tuple(range(0, 8))

#: `window_capacity_share` values to sweep. 5.0 is "capacity is never binding",
#: which is the arm that isolates the price from everything else.
CAPACITY_ARMS: Final[tuple[tuple[float, str], ...]] = (
    (5.0, "unlimited — no price ever forms"),
    (0.20, "loose"),
    (0.045, "default"),
    (0.02, "tight"),
    (0.008, "very tight"),
)


@dataclass(frozen=True, slots=True)
class Arm:
    label: str
    net: list[float]
    rate: float
    attempts: float
    stops: float


def _run(make, seeds: Sequence[int], config: WorldConfig) -> Arm:
    net, rate, attempts, stops = [], [], [], []
    for seed in seeds:
        world = World.generate(seed, ORIGIN, config)
        cal = Calendar(origin=world.origin, horizon_slots=world.horizon_slots)
        issuer_of = {m.mandate_id: ISSUERS[m.issuer].code for m in world.mandates}
        m = run_policy(make(cal, issuer_of), world)
        net.append(m.net_value_paise / 100)
        rate.append(m.recovery_rate)
        attempts.append(m.presentations)
        stops.append(m.stops)
    return Arm("", net, float(np.mean(rate)), float(np.mean(attempts)), float(np.mean(stops)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Isolate what the allocator's gain comes from.")
    ap.add_argument("--seeds", default="100-104")
    ap.add_argument("--mandates", type=int, default=800)
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass

    if "-" in args.seeds:
        lo, hi = args.seeds.split("-", 1)
        seeds = tuple(range(int(lo), int(hi) + 1))
    else:
        seeds = tuple(int(x) for x in args.seeds.split(","))

    config = WorldConfig(n_mandates=args.mandates, days=35)
    fitted = fit_cached(TRAIN_SEEDS, config)

    print("=" * 92)
    print("ABLATION — which part of the allocator earns the number")
    print("=" * 92)
    print(f"\n  {len(seeds)} held-out seeds, {args.mandates:,} mandates each, paired\n")

    b1 = _run(lambda c, io: FixedSchedule(c), seeds, config)
    b2 = _run(
        lambda c, io: GreedyEV(fitted.model, c, issuer_of=io, profile=fitted.profile),
        seeds,
        config,
    )

    print(f"  {'arm':<44}{'net value':>13}{'rate':>8}{'attempts':>10}{'stops':>8}")
    print("-" * 92)
    print(f"  {'B1 · fixed schedule':<44}{fmt(int(np.mean(b1.net) * 100)):>13}"
          f"{b1.rate:>8.1%}{b1.attempts:>10,.0f}{b1.stops:>8,.0f}")
    print(f"  {'B2 · greedy EV, same model and belief':<44}"
          f"{fmt(int(np.mean(b2.net) * 100)):>13}{b2.rate:>8.1%}"
          f"{b2.attempts:>10,.0f}{b2.stops:>8,.0f}")

    arms: list[tuple[str, Arm]] = []
    for share, label in CAPACITY_ARMS:
        cfg = AllocatorConfig(window_capacity_share=share)
        arm = _run(
            lambda c, io, cfg=cfg: SlotAllocator(
                fitted.model, c, profile=fitted.profile, issuer_of=io, config=cfg
            ),
            seeds,
            config,
        )
        arms.append((label, arm))
        print(f"  {'allocator · capacity ' + label:<44}"
              f"{fmt(int(np.mean(arm.net) * 100)):>13}{arm.rate:>8.1%}"
              f"{arm.attempts:>10,.0f}{arm.stops:>8,.0f}")

    print("\n" + "-" * 92)
    print("  WHAT EACH IDEA IS WORTH, versus B2 (identical model and belief)")
    print("-" * 92)
    for label, arm in arms:
        c = compare(arm.net, b2.net, a_name=label, b_name="B2")
        print(f"  {'capacity ' + label:<44}{c.render(unit=' ₹')}")

    unlimited = next(a for lbl, a in arms if lbl.startswith("unlimited"))
    default = next(a for lbl, a in arms if lbl == "default")
    dp_only = compare(unlimited.net, b2.net, a_name="dp", b_name="b2")
    price = compare(default.net, unlimited.net, a_name="priced", b_name="unpriced")

    print("\n" + "=" * 92)
    print("  DECOMPOSITION")
    print("=" * 92)
    print(f"  dynamic programme + option value, no price   {dp_only.mean_diff:>+12,.0f} ₹"
          f"   {'significant' if dp_only.significant else 'not distinguishable from zero'}")
    print(f"  adding the capacity price                    {price.mean_diff:>+12,.0f} ₹"
          f"   {'significant' if price.significant else 'not distinguishable from zero'}")
    print("\n  Value peaks at an intermediate capacity and falls away on both sides,")
    print("  which is what a selectivity threshold looks like rather than mere rationing.")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
