"""Generate and inspect a mandate book.

    python -m mandate_recovery.sim.generate --seed 42
    python -m mandate_recovery.sim.generate --seed 42 --json book.json

Reproducibility is the point: the same seed produces the same book on any
machine, so a results table can be re-derived by a reviewer rather than taken on
trust. The seed used for every reported number is printed alongside it.

Seeds 0–7 are for training. Seeds 100–109 are held out for evaluation and no
policy may be fitted on them; the split is a constant in `SEED_SPLIT` so it can
be checked rather than promised.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Final

import numpy as np

from ..core.clock import IST, SLOTS_PER_DAY
from ..core.money import fmt
from .customer import LATENT_TYPES
from .issuer import ISSUERS
from .world import Doom, World, WorldConfig

#: Checked rather than promised. The policy never sees an evaluation seed.
SEED_SPLIT: Final[dict[str, tuple[int, ...]]] = {
    "train": tuple(range(0, 8)),
    "eval": tuple(range(100, 110)),
}


def summarise(world: World) -> dict:
    m = world.mandates
    types = Counter(LATENT_TYPES[int(t)].name for t in world.population.type_index)
    issuers = Counter(ISSUERS[x.issuer].code for x in m)
    dooms = Counter(x.doom.value for x in m)
    amounts = np.array([x.amount_due for x in m])
    return {
        "seed": world.seed,
        "mandates": len(m),
        "days": world.config.days,
        "value_due_paise": int(amounts.sum()),
        "median_amount_paise": int(np.median(amounts)),
        "variable_amount_share": float(np.mean([x.variable_amount_allowed for x in m])),
        "new_registration_share": float(np.mean([x.is_new_registration for x in m])),
        "unrecoverable_share": float(
            sum(v for k, v in dooms.items() if k != Doom.NONE.value) / len(m)
        ),
        "issuer_uptime": world.issuers.uptime_fraction(),
        "liquidity_types": dict(types),
        "issuers": dict(issuers),
        "doom": dict(dooms),
    }


def render(world: World) -> None:
    s = summarise(world)
    print("=" * 72)
    print(f"MANDATE BOOK — seed {s['seed']}")
    print("=" * 72)
    split = next((k for k, v in SEED_SPLIT.items() if s["seed"] in v), "unassigned")
    print(f"\n  seed split            {split}")
    print(f"  mandates              {s['mandates']:,}")
    print(f"  horizon               {s['days']} days ({s['days'] * SLOTS_PER_DAY:,} slots)")
    print(f"  value due             {fmt(s['value_due_paise'])}")
    print(f"  median debit          {fmt(s['median_amount_paise'])}")
    print(f"  variable-amount       {s['variable_amount_share']:.1%}")
    print(f"  newly registered      {s['new_registration_share']:.1%}")
    print(f"  unrecoverable         {s['unrecoverable_share']:.1%}")
    print(f"  issuer uptime         {s['issuer_uptime']:.2%}")

    print("\n  liquidity types")
    for name, count in sorted(s["liquidity_types"].items(), key=lambda kv: -kv[1]):
        print(f"    {name:<22}{count:>6,}  {'#' * int(40 * count / s['mandates'])}")

    print("\n  unrecoverable segment")
    for name, count in sorted(s["doom"].items(), key=lambda kv: -kv[1]):
        if name == Doom.NONE.value:
            continue
        print(f"    {name:<22}{count:>6,}")

    print("\n" + "=" * 72)
    print("  calibration:  python -m mandate_recovery.sim.calibrate --seed "
          f"{s['seed']}")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate a seeded mandate book.")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--mandates", type=int, default=3_000)
    ap.add_argument("--days", type=int, default=35)
    ap.add_argument("--json", type=Path, help="write the summary to a file")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass

    world = World.generate(
        args.seed,
        datetime(2026, 9, 1, 0, 0, tzinfo=IST),
        WorldConfig(n_mandates=args.mandates, days=args.days),
    )
    render(world)
    if args.json:
        args.json.write_text(json.dumps(summarise(world), indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
