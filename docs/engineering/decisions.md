# Design decisions

Decisions that were genuinely contested, with the reasoning. Documented so they can be argued
with rather than discovered by reading source.

## Regulatory and operational vetoes are separate registries

The constraint layer distinguishes <span class="rule reg">REGULATORY</span> rules, which trace
to an NPCI or RBI instrument, from <span class="rule ops">OPERATIONAL</span> guards, which are
merchant policy.

The headline compliance claim counts regulatory rules only. If the two were mixed, "zero
violations across 2.5 million triples" could be quietly inflated by counting internal sanity
checks, and the number would stop meaning anything.

The split also decides *which* veto gets reported when an action is illegal several ways at
once. Regulatory rules are evaluated first:

```python
def test_regulatory_vetoes_are_reported_ahead_of_operational_ones():
    """When an action is illegal for several reasons, the reported one must be
    the reason a regulator would care about, not the one ops would."""
    s = make_state(status=MandateStatus.REVOKED)
    action = Commit(execute_at=ORIGIN + timedelta(hours=1, minutes=7), amount_paise=-5)
    assert is_permitted(action, s, ORIGIN).kind is RuleKind.REGULATORY
```

## Money is an integer count of paise, everywhere

No floats touch money at any point. A float rupee amount is a rounding difference waiting to
appear between what the policy valued, what the constraint layer checked, and what the ledger
recorded — three numbers that must be identical for an audit trail to mean anything.

`to_rupees()` exists but is documented as lossy and presentation-only.

## Nothing reads the wall clock

Every function that cares about time takes it as an argument. There is no `datetime.now()`
anywhere in the package.

Two things depend on this. Replay: reconstructing a run requires that the same inputs produce
the same decisions. And verification: if `is_permitted` could consult the clock, the
exhaustive check would only hold for the moment it happened to run.

Enforced over the AST, not by discipline:

```python
FORBIDDEN_CLOCK_CALLS = {
    ("datetime", "now"), ("datetime", "utcnow"),
    ("date", "today"), ("time", "time"), ("time", "monotonic"),
}
```

`time.perf_counter` is exempt in the model checker alone — it measures how long verification
took and never influences a verdict.

## Naive datetimes are rejected outright

```python
if dt.tzinfo is None:
    raise ValueError(f"naive datetime not allowed in the execution clock: {dt!r}")
```

An ambiguous instant in a payments system is a defect, not a convenience. The peak windows are
defined in IST; a naive datetime silently interpreted as UTC would place a 10:30 IST execution
— squarely inside the morning peak — into a permitted window.

There is a test for exactly that:

```python
def test_peakness_is_evaluated_in_ist_not_utc():
    """05:00 UTC is 10:30 IST — peak. A UTC-naive implementation would miss this."""
```

IST is implemented as a fixed UTC+05:30 offset rather than a `zoneinfo` lookup. India has no
daylight saving, so the offset is exact, and it removes a `tzdata` dependency that Windows
images frequently lack.

## The slot grid is derived, never hardcoded

`non_peak_slots_per_day()` returns 33 by *computing* it from `PEAK_WINDOWS`:

```python
def non_peak_slots_per_day() -> int:
    """33, under the 30-minute grid. Computed, not asserted, so the constant can
    never drift away from PEAK_WINDOWS."""
```

If the peak windows are ever corrected during primary-source verification, every derived
quantity moves with them and nothing needs hunting down.

## Re-planning is explicit

Discussed under [action space](/system/action-space#cancelpending-must-be-explicit). The rails
would let a new notification silently cancel the previous one; the system refuses to use that,
so abandoning a commitment appears in the audit log as a decision rather than a side effect.

## Exact DP rather than reinforcement learning

The per-mandate problem is roughly 30,000 nodes. Backward induction solves it exactly in
sub-milliseconds, deterministically, and produces the value function the slot auction needs
for its bids.

RL would be slower, non-deterministic, unverifiable, and would then have to be defended. It
would be a downgrade dressed as sophistication.

## A dead rule was kept, with a test explaining why

<span class="rule reg">C7</span> — the 23:50 notification cut-off — turns out to be
**unreachable** on the 30-minute slot grid. The 24-hour minimum lead means a notification
raised at or after 23:50 can only target an execution at or after 23:50 the following day, and
the grid has no aligned instant in `[23:50, 24:00)`. C5 dominates C7 entirely.

The rule was not deleted. It binds again the moment the grid gets finer, and defence in depth
on a money path is cheap. What matters is that the situation is *documented as a consequence
rather than left as apparently-dead code*:

```python
def test_c7_is_unreachable_on_the_thirty_minute_grid():
    """A documented consequence, not an oversight."""
    for minutes in range(24 * 60, 48 * 60 + 1, 30):
        fired = vetoed_by(action, s, clock)
        if "C7" in fired:
            assert "OPS-ALIGN" in fired, "C7 fired on an aligned slot — grid assumption broken"
```

The test also fails loudly if the grid assumption is ever broken by a future change.

## Every emitted rule id must be registered

A veto with no registry entry cannot be rendered into an audit log or cited in a pitch, so it
must not be possible to emit one:

```python
def test_every_emitted_rule_id_is_registered(state):
    for v in all_vetoes(probe, s, ORIGIN):
        assert v.rule_id in RULES, f"unregistered rule id {v.rule_id}"
        assert v.kind is RULES[v.rule_id][0]
```

## An optimisation that was justified rather than assumed

The reachability search originally re-derived, for every reachable state, that no commit is
permitted while a notification is pending. That cost 20 of the 21 seconds the search took.

Skipping it is sound — but only because Claim 1 sweeps that exhaustively across both pending
states and all offsets. The comment says so, because an optimisation on a verification path
needs its justification recorded next to it:

```python
# Skipped entirely while something is in flight: C8 vetoes every commit
# regardless of the execution offset, and CLAIM 1 already sweeps that
# exhaustively across both pending states and all offsets.
```

Result: 20.5s → 0.52s, with an identical reachable state count.
