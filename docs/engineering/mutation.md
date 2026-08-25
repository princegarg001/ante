# Mutation testing

::: tip Status
Built. Runs in CI on every push, `python -m tests.mutation`.
:::

A compliance suite that passes proves nothing on its own. A suite that asserts very little
also passes. The only way to find out whether the tests have teeth is to break the thing they
are supposed to protect and see if anyone notices.

So the **regulation itself** gets mutated — eleven plausible misreadings of a circular — and
every one must turn the suite red.

## The mutants

<div class="table-scroll">

| ID | Rule | Mutation | Result |
| --- | --- | --- | --- |
| M1 | <span class="rule reg">C5</span> | Notification aperture loses its 48h upper bound | caught |
| M2 | <span class="rule reg">C5</span> | Notification minimum drops from 24h to 12h | caught |
| M3 | <span class="rule reg">C1</span> | Retry cap off by one (4 → 5 presentations) | caught |
| M4 | <span class="rule reg">C2</span> | Evening peak window closes at 20:30 instead of 21:30 | caught |
| M5 | <span class="rule reg">C2</span> | Morning peak window never opens | caught |
| M6 | <span class="rule reg">C8</span> | One-pending-notification rule disabled | caught |
| M7 | <span class="rule reg">RATCHET</span> | Terminal-cause ratchet disabled | caught |
| M8 | <span class="rule reg">C15</span> | AFA-free ceiling check disabled | caught |
| M9 | <span class="rule reg">C19</span> | Mandate-cap check disabled | caught |
| M10 | <span class="rule reg">C2</span> | Peak-window veto disabled entirely | caught |
| M11 | meta | The model checker's independent spec is made to agree with the code | caught |

</div>

M1 is not a hypothetical misreading. It is exactly what the first draft of this project
assumed — that the notification rule was a 24-hour floor with no ceiling. The mutation
reproduces a mistake that was actually made.

## What the first run found

<div class="stat-grid">
  <div class="stat"><span class="v">9 / 11</span><span class="k">first run</span></div>
  <div class="stat ok"><span class="v">11 / 11</span><span class="k">after fixes</span></div>
</div>

Two survived. Both were real holes rather than harness noise, and finding them is the entire
justification for the exercise.

### M3 — nothing pinned the retry cap to a literal

Changing `MAX_ATTEMPTS` from 4 to 5 left the whole suite green.

The reason is subtle and worth internalising. Every assertion about the retry cap *imported the
symbol*:

```python
def test_c1_vetoes_the_fifth_attempt():
    s = make_state(attempts_used=MAX_ATTEMPTS)   # moves with the mutation
    assert "C1" in vetoed_by(commit(24), s)
```

So did the model checker's supposedly independent specification. Mutate the constant and every
assertion about it moved in lockstep. The suite was verifying that the code was
self-consistent, not that it matched the regulation.

**The fix**, in `tests/test_regulatory_constants.py`:

```python
def test_c1_retry_budget_is_one_execution_plus_three_retries():
    """NPCI UPI/API Guidelines 2025 — 1 original + 3 retries per mandate."""
    assert MAX_ATTEMPTS == 4
```

The general principle: **a regulatory constant must be asserted against a literal with its
source beside it.** That way changing a value requires deliberately editing a test that cites a
circular — the difference between a value being *configured* and a value being *claimed*.

Every constant is now pinned this way: the retry cap, both aperture bounds, the peak windows,
the 23:50 cut-off, and both AFA ceilings.

### M11 — the independent spec could drift silently

Zeroing the morning peak window inside the model checker's own specification also left the
suite green.

The reason: `_inv_violations` is normally only consulted about actions the constraint layer has
already *permitted* — and permitted actions were lawful for other reasons, so a corrupted spec
never got the chance to disagree. The independent check had quietly become decorative.

**The fix** was to ask the specification directly about actions known to be illegal:

```python
@pytest.mark.parametrize("hours,amount,overrides,expected", [
    (34, rupees(499), {}, "C2"),                        # 10:00 next day
    (12, rupees(499), {}, "C5"),                        # under the aperture
    (24, rupees(499), {"attempts_used": 4}, "C1"),
    ...
])
def test_independent_spec_flags_known_illegal_actions(...):
    assert expected in _inv_violations(action, make_state(**overrides), ORIGIN)
```

Plus the mirror image, because a specification that flags *everything* would also kill every
mutant:

```python
def test_independent_spec_accepts_a_lawful_action():
    assert _inv_violations(lawful_commit, make_state(), ORIGIN) == []
```

## A harness bug worth admitting

The first version of the mutation runner detected failure by grepping pytest output for the
word `failed`. Combined with `-x`, which stops at the first failure and prints a traceback,
the summary line fell outside the captured tail — and every mutant was reported as surviving,
including ones that were being caught cleanly.

A detector that reports "everything survived" looks like a devastating finding. It was a
truncated pipe.

```python
def _run_suite() -> bool:
    """True if the suite is green. Exit code, not log scraping — a truncated
    traceback once made a red run look green here."""
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-m", "not slow"], ...)
    return proc.returncode == 0
```

The lesson generalises past this repo: **verification tooling needs verifying too.** M11 exists
for the same reason.

## Running it

```bash
make mutants
```

Each mutant applies a single regex substitution to one source file, runs the full suite,
restores the file, and records whether the suite went red. Roughly six minutes for all eleven.

A surviving mutant is reported as what it is — a regulation the suite would not notice
breaking:

```
Survivors — each one is a regulation the suite would not notice breaking:
  M3 [C1] Retry cap off by one (4 -> 5 presentations)
```
