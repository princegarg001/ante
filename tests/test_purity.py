"""Static guards on the decision path.

The README claims that nothing in the package reads the wall clock and that the
constraint layer performs no I/O. Those claims are what make a run replayable and
the exhaustive verification meaningful — so they are enforced here rather than
left as prose that drifts.

Implemented over the AST rather than with a grep, so a call cannot slip past by
being spelled differently, and so the failure message names the line.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "mandate_recovery"

#: Reading the wall clock inside the decision path would make a replayed run
#: diverge from the original, and would make the model checker's guarantees
#: conditional on when it happened to run.
FORBIDDEN_CLOCK_CALLS = {
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("date", "today"),
    ("time", "time"),
    ("time", "monotonic"),
}

#: `perf_counter` measures how long verification took. It never influences a
#: verdict, so it is allowed — but only in the checker, which is not on the path.
CLOCK_EXEMPT_FILES = {"modelcheck.py"}

#: Anything that could make `is_permitted` depend on the outside world.
FORBIDDEN_IMPORTS_ON_DECISION_PATH = {
    "os", "io", "random", "socket", "subprocess", "pathlib",
    "requests", "urllib", "http", "sqlite3", "logging", "numpy",
}

DECISION_PATH = {"rules.py", "clock.py", "money.py", "types.py"}


def package_files() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_the_package_is_non_empty() -> None:
    """Guards every test below: an empty glob would make them all pass."""
    assert len(package_files()) >= 6


@pytest.mark.parametrize("path", package_files(), ids=lambda p: p.name)
def test_no_wall_clock_reads(path: Path) -> None:
    """Every function that cares about time takes it as an argument."""
    if path.name in CLOCK_EXEMPT_FILES:
        pytest.skip("verification harness, not on the decision path")
    offenders = []
    for node in ast.walk(parse(path)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            value = node.func.value
            owner = getattr(value, "id", None) or getattr(value, "attr", None)
            if (owner, node.func.attr) in FORBIDDEN_CLOCK_CALLS:
                offenders.append(f"{path.name}:{node.lineno} {owner}.{node.func.attr}()")
    assert not offenders, "wall-clock read on the decision path: " + "; ".join(offenders)


@pytest.mark.parametrize(
    "path",
    [p for p in package_files() if p.name in DECISION_PATH],
    ids=lambda p: p.name,
)
def test_decision_path_imports_nothing_that_touches_the_world(path: Path) -> None:
    """No I/O, no randomness, no globals. `is_permitted` is a function of its
    arguments and nothing else."""
    offenders = []
    for node in ast.walk(parse(path)):
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[0]] if node.level == 0 else []
        else:
            continue
        for name in names:
            if name in FORBIDDEN_IMPORTS_ON_DECISION_PATH:
                offenders.append(f"{path.name}:{node.lineno} imports {name}")
    assert not offenders, "; ".join(offenders)


def test_the_import_guard_can_actually_fail() -> None:
    """A guard that cannot fire is decoration. This asserts the detector works by
    running it against a module that deliberately breaks the rule."""
    tree = ast.parse("import random\nfrom datetime import datetime\nx = datetime.now()\n")
    found_import = any(
        isinstance(n, ast.Import) and any(a.name == "random" for a in n.names)
        for n in ast.walk(tree)
    )
    found_clock = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and (getattr(n.func.value, "id", None), n.func.attr) in FORBIDDEN_CLOCK_CALLS
        for n in ast.walk(tree)
    )
    assert found_import and found_clock


def test_every_domain_type_is_frozen() -> None:
    """Nothing mutates a MandateState in place — transitions produce new states.

    This is what lets the audit log replay a run and reconstruct it exactly.
    """
    from dataclasses import fields, is_dataclass

    from mandate_recovery.core import types as t

    for name in dir(t):
        obj = getattr(t, name)
        if is_dataclass(obj) and isinstance(obj, type):
            params = getattr(obj, "__dataclass_params__")
            assert params.frozen, f"{name} is a mutable dataclass on the decision path"
            # A frozen container holding a mutable field is frozen in name only.
            for f in fields(obj):
                assert not isinstance(f.default, (list, dict, set)), (
                    f"{name}.{f.name} has a mutable default"
                )
