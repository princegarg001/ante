"""Reconstruct a run from the journal.

`--replay <run_id>` is the question an incident review actually asks: what did
this thing decide, why, and what did it do about it. The journal is the only
source — there is no separate reporting store to drift out of sync with it.

Verification is not optional here. Reading walks the hash chain, so a replay
that prints anything at all has already proved the log was not edited.

Run:  python -m mandate_recovery.act.replay runs/journal.jsonl
      python -m mandate_recovery.act.replay runs/journal.jsonl --run night-batch
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ..core.money import fmt
from .journal import Journal, RecordKind, TamperError


@dataclass
class RunSummary:
    run_id: str
    mode: str = "unknown"
    started: str = ""
    ended: str = ""
    end_reason: str = ""
    decisions: int = 0
    intents: int = 0
    effects_performed: int = 0
    effects_ok: int = 0
    paise_attempted: int = 0
    skipped: Counter = None  # type: ignore[assignment]
    kills: int = 0
    in_doubt: set[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.skipped is None:
            self.skipped = Counter()
        if self.in_doubt is None:
            self.in_doubt = set()


def summarise(journal: Journal, run_id: str | None = None) -> dict[str, RunSummary]:
    summaries: dict[str, RunSummary] = {}
    for rec in journal:
        if run_id is not None and rec.run_id != run_id:
            continue
        s = summaries.setdefault(rec.run_id, RunSummary(rec.run_id))
        body = rec.body
        match rec.kind:
            case RecordKind.RUN_START:
                s.mode = str(body.get("mode", "unknown"))
                s.started = rec.ts
            case RecordKind.DECISION:
                s.decisions += 1
            case RecordKind.INTENT:
                s.intents += 1
                s.in_doubt.add(str(body["idem_key"]))
            case RecordKind.EFFECT:
                s.in_doubt.discard(str(body["idem_key"]))
                if body.get("performed"):
                    s.effects_performed += 1
                    s.paise_attempted += int(body.get("amount_paise", 0))
                    if body.get("ok"):
                        s.effects_ok += 1
            case RecordKind.SKIPPED:
                s.skipped[str(body.get("reason", "?"))] += 1
            case RecordKind.KILL:
                s.kills += 1
            case RecordKind.RUN_END:
                s.ended = rec.ts
                s.end_reason = str(body.get("reason", ""))
    return summaries


def render(journal: Journal, run_id: str | None, verbose: bool) -> int:
    try:
        total = journal.verify()
    except TamperError as exc:
        print(f"REFUSING TO REPLAY — {exc}")
        return 2

    print("=" * 72)
    print(f"JOURNAL REPLAY — {journal.path}")
    print("=" * 72)
    print(f"\nchain verified: {total:,} records, no breaks\n")

    summaries = summarise(journal, run_id)
    if not summaries:
        print("no matching runs")
        return 1

    exit_code = 0
    for s in summaries.values():
        print("-" * 72)
        print(f"run {s.run_id}   mode={s.mode}")
        print(f"  started            {s.started or '-'}")
        print(f"  ended              {s.ended or '(no RUN_END — run did not finish)'}")
        if s.end_reason:
            print(f"  end reason         {s.end_reason}")
        print(f"  decisions          {s.decisions}")
        print(f"  intents            {s.intents}")
        print(f"  effects performed  {s.effects_performed}  ({s.effects_ok} ok)")
        print(f"  value attempted    {fmt(s.paise_attempted)}")
        if s.kills:
            print(f"  kill switch hits   {s.kills}")
        for reason, n in sorted(s.skipped.items()):
            print(f"  skipped/{reason:<10} {n}")
        if s.in_doubt:
            exit_code = 1
            print(f"  IN DOUBT           {len(s.in_doubt)} intent(s) with no outcome")
            for key in sorted(s.in_doubt):
                print(f"      {key[:16]}…  — resolve with Executor.recover()")

    if verbose:
        print("\n" + "-" * 72)
        print("DECISIONS\n")
        for rec in journal:
            if run_id is not None and rec.run_id != run_id:
                continue
            if rec.kind is RecordKind.DECISION:
                b = rec.body
                print(f"  {rec.ts}  {b['mandate_id']}  attempt {b['attempt_index']}")
                print(f"      {b['justification']}")
                if b.get("bid_paise") is not None:
                    print(
                        f"      bid {fmt(int(b['bid_paise']))}"
                        f"  clearing {fmt(int(b['clearing_price_paise'] or 0))}"
                    )

    print()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Replay a run from the journal.")
    ap.add_argument("journal", type=Path, help="path to journal.jsonl")
    ap.add_argument("--run", dest="run_id", help="limit to one run id")
    ap.add_argument("-v", "--verbose", action="store_true", help="print decisions")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass

    if not args.journal.exists():
        print(f"no journal at {args.journal}")
        return 1

    return render(Journal(args.journal), args.run_id, args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
