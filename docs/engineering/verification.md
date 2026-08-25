# Verification

::: tip Status
Built and running in CI on every push.
:::

Most submissions will assert compliance with a handful of unit tests. This layer makes a
stronger claim, in three levels, and each level exists because the level below it can pass
while being wrong.

<div class="stat-grid">
  <div class="stat ok"><span class="v">2,511,760</span><span class="k">triples enumerated</span></div>
  <div class="stat ok"><span class="v">54,889</span><span class="k">reachable states</span></div>
  <div class="stat ok"><span class="v">0</span><span class="k">violations</span></div>
  <div class="stat ok"><span class="v">11 / 11</span><span class="k">mutants killed</span></div>
  <div class="stat"><span class="v">106</span><span class="k">tests</span></div>
  <div class="stat"><span class="v">15 s</span><span class="k">full verification</span></div>
</div>

## Level 1 · Exhaustive enumeration

Property-based testing samples. This enumerates.

```
$ make verify

CLAIM 1  state x action sweep
  triples enumerated   2,511,760
  permitted            38,818
  vetoed               2,472,942
  counterexamples      0
  elapsed              14.64s

CLAIM 2  reachability under permitted actions
  states reached       54,889
  max attempts seen    4 (cap 4)
  max pending PDNs     1 (cap 1)
  cap actually binding yes
  elapsed              0.72s
```

**Claim 1** sweeps a bounded but complete grid of `(state, action, clock)` triples and asserts
that every action the layer permits satisfies every regulatory invariant. Dimensions swept:
every 30-minute clock slot across the day (C7 needs all times of day, including the 23:50
boundary), execution offsets from −2h to +52h straddling both aperture edges, eight amount
levels straddling zero and every ceiling, all attempt counts, both pending states, and both
variable-amount settings.

**Claim 2** breadth-first searches the entire reachable state space under an adversarial policy
— from every state, *every* permitted commit is followed — and asserts that no sequence of
permitted actions can overrun the budget or hold two commitments at once.

### The checker restates the regulation independently

The invariants in `modelcheck.py` are written from the circular, not imported from
`rules.py`. The peak-window literals are deliberately duplicated:

```python
# COMPLIANCE.md C2 — peak windows as (start_minute, end_minute), half-open.
# Written out again rather than imported, on purpose.
_PEAK_MINUTES = (
    (10 * 60, 13 * 60),          # 10:00-13:00
    (17 * 60, 21 * 60 + 30),     # 17:00-21:30
)
```

A checker that imported the implementation's own predicates would only prove that the code
equals itself. Duplicating the specification means the two can disagree, and a disagreement
fails the build.

### Vacuity is reported, not hidden

```
cap actually binding yes
```

Each attempt costs at least 24 hours of notification lead ([C5](/constraints/)), so four
attempts require at least four days of horizon. A run over a shorter window can never reach the
retry cap, and the C1 claim would pass *vacuously* — true, and meaningless.

The checker therefore reports whether the cap was actually driven to its limit, and treats a
non-binding run as `INCONCLUSIVE` rather than `VERIFIED`:

```python
@property
def cap_binding(self) -> bool:
    """A proof that cannot fail is not a proof, so this is reported alongside it."""
    return self.max_attempts_seen == MAX_ATTEMPTS
```

This was not a hypothetical. The first implementation ran a two-day horizon and reported
`max attempts seen 1` — the claim was passing without ever testing anything.

### It also guards against a degenerate layer

A constraint layer that vetoed *everything* would satisfy Claim 1 perfectly. So the sweep
asserts that both verdicts are well represented:

```python
def test_the_sweep_actually_exercises_both_verdicts(swept):
    assert swept.permitted > 1_000
    assert swept.vetoed > swept.permitted
```

## Level 2 · Property-based tests

Hypothesis wanders unbounded where the grid is bounded — arbitrary months, unaligned instants,
absurd amounts, every status and cause combination.

The central property:

```python
@given(state=states(), clock=clocks, lead=lead_minutes, amount=amounts)
def test_permitted_implies_lawful(state, clock, lead, amount):
    action = Commit(execute_at=clock + timedelta(minutes=lead), amount_paise=amount)
    if is_permitted(action, state, clock).allowed:
        assert _inv_violations(action, state, clock) == []
```

Specialisations are kept separate so a failure names the rule it broke: no permitted execution
in a peak window, no permitted commit over the budget, none while a notification is pending,
no lead outside the aperture.

### Sequences, not just actions

A budget overrun is a property of a *sequence*. A stateful machine drives one mandate through
thousands of random sequences of permitted actions, applying only what the layer allows, so
any invariant failure is a failure of the layer rather than of the test:

```python
class MandateLifecycle(RuleBasedStateMachine):
    @invariant()
    def budget_is_never_exceeded(self):
        assert self.state.attempts_used <= MAX_ATTEMPTS
        assert self.presentations <= MAX_ATTEMPTS

    @invariant()
    def every_commitment_is_lawful(self):
        pdn = self.state.pending_pdn
        if pdn is None:
            return
        assert is_non_peak(pdn.execute_at)
        assert timedelta(hours=24) <= pdn.execute_at - pdn.notified_at <= timedelta(hours=48)
```

The machine models a rejected notification as costing calendar time but *not* an attempt,
because under [C6](/constraints/) no presentation was made.

## Level 3 · Mutation testing

The first two levels can both pass while asserting very little. So the regulation itself is
mutated and the suite must notice. All eleven mutants are caught — and the first run caught
only nine, which is the most useful thing that has happened in this build so far.

That story is worth its own page: [Mutation testing](/engineering/mutation).

## Static guards

The claims that make replay meaningful are enforced over the AST rather than left as prose:
no wall-clock read anywhere on the decision path, no I/O or randomness imported into the
constraint layer, every domain type frozen with no mutable defaults.

```python
def test_the_import_guard_can_actually_fail():
    """A guard that cannot fire is decoration."""
```

Even the guards are checked for being able to fail.

## Running it

```bash
make test      # unit, property, stateful, purity — 30s
make verify    # exhaustive enumeration — 15s
make mutants   # mutation testing — 6 min
make check     # all three
```

CI runs all three on every push. Compliance verified once by hand on the last day is a log
file, not a control.
