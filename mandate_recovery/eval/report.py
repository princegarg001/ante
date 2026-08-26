"""The results table.

    python -m mandate_recovery.eval.report --seeds 100-109

Every policy meets the identical world on a given seed, so the comparisons are
paired and the intervals are on per-seed differences. A fresh world is built for
each (policy, seed) pair because a world is consumed by being acted on.

The headline is **net value** — recovered minus the cost of recovering — because
recovered rupees alone rewards a policy for spending the customer's patience and
the merchant's mandate book to get them.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Final, Sequence

import numpy as np

from ..core.clock import IST
from ..core.money import fmt
from ..sim.world import World, WorldConfig
from ..predict.dataset import collect
from ..predict.model import SurvivalModel, TrainedModel
from ..sim.issuer import ISSUERS
from .baselines import FixedSchedule, NoRetry, StripeStyle
from .greedy import GreedyEV
from .harness import RunMetrics, run_policy
from .oracle import ClairvoyantOracle
from .policy import Calendar
from .stats import PairedComparison, compare, recovery_efficiency

ORIGIN: Final[datetime] = datetime(2026, 9, 1, 0, 0, tzinfo=IST)

#: Held out. No policy is fitted on these — see `sim/generate.py::SEED_SPLIT`.
EVAL_SEEDS: Final[tuple[int, ...]] = tuple(range(100, 110))


@dataclass
class Suite:
    seeds: tuple[int, ...]
    config: WorldConfig
    runs: dict[str, list[RunMetrics]]

    def values(self, policy: str, attr: Callable[[RunMetrics], float]) -> list[float]:
        return [attr(m) for m in self.runs[policy]]


#: Trained once per process on the TRAINING seeds only. Evaluation seeds are
#: never used to fit anything.
_MODEL: TrainedModel | None = None
TRAIN_SEEDS: Final[tuple[int, ...]] = tuple(range(0, 8))


def trained_model(config: WorldConfig) -> TrainedModel:
    global _MODEL
    if _MODEL is None:
        data = collect(TRAIN_SEEDS, config)
        _MODEL = SurvivalModel.fit(data, seed=0)
    return _MODEL


def _policies(world: World, model: TrainedModel) -> list:
    cal = Calendar(origin=world.origin, horizon_slots=world.horizon_slots)
    issuer_of = {m.mandate_id: ISSUERS[m.issuer].code for m in world.mandates}
    return [
        NoRetry(cal),
        FixedSchedule(cal),
        GreedyEV(model, cal, issuer_of=issuer_of),
        StripeStyle(cal),
        ClairvoyantOracle(world, cal),
    ]


def run_suite(seeds: Sequence[int], config: WorldConfig) -> Suite:
    model = trained_model(config)
    runs: dict[str, list[RunMetrics]] = {}
    for seed in seeds:
        probe = World.generate(seed, ORIGIN, config)
        n_policies = len(_policies(probe, model))
        for index in range(n_policies):
            world = World.generate(seed, ORIGIN, config)
            policy = _policies(world, model)[index]
            metrics = run_policy(policy, world)
            runs.setdefault(policy.name, []).append(metrics)
    return Suite(tuple(seeds), config, runs)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render(suite: Suite) -> None:
    names = list(suite.runs)
    baseline = next(n for n in names if n.startswith("B1"))
    oracle = next(n for n in names if n.startswith("oracle"))

    first = suite.runs[names[0]][0]
    print("=" * 100)
    print("RESULTS — recovery of a failed-debit batch")
    print("=" * 100)
    print(
        f"\n  seeds        {len(suite.seeds)} held out ({min(suite.seeds)}–{max(suite.seeds)})"
    )
    print(f"  book         {suite.config.n_mandates:,} mandates per seed")
    print(f"  batch        {np.mean([m.batch_size for m in suite.runs[names[0]]]):,.0f} "
          f"failed mandates, "
          f"{fmt(int(np.mean([m.batch_value_paise for m in suite.runs[names[0]]])))} at risk")
    print("  pairing      common random numbers — every policy meets the identical world")
    unact = np.mean([m.unactionable for m in suite.runs[names[0]]])
    unact_v = np.mean([m.unactionable_value_paise for m in suite.runs[names[0]]])
    print(f"  unactionable {unact:,.0f} of them ({fmt(int(unact_v))}) — already revoked or expired")
    print("               before any decision could be taken. No policy is offered these.")

    print("\n" + "-" * 100)
    hdr = (f"  {'policy':<38}{'recovered':>13}{'net value':>13}{'rate':>8}"
           f"{'₹/attempt':>11}{'survived':>10}{'stops':>7}{'illegal':>9}")
    print(hdr)
    print("-" * 100)
    for name in names:
        rs = suite.runs[name]
        print(
            f"  {name:<38}"
            f"{fmt(int(np.mean([m.recovered_paise for m in rs]))):>13}"
            f"{fmt(int(np.mean([m.net_value_paise for m in rs]))):>13}"
            f"{np.mean([m.recovery_rate for m in rs]):>7.1%}"
            f"{np.mean([m.slot_efficiency_paise for m in rs]) / 100:>11,.0f}"
            f"{np.mean([m.survival_rate for m in rs]):>10.1%}"
            f"{np.mean([m.stops for m in rs]):>7,.0f}"
            f"{np.mean([len(m.violating_mandates) for m in rs]):>9,.0f}"
        )

    # -- paired comparisons ------------------------------------------------
    print("\n" + "-" * 100)
    print(f"  PAIRED DIFFERENCE IN NET VALUE vs {baseline}")
    print(f"  {'':<38}{'mean':>13}  {'95% bootstrap CI':^27}  significance")
    print("-" * 100)
    b_vals = suite.values(baseline, lambda m: m.net_value_paise / 100)
    for name in names:
        if name == baseline:
            continue
        vals = suite.values(name, lambda m: m.net_value_paise / 100)
        c = compare(vals, b_vals, a_name=name, b_name=baseline)
        print(f"  {name:<38}{c.render(unit=' ₹')}")
    print("\n  * interval excludes zero")

    # -- the violation slide ----------------------------------------------
    print("\n" + "-" * 100)
    print("  WHAT THE TRANSPLANT COSTS")
    print("-" * 100)
    for name in names:
        rs = suite.runs[name]
        v: dict[str, float] = {}
        for m in rs:
            for rule, count in m.violations.items():
                v[rule] = v.get(rule, 0.0) + count / len(rs)
        if not v:
            print(f"  {name:<38}no illegal action proposed")
            continue
        detail = "  ".join(f"{k}×{int(round(n)):,}" for k, n in sorted(v.items(), key=lambda kv: -kv[1]))
        touched = np.mean([len(m.violating_mandates) for m in rs])
        share = np.mean([len(m.violating_mandates) / max(1, m.batch_size) for m in rs])
        print(f"  {name:<38}{detail}")
        print(f"  {'':<38}on {touched:,.0f} mandates ({share:.0%} of the batch)")

    # -- headroom ----------------------------------------------------------
    o_vals = suite.values(oracle, lambda m: m.net_value_paise / 100)
    print("\n" + "-" * 100)
    print("  HEADROOM AGAINST A LAWFUL CLAIRVOYANT")
    print("-" * 100)
    print(f"  {baseline:<38}{fmt(int(np.mean(b_vals) * 100)):>13}")
    print(f"  {oracle:<38}{fmt(int(np.mean(o_vals) * 100)):>13}")
    print(f"  {'headroom available':<38}{fmt(int((np.mean(o_vals) - np.mean(b_vals)) * 100)):>13}")
    for name in names:
        if name in (baseline, oracle):
            continue
        eff = recovery_efficiency(
            suite.values(name, lambda m: m.net_value_paise / 100), b_vals, o_vals
        )
        print(f"  {'recovery efficiency · ' + name.split(' · ')[0]:<38}{eff:>12.1%}")

    print("\n" + "=" * 100)


def to_json(suite: Suite) -> dict:
    out: dict = {"seeds": list(suite.seeds), "mandates": suite.config.n_mandates, "policies": {}}
    for name, rs in suite.runs.items():
        out["policies"][name] = {
            "recovered_paise": float(np.mean([m.recovered_paise for m in rs])),
            "net_value_paise": float(np.mean([m.net_value_paise for m in rs])),
            "recovery_rate": float(np.mean([m.recovery_rate for m in rs])),
            "slot_efficiency_paise": float(np.mean([m.slot_efficiency_paise for m in rs])),
            "survival_rate": float(np.mean([m.survival_rate for m in rs])),
            "presentations": float(np.mean([m.presentations for m in rs])),
            "stops": float(np.mean([m.stops for m in rs])),
            "regulatory_violations": float(np.mean([m.regulatory_violations for m in rs])),
            "violating_mandates": float(np.mean([len(m.violating_mandates) for m in rs])),
            "unactionable": float(np.mean([m.unactionable for m in rs])),
            "stopped_value_paise": float(np.mean([m.stopped_value_paise for m in rs])),
            "batch_size": float(np.mean([m.batch_size for m in rs])),
            "batch_value_paise": float(np.mean([m.batch_value_paise for m in rs])),
        }
    return out


def _parse_seeds(spec: str) -> tuple[int, ...]:
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        return tuple(range(int(lo), int(hi) + 1))
    return tuple(int(x) for x in spec.split(","))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the evaluation suite.")
    ap.add_argument("--seeds", default="100-109", help="e.g. 100-109 or 100,101")
    ap.add_argument("--mandates", type=int, default=1_500)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass

    suite = run_suite(_parse_seeds(args.seeds), WorldConfig(n_mandates=args.mandates))
    render(suite)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(to_json(suite), indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
