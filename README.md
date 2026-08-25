# Mandate Recovery Engine

**Track 03 · AI Revenue Recovery · Razorpay AI Buildathon**

A constrained-budget recovery agent for failed UPI Autopay / e-mandate debits.

---

## The problem everyone else is solving

Stripe Smart Retries, Recurly Intelligent Retries, Gr4vy — every recovery product on the
market answers one question: *when should I retry this failed payment?* They assume retries
are cheap and roughly unlimited. Stripe's published default is around eight attempts across
two weeks.

**In India that policy is illegal.**

## The problem actually in front of you

NPCI allows **one execution plus three retries** per mandate per cycle, **only outside**
10:00–13:00 and 17:00–21:30 IST. RBI requires a pre-debit notification, and it must be raised
in a **two-sided window** — no earlier than 48 hours and no later than 24 hours before the
debit. Only **one such notification may be pending per mandate** at a time: raising a new one
cancels the old. And if the first presentation on a mandate fails, the mandate is revoked.

Put together:

> You get one irrevocable, blind, 24-hours-ahead bet at a time, at most four of them, and
> losing the first one can destroy the asset you are collecting against.

That is not a retry schedule. It is sequential decision-making under a serialization
constraint with the mandate posted as collateral — which is why ~20 million UPI Autopay
mandates are revoked every month, mostly because someone presented a debit into an empty
account.

**Everyone else optimises when to retry. I built the thing that decides whether to bet.**

See [COMPLIANCE.md](COMPLIANCE.md) for every constraint with its source and verification
status, and [BUILD-SPEC.md](BUILD-SPEC.md) for the full design.

---

## Status

Built in the open against a 5 September deadline. This section is accurate as of the last
commit — it says what runs, not what is planned.

| Component | State |
|---|---|
| `core/` — IST clock, non-peak slot grid, domain types, paise money | **done** |
| `constraints/` — C1–C24 as pure functions, every veto citing its rule | **done** |
| `constraints/modelcheck.py` — exhaustive verification | **done** |
| `tests/mutation.py` — mutation testing of the compliance suite | **done** |
| `tests/test_purity.py` — AST guards on the decision path | **done** |
| `act/` — WAL, idempotency, ceilings, kill switch, hash-chained receipts | not started |
| `sim/` — latent balance process, issuer downtime, churn | not started |
| `eval/` — harness, baselines, oracle bound | not started |
| `belief/`, `predict/`, `policy/` — the allocator | not started |
| `ingest/` — Razorpay test-mode webhooks | not started |
| `diagnose/` — rules ratchet + LLM adjudicator | not started |

---

## Verified, not merely tested

Most submissions will assert compliance with a handful of unit tests. This layer makes a
stronger claim, in three levels.

**1 · Exhaustive enumeration.** Every `(state, action, clock)` triple in a bounded but
complete grid is enumerated, and every action the layer permits is cross-checked against the
regulation. Not "no counterexample was sampled" — no counterexample exists in the grid.

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

The checker restates the regulation *independently* of the implementation — it duplicates the
peak-window literals on purpose. A checker that imported the code's own predicates would only
prove the code equals itself.

`cap actually binding` exists because a proof that cannot fail is not a proof. Each attempt
costs at least 24 hours of notification lead, so a horizon under five days can never reach the
retry cap and the claim would pass vacuously. The run reports whether it genuinely bound.

**2 · Property-based tests.** Hypothesis wanders unbounded where the grid is bounded —
arbitrary months, unaligned instants, absurd amounts — including a stateful machine that
drives a mandate through thousands of random *sequences* of legal actions, because a budget
overrun is a property of a sequence, not of an action.

**3 · Mutation testing.** A compliance suite that passes proves nothing on its own; a suite
asserting very little also passes. So the regulation itself is mutated — an aperture with no
upper bound, a peak window off by an hour, a retry cap off by one, the serialization rule
quietly dropped — and every mutant must turn the suite red.

```
$ make mutants

  M1   caught   [C5     ] Pre-debit notification aperture loses its 48h upper bound
  M2   caught   [C5     ] Pre-debit notification minimum drops to 12h
  M3   caught   [C1     ] Retry cap off by one (4 -> 5 presentations)
  M4   caught   [C2     ] Evening peak window closes at 20:30 instead of 21:30
  M5   caught   [C2     ] Morning peak window never opens
  M6   caught   [C8     ] One-pending-PDN serialization rule disabled
  M7   caught   [RATCHET] Terminal-cause ratchet disabled
  M8   caught   [C15    ] AFA-free ceiling check disabled
  M9   caught   [C19    ] Mandate-cap check disabled
  M10  caught   [C2     ] Peak-window veto disabled entirely
  M11  caught   [meta   ] Model checker's spec agrees with the code by construction

  11/11 mutants killed
```

This paid for itself immediately. The first run killed 9 of 11, and both survivors were real
holes rather than harness noise:

- **M3** survived because every assertion about the retry cap *imported* `MAX_ATTEMPTS`,
  including the model checker's supposedly independent specification. Change the constant and
  the whole suite moved with it. Nothing anywhere pinned the number 4 to the circular it comes
  from. Fixed in [tests/test_regulatory_constants.py](tests/test_regulatory_constants.py): a
  regulatory constant is now asserted against a literal with its source beside it, so changing
  one means deliberately editing a test that cites a circular.
- **M11** survived because the model checker's independent restatement of the regulation is
  normally only asked whether *permitted* actions are legal — and those were legal for other
  reasons, so a corrupted spec never got the chance to disagree. Fixed by asking the spec
  directly about known-illegal actions, which is what makes it a specification rather than an
  echo.

A surviving mutant is a hole in the compliance argument, and it is reported as one.

**Plus static guards.** The claims that make replay meaningful are enforced over the AST, not
left as prose: no wall-clock read anywhere on the decision path, no I/O or randomness imported
into the constraint layer, every domain type frozen
([tests/test_purity.py](tests/test_purity.py)).

---

## Run it

```bash
make install     # pip install -e ".[dev]"
make test        # unit + property tests
make verify      # exhaustive constraint verification
make mutants     # mutation testing
make check       # all three — the full compliance gate
```

CI runs all three on every push ([.github/workflows/compliance.yml](.github/workflows/compliance.yml)).
Compliance verified once by hand on the last day is a log file, not a control.

---

## Design notes worth arguing with

**Regulatory and operational vetoes are kept separate.** `RuleKind.REGULATORY` cites a
circular; `RuleKind.OPERATIONAL` is merchant policy and blast-radius control. The headline
compliance claim counts only the former, so it cannot be inflated by counting internal
guards. When an action is illegal for several reasons at once, the reported veto is the one a
regulator would care about.

**Re-planning must be explicit.** Because only one notification may be pending (C8), the
policy cannot silently overwrite a commitment — it must emit `CancelPending` first. The cost
of abandoning a bet therefore appears in the audit log as a decision that was taken.

**`Wait` is an action, not an absence of one.** Under a two-sided notification window the set
of reachable execution times moves every slot, so declining to commit today has a real cost
and belongs in the log.

**The terminal-cause ratchet is one-way.** Diagnosis may move a cause into the terminal set;
nothing may move it out. Retrying a revoked mandate is not merely wasteful, it is abusive.

**Money is an integer count of paise, everywhere.** A float rupee amount is a rounding
difference waiting to appear between what the policy valued, what the constraint layer
checked, and what the ledger recorded.

**Nothing in the package reads the wall clock.** Every function that cares about time takes it
as an argument, which is what makes a run replayable and the verification meaningful.

---

## Honest limitations

- **Every regulatory constraint is currently sourced from secondary material** — law-firm
  notes, PSP developer documentation, press reporting. None has yet been confirmed against the
  NPCI or RBI circular PDF. `COMPLIANCE.md` marks each row `SECONDARY` and carries the
  verification checklist. Three of them (the 48-hour upper bound, the one-pending-notification
  rule, and first-presentation revocation) come substantially from a single PSP's docs and may
  turn out to be PSP convention rather than regulation.
- **C7, the 23:50 notification cut-off, is unreachable on the current 30-minute slot grid** —
  it is dominated by the 24-hour minimum lead. The rule is retained, and a test documents why.
  It binds again at finer granularity.
- The exhaustive sweep is exhaustive over a *bounded* grid. Its bounds are stated in the
  source and chosen to straddle every boundary the rules depend on, but it is not a proof over
  all of time.
