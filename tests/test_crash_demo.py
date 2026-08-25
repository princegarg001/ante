"""The crash demo, run for real in CI.

A demonstration that only works on the author's laptop is a liability, and a
demo that quietly stops working is worse than no demo — it gets discovered
live. So the whole thing runs end to end here: a real subprocess, a real
uncatchable kill, a real restart against the same journal and gateway records.

Marked slow because it spawns a process and waits on the filesystem. It is not
optional; `make check` runs it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.slow
def test_crash_demo_survives_a_real_kill() -> None:
    proc = subprocess.run(
        [sys.executable, "-u", "-m", "demo.crash_demo"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        encoding="utf-8",
        errors="replace",
    )
    output = proc.stdout + proc.stderr

    assert proc.returncode == 0, output
    assert "FAIL" not in output, output

    # The two claims the demo exists to make, asserted here rather than trusted
    # to a human reading the terminal.
    assert "no notification cancelled (C8)" in output
    assert "no mandate was committed twice" in output
