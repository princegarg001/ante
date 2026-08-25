"""The money path.

The headline property is the crash test: a process that dies in the window
between raising a pre-debit notification and recording that it did so must, on
restart, neither raise a second one nor lose the first. Under C8 a second
notification cancels the first, so the naive recovery strategy is not merely
wasteful — it silently pushes the execution out by a day.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from mandate_recovery.act import (
    BlastRadius,
    CeilingExceeded,
    DryRunGateway,
    ExecutionMode,
    Executor,
    FakeGateway,
    Journal,
    KillSwitch,
)
from mandate_recovery.act.executor import DecisionContext, Ledger, idempotency_key
from mandate_recovery.act.gateway import CrashInjected
from mandate_recovery.act.journal import RecordKind
from mandate_recovery.core.money import rupees
from mandate_recovery.core.types import Commit, MandateStatus
from tests.conftest import ORIGIN, make_state

CTX = DecisionContext(
    mandate_id="MND_0001",
    cycle_id="2026-09",
    attempt_index=0,
    justification="salary lands on the 1st; committing to the 06:30 slot",
)


def ctx(attempt: int, mandate_id: str = "MND_0001") -> DecisionContext:
    """A distinct decision. `replace` rather than `__dict__` — DecisionContext is
    a slotted frozen dataclass and has no instance dict."""
    return replace(CTX, mandate_id=mandate_id, attempt_index=attempt)


def commit(hours: float = 24, amount: int = rupees(499)) -> Commit:
    return Commit(execute_at=ORIGIN + timedelta(hours=hours), amount_paise=amount)


def build(tmp_path, gateway=None, **kw) -> Executor:
    ex = Executor(
        Journal(tmp_path / "journal.jsonl"),
        gateway if gateway is not None else FakeGateway(),
        now=lambda: ORIGIN,
        mode=kw.pop("mode", ExecutionMode.LIVE),
        **kw,
    )
    ex.recover()
    return ex


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #


def test_dry_run_is_the_default(tmp_path) -> None:
    """Running the system by accident must not move money."""
    ex = Executor(
        Journal(tmp_path / "j.jsonl"), DryRunGateway(), now=lambda: ORIGIN
    )
    assert ex.mode is ExecutionMode.DRY_RUN


def test_dry_run_is_recorded_so_a_replay_cannot_confuse_the_two(tmp_path) -> None:
    ex = build(tmp_path, DryRunGateway(), mode=ExecutionMode.DRY_RUN)
    ex.begin_run("r1")
    ex.submit(commit(), make_state(), ORIGIN, CTX)
    ex.end_run()

    modes = {
        r.body["mode"]
        for r in ex.journal
        if r.kind in (RecordKind.RUN_START, RecordKind.INTENT)
    }
    assert modes == {"DRY_RUN"}


# --------------------------------------------------------------------------- #
# The constraint layer is re-consulted at the boundary
# --------------------------------------------------------------------------- #


def test_an_action_the_constraint_layer_forbids_is_never_executed(tmp_path) -> None:
    """Defence in depth. A bug in the policy must not become an illegal debit."""
    gw = FakeGateway()
    ex = build(tmp_path, gw)
    ex.begin_run("r1")

    outcome = ex.submit(commit(hours=34), make_state(), ORIGIN, CTX)  # 10:00 → peak

    assert outcome.status == "VETOED"
    assert "C2" in outcome.detail
    assert gw.raise_calls == 0


def test_a_veto_is_journalled_with_its_rule(tmp_path) -> None:
    ex = build(tmp_path)
    ex.begin_run("r1")
    ex.submit(commit(), make_state(status=MandateStatus.REVOKED), ORIGIN, CTX)

    skips = [r for r in ex.journal if r.kind is RecordKind.SKIPPED]
    assert len(skips) == 1
    assert skips[0].body["reason"] == "VETOED"
    assert "C12" in skips[0].body["detail"]


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


def test_the_same_decision_twice_performs_one_effect(tmp_path) -> None:
    gw = FakeGateway()
    ex = build(tmp_path, gw)
    ex.begin_run("r1")

    first = ex.submit(commit(), make_state(), ORIGIN, CTX)
    second = ex.submit(commit(), make_state(), ORIGIN, CTX)

    assert first.status == "APPLIED"
    assert second.status == "DUPLICATE"
    assert gw.raise_calls == 1


def test_the_key_is_the_decision_not_the_attempt(tmp_path) -> None:
    """Excludes the run id and the wall clock, so a restarted run reaches the
    same key and the second attempt is a no-op."""
    a = idempotency_key("MND_1", "2026-09", 0, commit())
    b = idempotency_key("MND_1", "2026-09", 0, commit())
    assert a == b

    assert idempotency_key("MND_1", "2026-09", 1, commit()) != a       # attempt
    assert idempotency_key("MND_2", "2026-09", 0, commit()) != a       # mandate
    assert idempotency_key("MND_1", "2026-10", 0, commit()) != a       # cycle
    assert idempotency_key("MND_1", "2026-09", 0, commit(30)) != a     # time
    assert idempotency_key("MND_1", "2026-09", 0, commit(24, rupees(299))) != a


def test_a_declined_presentation_still_counts_as_applied(tmp_path) -> None:
    """C1 counts presentations, not successes. A declined attempt consumed the
    slot and must never be silently repeated."""
    key = idempotency_key(CTX.mandate_id, CTX.cycle_id, CTX.attempt_index, commit())
    gw = FakeGateway(decline_on={key})
    ex = build(tmp_path, gw)
    ex.begin_run("r1")
    ex.submit(commit(), make_state(), ORIGIN, CTX)

    reopened = build(tmp_path, gw)
    assert key in reopened.ledger.applied


# --------------------------------------------------------------------------- #
# The crash test
# --------------------------------------------------------------------------- #


def test_crash_in_the_in_doubt_window_leaves_an_unresolved_intent(tmp_path) -> None:
    key = idempotency_key(CTX.mandate_id, CTX.cycle_id, CTX.attempt_index, commit())
    gw = FakeGateway(crash_on={key})
    ex = build(tmp_path, gw)
    ex.begin_run("r1")

    with pytest.raises(CrashInjected):
        ex.submit(commit(), make_state(), ORIGIN, CTX)

    # The effect landed at the gateway; the log records only the intent.
    assert gw.effect_count == 1
    ledger = Ledger.from_journal(Journal(tmp_path / "journal.jsonl"))
    assert key in ledger.in_doubt
    assert key not in ledger.applied


def test_restart_adopts_the_effect_and_never_raises_a_second_notification(
    tmp_path,
) -> None:
    """The property the whole design exists for.

    Under C8 a second notification cancels the first. A recovery strategy of
    "retry anything unfinished" would therefore destroy the pending commitment
    and push execution out by a day — while looking like a transient error.
    """
    key = idempotency_key(CTX.mandate_id, CTX.cycle_id, CTX.attempt_index, commit())
    gw = FakeGateway(crash_on={key})

    crashed = build(tmp_path, gw)
    crashed.begin_run("night-batch")
    with pytest.raises(CrashInjected):
        crashed.submit(commit(), make_state(), ORIGIN, CTX)

    # --- new process, same journal, same gateway ---
    gw.crash_on.clear()
    restarted = Executor(
        Journal(tmp_path / "journal.jsonl"), gw, now=lambda: ORIGIN,
        mode=ExecutionMode.LIVE,
    )
    report = restarted.recover()

    assert report.in_doubt_found == 1
    assert report.adopted == 1
    assert report.never_performed == 0

    restarted.begin_run("night-batch")
    outcome = restarted.submit(commit(), make_state(), ORIGIN, CTX)

    assert outcome.status == "DUPLICATE"
    assert gw.raise_calls == 1, "a second notification was raised"
    assert gw.cancelled_sequence_ids == [], "the pending notification was cancelled"
    assert gw.pending_for("MND_0001") is not None


def test_a_recovered_effect_is_filed_against_the_run_that_intended_it(tmp_path) -> None:
    """Regression: recovery originally wrote outcomes under a synthetic
    "recovery" run id. A run-scoped replay then filtered that record out and
    reported the intent as permanently in doubt — the audit trail said the
    system had lost track of an effect it had in fact reconciled.

    The outcome belongs to the run that meant to do it. The `resolution` field
    records that it arrived late.
    """
    key = idempotency_key(CTX.mandate_id, CTX.cycle_id, CTX.attempt_index, commit())
    gw = FakeGateway(crash_on={key})

    crashed = build(tmp_path, gw)
    crashed.begin_run("night-batch")
    with pytest.raises(CrashInjected):
        crashed.submit(commit(), make_state(), ORIGIN, CTX)

    gw.crash_on.clear()
    Executor(
        Journal(tmp_path / "journal.jsonl"), gw, now=lambda: ORIGIN,
        mode=ExecutionMode.LIVE,
    ).recover()

    effects = [r for r in Journal(tmp_path / "journal.jsonl") if r.kind is RecordKind.EFFECT]
    assert len(effects) == 1
    assert effects[0].run_id == "night-batch"
    assert effects[0].body["resolution"] == "recovered"
    # And it carries the amount, or the blast-radius counters would under-count
    # exactly the spend a crash made hardest to see.
    assert effects[0].body["amount_paise"] == rupees(499)


def test_recovery_closes_an_intent_whose_effect_never_happened(tmp_path) -> None:
    """The other half. If the gateway never performed it, the decision is free to
    be taken again — but that must be established by asking, not assuming."""
    path = tmp_path / "journal.jsonl"
    j = Journal(path)
    j.open()
    j.append(RecordKind.INTENT, "r1", "2026-09-01T00:00:00+05:30", {"idem_key": "ghost"})

    gw = FakeGateway()
    ex = Executor(Journal(path), gw, now=lambda: ORIGIN, mode=ExecutionMode.LIVE)
    report = ex.recover()

    assert report.in_doubt_found == 1
    assert report.never_performed == 1
    assert "ghost" not in ex.ledger.applied
    assert "ghost" not in ex.ledger.in_doubt


def test_submitting_while_in_doubt_refuses_rather_than_guessing(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    j = Journal(path)
    j.open()
    key = idempotency_key(CTX.mandate_id, CTX.cycle_id, CTX.attempt_index, commit())
    j.append(RecordKind.INTENT, "r1", "2026-09-01T00:00:00+05:30", {"idem_key": key})

    ex = Executor(Journal(path), FakeGateway(), now=lambda: ORIGIN, mode=ExecutionMode.LIVE)
    ex.journal.open()
    ex.ledger = Ledger.from_journal(ex.journal)     # deliberately skipping recover()
    ex.begin_run("r1")

    with pytest.raises(RuntimeError, match="in doubt"):
        ex.submit(commit(), make_state(), ORIGIN, CTX)


def test_a_crash_writes_no_outcome_record(tmp_path) -> None:
    """Claiming an effect failed when it may have succeeded is a lie the log
    cannot take back. Silence is the honest state."""
    key = idempotency_key(CTX.mandate_id, CTX.cycle_id, CTX.attempt_index, commit())
    gw = FakeGateway(crash_on={key})
    ex = build(tmp_path, gw)
    ex.begin_run("r1")
    with pytest.raises(CrashInjected):
        ex.submit(commit(), make_state(), ORIGIN, CTX)

    assert [r.kind for r in ex.journal].count(RecordKind.EFFECT) == 0


# --------------------------------------------------------------------------- #
# Blast radius
# --------------------------------------------------------------------------- #


def test_execution_cap_stops_the_run(tmp_path) -> None:
    gw = FakeGateway()
    ex = build(tmp_path, gw, blast_radius=BlastRadius(max_executions=2))
    ex.begin_run("r1")

    for i in range(2):
        ex.submit(commit(), make_state(), ORIGIN, ctx(i))

    with pytest.raises(CeilingExceeded, match="execution cap"):
        ex.submit(commit(), make_state(), ORIGIN, ctx(2))
    assert gw.raise_calls == 2


def test_value_cap_is_checked_before_the_effect_not_after(tmp_path) -> None:
    gw = FakeGateway()
    ex = build(tmp_path, gw, blast_radius=BlastRadius(max_paise_attempted=rupees(600)))
    ex.begin_run("r1")

    ex.submit(commit(amount=rupees(499)), make_state(), ORIGIN, CTX)
    with pytest.raises(CeilingExceeded, match="value cap"):
        ex.submit(commit(amount=rupees(499)), make_state(), ORIGIN, ctx(1))

    assert gw.raise_calls == 1, "the effect ran before the ceiling was checked"


def test_ceilings_survive_a_restart_within_the_same_run(tmp_path) -> None:
    """Otherwise a crash loop would spend the blast radius once per restart."""
    gw = FakeGateway()
    ex = build(tmp_path, gw, blast_radius=BlastRadius(max_executions=2))
    ex.begin_run("r1")
    ex.submit(commit(), make_state(), ORIGIN, CTX)

    resumed = build(tmp_path, gw, blast_radius=BlastRadius(max_executions=2))
    resumed.begin_run("r1")
    assert resumed.ledger.executions["r1"] == 1


# --------------------------------------------------------------------------- #
# Kill switch
# --------------------------------------------------------------------------- #


def test_kill_switch_halts_new_work(tmp_path) -> None:
    gw = FakeGateway()
    ks = KillSwitch(tmp_path / "STOP")
    ex = build(tmp_path, gw, kill_switch=ks)
    ex.begin_run("r1")

    ex.submit(commit(), make_state(), ORIGIN, CTX)
    ks.engage()
    outcome = ex.submit(commit(hours=30), make_state(), ORIGIN, ctx(1))

    assert outcome.status == "KILLED"
    assert gw.raise_calls == 1


def test_kill_switch_can_be_engaged_from_outside_the_process(tmp_path) -> None:
    """An operator halting a misbehaving agent should not need a deploy."""
    flag = tmp_path / "STOP"
    ks = KillSwitch(flag)
    assert not ks.is_set()
    flag.write_text("engaged\n", encoding="utf-8")
    assert ks.is_set()
