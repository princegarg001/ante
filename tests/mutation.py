"""Mutation testing for the constraint layer.

A compliance test suite that passes proves nothing on its own — a suite that
asserts very little also passes. This mutates the *regulation itself* and checks
that the suite notices.

Each mutation is a plausible misreading of a circular: an aperture with no upper
bound (which is exactly what the first draft of the build plan assumed), a peak
window off by an hour, a retry cap off by one, a serialization rule quietly
dropped. Every one of them must turn the suite red. A surviving mutant is a hole
in the compliance argument, not a curiosity.

Run:  python -m tests.mutation
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: The suite that exists to catch a mutated regulation. Deliberately narrower
#: than the whole test run.
#:
#: Every mutation below edits the constraint layer or the model checker. Running
#: the simulator, model and evaluation tests against each one added roughly
#: fifteen minutes to CI to re-fit gradient-boosted trees eleven times, and none
#: of them is capable of detecting a changed peak window — that is what these
#: files are for. Scoping the run to them is a statement about which suite is
#: being tested for teeth, not a shortcut around the check.
COMPLIANCE_SUITE: tuple[str, ...] = (
    "tests/test_clock.py",
    "tests/test_constraints.py",
    "tests/test_properties.py",
    "tests/test_modelcheck.py",
    "tests/test_regulatory_constants.py",
)
RULES = ROOT / "mandate_recovery" / "constraints" / "rules.py"
CLOCK = ROOT / "mandate_recovery" / "core" / "clock.py"
CHECK = ROOT / "mandate_recovery" / "constraints" / "modelcheck.py"


@dataclass(frozen=True)
class Mutation:
    mid: str
    description: str
    path: Path
    pattern: str
    replacement: str
    rule: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "M1",
        "Pre-debit notification aperture loses its 48h upper bound",
        RULES,
        r"PDN_MAX_LEAD: Final\[timedelta\] = timedelta\(hours=48\)",
        "PDN_MAX_LEAD: Final[timedelta] = timedelta(hours=999)",
        "C5",
    ),
    Mutation(
        "M2",
        "Pre-debit notification minimum drops to 12h",
        RULES,
        r"PDN_MIN_LEAD: Final\[timedelta\] = timedelta\(hours=24\)",
        "PDN_MIN_LEAD: Final[timedelta] = timedelta(hours=12)",
        "C5",
    ),
    Mutation(
        "M3",
        "Retry cap off by one (4 -> 5 presentations)",
        RULES,
        r"MAX_ATTEMPTS: Final\[int\] = 4",
        "MAX_ATTEMPTS: Final[int] = 5",
        "C1",
    ),
    Mutation(
        "M4",
        "Evening peak window closes at 20:30 instead of 21:30",
        CLOCK,
        r"\(time\(17, 0\), time\(21, 30\)\)",
        "(time(17, 0), time(20, 30))",
        "C2",
    ),
    Mutation(
        "M5",
        "Morning peak window never opens",
        CLOCK,
        r"\(time\(10, 0\), time\(13, 0\)\)",
        "(time(10, 0), time(10, 0))",
        "C2",
    ),
    Mutation(
        "M6",
        "One-pending-PDN serialization rule disabled",
        RULES,
        r"if isinstance\(a, Commit\) and s\.pending_pdn is not None:",
        "if False:",
        "C8",
    ),
    Mutation(
        "M7",
        "Terminal-cause ratchet disabled (retries against revoked mandates)",
        RULES,
        r"if isinstance\(a, Commit\) and s\.cause in TERMINAL_CAUSES:",
        "if False:",
        "RATCHET",
    ),
    Mutation(
        "M8",
        "AFA-free ceiling check disabled",
        RULES,
        r"if a\.amount_paise > ceiling:",
        "if False:",
        "C15",
    ),
    Mutation(
        "M9",
        "Mandate-cap check disabled (debit above authorised maximum)",
        RULES,
        r"if isinstance\(a, Commit\) and a\.amount_paise > s\.max_amount_paise:",
        "if False:",
        "C19",
    ),
    Mutation(
        "M10",
        "Peak-window veto disabled entirely",
        RULES,
        r"if isinstance\(a, Commit\) and not is_non_peak\(a\.execute_at\):",
        "if False:",
        "C2",
    ),
    Mutation(
        "M11",
        "Model checker's independent spec agrees with the code by construction",
        CHECK,
        r"\(10 \* 60, 13 \* 60\),          # 10:00-13:00",
        "(10 * 60, 10 * 60),          # mutated",
        "meta",
    ),
)


def _run_suite() -> bool:
    """True if the suite is green. Exit code, not log scraping — a truncated
    traceback once made a red run look green here."""
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "-m", "not slow",
            "-p", "no:cacheprovider", *COMPLIANCE_SUITE,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mutate the regulation; check the suite notices.")
    ap.add_argument("--only", help="run a single mutation by id, e.g. M4")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass

    selected = [m for m in MUTATIONS if not args.only or m.mid == args.only]

    print("=" * 78)
    print("MUTATION TESTING — does the compliance suite actually have teeth?")
    print("=" * 78)
    print()
    print("suite under test: " + " ".join(COMPLIANCE_SUITE))

    if not _run_suite():
        print("\nBaseline suite is already failing. Fix that before mutating.")
        return 2
    print("\nbaseline: green\n")

    started = time.perf_counter()
    survivors: list[Mutation] = []

    for m in selected:
        original = m.path.read_text(encoding="utf-8")
        mutated, count = re.subn(m.pattern, m.replacement, original, count=1)
        if count != 1:
            print(f"  {m.mid:<4} SKIPPED  pattern not found — mutation is stale")
            survivors.append(m)
            continue
        m.path.write_text(mutated, encoding="utf-8")
        try:
            caught = not _run_suite()
        finally:
            m.path.write_text(original, encoding="utf-8")
        flag = "caught  " if caught else "SURVIVED"
        print(f"  {m.mid:<4} {flag} [{m.rule:<7}] {m.description}")
        if not caught:
            survivors.append(m)

    elapsed = time.perf_counter() - started
    killed = len(selected) - len(survivors)
    print("\n" + "=" * 78)
    print(f"{killed}/{len(selected)} mutants killed in {elapsed:.0f}s")
    if survivors:
        print("\nSurvivors — each one is a regulation the suite would not notice breaking:")
        for m in survivors:
            print(f"  {m.mid} [{m.rule}] {m.description}")
    else:
        print("Every mutated regulation turned the suite red.")
    print("=" * 78)
    return 0 if not survivors else 1


if __name__ == "__main__":
    raise SystemExit(main())
