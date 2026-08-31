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
from ..policy.allocator import SlotAllocator
from ..predict.pipeline import Fitted, fit_cached
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


#: Fitted once per process on the TRAINING seeds only. Evaluation seeds are never
#: used to fit anything.
TRAIN_SEEDS: Final[tuple[int, ...]] = tuple(range(0, 8))


def trained_model(config: WorldConfig) -> Fitted:
    return fit_cached(TRAIN_SEEDS, config)


def _policies(world: World, fitted: Fitted) -> list:
    cal = Calendar(origin=world.origin, horizon_slots=world.horizon_slots)
    issuer_of = {m.mandate_id: ISSUERS[m.issuer].code for m in world.mandates}
    return [
        NoRetry(cal),
        FixedSchedule(cal),
        GreedyEV(fitted.model, cal, issuer_of=issuer_of, profile=fitted.profile),
        StripeStyle(cal),
        SlotAllocator(fitted.model, cal, profile=fitted.profile, issuer_of=issuer_of),
        ClairvoyantOracle(world, cal),
    ]


def run_suite(
    seeds: Sequence[int], config: WorldConfig, audit_dir: Path | None = None
) -> Suite:
    fitted = trained_model(config)
    runs: dict[str, list[RunMetrics]] = {}
    for seed in seeds:
        probe = World.generate(seed, ORIGIN, config)
        n_policies = len(_policies(probe, fitted))
        for index in range(n_policies):
            world = World.generate(seed, ORIGIN, config)
            policy = _policies(world, fitted)[index]
            metrics = run_policy(policy, world, audit_dir=audit_dir)
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
    audited = [m for rs in suite.runs.values() for m in rs if m.journal_path]
    if audited:
        records = sum(m.journal_records for m in audited)
        print(f"  audit        {records:,} hash-chained records across "
              f"{len(audited)} runs — every presentation is replayable")
    unact = np.mean([m.unactionable for m in suite.runs[names[0]]])
    unact_v = np.mean([m.unactionable_value_paise for m in suite.runs[names[0]]])
    print(f"  unactionable {unact:,.0f} of them ({fmt(int(unact_v))}) — already revoked or expired")
    print("               before any decision could be taken. No policy is offered these.")

    print("\n" + "-" * 100)
    hdr = (f"  {'policy':<38}{'recovered':>13}{'net value':>13}{'rate':>8}"
           f"{'₹/att':>8}{'surv':>7}{'stop':>6}{'escal':>7}{'illegal':>9}")
    print(hdr)
    print("-" * 100)
    for name in names:
        rs = suite.runs[name]
        print(
            f"  {name:<38}"
            f"{fmt(int(np.mean([m.recovered_paise for m in rs]))):>13}"
            f"{fmt(int(np.mean([m.net_value_paise for m in rs]))):>13}"
            f"{np.mean([m.recovery_rate for m in rs]):>7.1%}"
            f"{np.mean([m.slot_efficiency_paise for m in rs]) / 100:>8,.0f}"
            f"{np.mean([m.survival_rate for m in rs]):>7.0%}"
            f"{np.mean([m.stops for m in rs]):>6,.0f}"
            f"{np.mean([sum(m.escalations.values()) for m in rs]):>7,.0f}"
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

    # -- the stop list, scored ---------------------------------------------
    print("\n" + "-" * 100)
    print("  THE STOP LIST — what was refused, and what refusing it cost")
    print("-" * 100)
    print(f"  {'policy':<38}{'refusals':>10}{'value refused':>16}"
          f"{'right':>9}{'regret':>14}{'regret %':>10}")
    for name in names:
        rs = suite.runs[name]
        rows = [r for m in rs for r in m.stop_ledger]
        if not rows:
            print(f"  {name:<38}{'—':>10}")
            continue
        n = len(rows) / len(rs)
        refused = sum(r.outstanding_paise for r in rows) / len(rs)
        regret = sum(r.recoverable_paise for r in rows) / len(rs)
        right = sum(1 for r in rows if r.was_right) / max(1, len(rows))
        print(f"  {name:<38}{n:>10,.0f}{fmt(int(refused)):>16}"
              f"{right:>9.0%}{fmt(int(regret)):>14}"
              f"{regret / max(1.0, refused):>10.1%}")
    print()
    print("  'right' is the share of refusals from which a clairvoyant could have")
    print("  collected nothing. 'regret' is what the rest would in fact have paid —")
    print("  ground truth, computed after the run, never visible to any policy.")

    # -- escalation ladder --------------------------------------------------
    print("\n" + "-" * 100)
    print("  COMPLIANT ESCALATION — what happens to a mandate a retry cannot help")
    print("-" * 100)
    for name in names:
        rs = suite.runs[name]
        tally: dict[str, float] = {}
        for m in rs:
            for kind, count in m.escalations.items():
                tally[kind] = tally.get(kind, 0.0) + count / len(rs)
        if not tally:
            print(f"  {name:<38}stops only — no escalation ladder")
            continue
        detail = "   ".join(
            f"{k}×{v:,.0f}" for k, v in sorted(tally.items(), key=lambda kv: -kv[1])
        )
        print(f"  {name:<38}{detail}")

    # -- diagnosis ----------------------------------------------------------
    pairs = [pr for m in suite.runs[names[1]] for pr in m.diagnosis_pairs]
    if pairs:
        terminal = {
            "MANDATE_EXPIRED", "MANDATE_REVOKED",
            "AFA_REQUIRED", "VPA_INVALID", "TERMINAL",
        }
        correct = sum(1 for t, i in pairs if t == i)
        dangerous = sum(
            1 for t, i in pairs if t in terminal and i not in terminal
        )
        confidently_wrong = sum(
            1
            for t, i in pairs
            if t in terminal and i not in terminal and i != "UNKNOWN"
        )
        print("\n" + "-" * 100)
        print("  DIAGNOSIS — the agent infers the cause, it does not read it")
        print("-" * 100)
        print(f"  failures classified          {len(pairs):,}")
        print(f"  accuracy                     {correct / len(pairs):.1%}")
        print(f"  terminal read as recoverable {dangerous:,}"
              f"  ({dangerous / max(1, len(pairs)):.1%})")
        print(f"  ...of which confidently so   {confidently_wrong:,}")
        print()
        counts: dict[tuple[str, str], int] = {}
        for pr in pairs:
            counts[pr] = counts.get(pr, 0) + 1
        for (t, i), n in sorted(counts.items(), key=lambda kv: -kv[1])[:8]:
            flag = "  <- fails towards uncertainty" if t != i else ""
            print(f"    {t:<22} -> {i:<22}{n:>7,}{flag}")
        print()
        print("  Every misclassification lands on UNKNOWN rather than on a confident")
        print("  wrong cause. The policy is told \"I do not know\", never \"go ahead\".")

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

    # The ablation that isolates allocation from the model.
    greedy = next((n for n in names if n.startswith("B2")), None)
    alloc = next((n for n in names if n.startswith("allocator")), None)
    if greedy and alloc:
        g = suite.values(greedy, lambda m: m.net_value_paise / 100)
        a = suite.values(alloc, lambda m: m.net_value_paise / 100)
        c = compare(a, g, a_name=alloc, b_name=greedy)
        print()
        print("-" * 100)
        print("  WHAT ALLOCATION IS WORTH, HOLDING THE MODEL FIXED")
        print("-" * 100)
        print(f"  {'allocator vs B2 (same model, no budget reasoning)':<38}{c.render(unit=' ₹')}")

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
            "diagnosis_accuracy": (
                float(
                    np.mean(
                        [
                            sum(1 for t, i in m.diagnosis_pairs if t == i)
                            / max(1, len(m.diagnosis_pairs))
                            for m in rs
                        ]
                    )
                )
            ),
            "escalations": float(np.mean([sum(m.escalations.values()) for m in rs])),
            "stop_refusals": float(np.mean([len(m.stop_ledger) for m in rs])),
            "stop_value_refused_paise": float(
                np.mean([sum(r.outstanding_paise for r in m.stop_ledger) for m in rs])
            ),
            "stop_realised_regret_paise": float(
                np.mean([sum(r.recoverable_paise for r in m.stop_ledger) for m in rs])
            ),
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
    ap.add_argument(
        "--audit",
        type=Path,
        help="drive every presentation through the write-ahead log and "
             "hash-chained receipts, so the reported rupees are replayable",
    )
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass

    suite = run_suite(
        _parse_seeds(args.seeds),
        WorldConfig(n_mandates=args.mandates),
        audit_dir=args.audit,
    )
    render(suite)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(to_json(suite), indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
