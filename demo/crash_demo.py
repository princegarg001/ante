"""Crash-safety demonstration: a real `kill -9` in the in-doubt window.

Not a simulation. The orchestrator spawns a worker process, waits until that
worker is parked at the exact instant where a pre-debit notification has landed
at the gateway but its outcome has not yet been written to the journal, and then
terminates it uncatchably. The worker gets no chance to clean up, flush, or
write a "sorry, I died" record — because in production it would not get one.

Then a fresh process starts on the same journal and the same gateway records,
and the demonstration is that it:

  1. detects the unresolved intent left behind by the kill
  2. resolves the intent by *asking* the gateway, not by retrying
  3. re-runs the identical batch and performs zero duplicate effects
  4. never raises a second notification, so nothing gets cancelled under C8
  5. still verifies as an unbroken hash chain

Run:  python -m demo.crash_demo
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from mandate_recovery.act import (
    ExecutionMode,
    Executor,
    Journal,
    RecordKind,
)
from mandate_recovery.act.executor import DecisionContext, Ledger, idempotency_key
from mandate_recovery.act.gateway import FileGateway
from mandate_recovery.core.clock import IST
from mandate_recovery.core.money import fmt, rupees
from mandate_recovery.core.types import (
    CauseClass,
    Category,
    Commit,
    MandateState,
    MandateStatus,
)

RUN_DIR = Path("runs/crash-demo")
JOURNAL = RUN_DIR / "journal.jsonl"
GATEWAY = RUN_DIR / "gateway.json"
MARKER = RUN_DIR / "READY_TO_KILL"

CLOCK = datetime(2026, 9, 1, 0, 0, tzinfo=IST)
RUN_ID = "night-batch"
KILL_AT = 2                      # index of the mandate to die on


def build_batch() -> list[tuple[MandateState, Commit, DecisionContext]]:
    """Five mandates, each with a lawful commitment 24h ahead at 00:00 — non-peak,
    slot-aligned, inside the notification aperture."""
    batch = []
    for i in range(5):
        mandate_id = f"MND_{i:04d}"
        amount = rupees(499 + i * 100)
        state = MandateState(
            mandate_id=mandate_id,
            status=MandateStatus.LIVE,
            cause=CauseClass.INSUFFICIENT_FUNDS,
            attempts_used=0,
            is_first_presentation=True,
            amount_due_paise=amount,
            max_amount_paise=rupees(2_000),
            category=Category.STANDARD,
            cycle_end=CLOCK + timedelta(days=25),
            validity_end=CLOCK + timedelta(days=365),
            pending_pdn=None,
            contacts_used=0,
            issuer_id="HDFC",
        )
        action = Commit(execute_at=CLOCK + timedelta(hours=24), amount_paise=amount)
        ctx = DecisionContext(
            mandate_id=mandate_id,
            cycle_id="2026-09",
            attempt_index=0,
            justification=(
                f"balance posterior favours the 1st; committing {fmt(amount)} "
                f"to the 00:00 slot"
            ),
            policy_version="day2-demo",
        )
        batch.append((state, action, ctx))
    return batch


def kill_key() -> str:
    state, action, ctx = build_batch()[KILL_AT]
    return idempotency_key(ctx.mandate_id, ctx.cycle_id, ctx.attempt_index, action)


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #


def run_worker(pause: bool) -> int:
    gateway = FileGateway(GATEWAY, pause_after_key=kill_key() if pause else None)
    executor = Executor(
        Journal(JOURNAL),
        gateway,
        now=lambda: CLOCK,
        mode=ExecutionMode.LIVE,
    )
    executor.recover()
    executor.begin_run(RUN_ID)
    for state, action, ctx in build_batch():
        outcome = executor.submit(action, state, CLOCK, ctx)
        print(f"  {ctx.mandate_id}  {outcome.status:<9} {outcome.detail}")
    executor.end_run()
    return 0


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def banner(text: str) -> None:
    print("\n" + "=" * 72)
    print(text)
    print("=" * 72)


def journal_state() -> tuple[int, set[str]]:
    ledger = Ledger.from_journal(Journal(JOURNAL))
    return len(ledger.applied), ledger.in_doubt


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass

    if args.worker:
        return run_worker(args.pause)

    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR)
    RUN_DIR.mkdir(parents=True)

    banner("1 · START THE BATCH, THEN KILL IT MID-FLIGHT")
    print(f"\nfive mandates, live mode, journal at {JOURNAL}")
    print(f"the process will be killed while committing {build_batch()[KILL_AT][2].mandate_id}\n")

    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "demo.crash_demo", "--worker", "--pause"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    deadline = time.monotonic() + 60
    while not MARKER.exists() and time.monotonic() < deadline:
        if proc.poll() is not None:
            print(proc.stdout.read() if proc.stdout else "")
            print("worker exited before reaching the in-doubt window")
            return 1
        time.sleep(0.05)

    if not MARKER.exists():
        proc.kill()
        print("timed out waiting for the worker")
        return 1

    proc.kill()                       # SIGKILL on POSIX, TerminateProcess on Windows
    proc.wait(timeout=10)
    print(f"  worker pid {proc.pid} killed uncatchably (exit {proc.returncode})")

    applied, in_doubt = journal_state()
    gateway = FileGateway(GATEWAY)
    print(f"\n  journal: {applied} effect(s) recorded, {len(in_doubt)} in doubt")
    print(f"  gateway: {gateway.raise_calls} notification(s) actually raised")
    print("\n  the gap is the point — the gateway did work the journal cannot account for")

    banner("2 · RESTART AND RECONCILE")
    gateway = FileGateway(GATEWAY)
    executor = Executor(Journal(JOURNAL), gateway, now=lambda: CLOCK, mode=ExecutionMode.LIVE)
    report = executor.recover()
    torn_note = (
        "" if report.torn_bytes_discarded else "  (kill landed between writes this run)"
    )
    print(f"\n  torn bytes discarded  {report.torn_bytes_discarded}{torn_note}")
    print(f"  intents in doubt      {report.in_doubt_found}")
    print(f"  adopted from gateway  {report.adopted}")
    print(f"  never performed       {report.never_performed}")
    print("\n  resolved by asking the gateway, never by retrying")

    banner("3 · RE-RUN THE IDENTICAL BATCH")
    print()
    raises_before = gateway.raise_calls
    executor.begin_run(RUN_ID)
    statuses: list[str] = []
    for state, action, ctx in build_batch():
        outcome = executor.submit(action, state, CLOCK, ctx)
        statuses.append(outcome.status)
        print(f"  {ctx.mandate_id}  {outcome.status:<9} {outcome.detail}")
    executor.end_run()

    new_raises = gateway.raise_calls - raises_before

    banner("4 · VERDICT")
    total_records = Journal(JOURNAL).verify()
    applied, in_doubt = journal_state()
    duplicates = statuses.count("DUPLICATE")
    cancelled = gateway.cancelled_sequence_ids

    batch_size = len(build_batch())
    per_mandate = Counter(
        rec.body["mandate_id"]
        for rec in Journal(JOURNAL)
        if rec.kind is RecordKind.INTENT
    )
    applied_keys = Ledger.from_journal(Journal(JOURNAL)).applied

    checks = [
        ("hash chain verifies",
         total_records > 0, f"{total_records} records"),
        ("one effect per mandate, no more",
         len(applied_keys) == batch_size, f"{len(applied_keys)} of {batch_size}"),
        ("notifications raised in total",
         gateway.raise_calls == batch_size, f"{gateway.raise_calls}, expected {batch_size}"),
        ("re-run raised only the outstanding ones",
         new_raises == batch_size - duplicates, f"{new_raises}, expected {batch_size - duplicates}"),
        ("already-done work was skipped",
         duplicates == KILL_AT + 1, f"{duplicates} duplicates, expected {KILL_AT + 1}"),
        ("no mandate was committed twice",
         max(per_mandate.values()) == 1, f"max intents for one mandate: {max(per_mandate.values())}"),
        ("nothing left in doubt",
         not in_doubt, f"{len(in_doubt)}"),
        ("no notification cancelled (C8)",
         cancelled == [], f"{cancelled}"),
    ]
    print()
    ok = True
    for label, passed, detail in checks:
        mark = "PASS" if passed else "FAIL"
        ok &= bool(passed)
        print(f"  [{mark}]  {label:<34} {detail}")

    print(f"\n  replay:  python -m mandate_recovery.act.replay {JOURNAL} --run {RUN_ID}")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
