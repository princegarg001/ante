# The world simulator

::: tip Status
Built, calibrated, and gated in CI. `make world`.
:::

Every number this project reports is graded against this component, which makes it the one
most worth being suspicious of. A simulator that flatters the agent fails *silently* — the
results look better, not broken — so it is treated as something to be constrained rather than
something to be tuned.

<div class="stat-grid">
  <div class="stat"><span class="v">8</span><span class="k">liquidity types</span></div>
  <div class="stat"><span class="v">8</span><span class="k">issuers</span></div>
  <div class="stat ok"><span class="v">24.0%</span><span class="k">approval per execution</span></div>
  <div class="stat"><span class="v">15.1%</span><span class="k">book unrecoverable</span></div>
</div>

## Balance is a marked point process

Not noise around a mean. Income arrives in discrete jumps on days that depend on how the
customer earns; a compound-Poisson spend process draws it down between arrivals. A debit
succeeds when the balance covers it **at the instant of presentation**.

That one mechanism is what makes both of the policy's levers meaningful, and makes them
meaningful for the right reason rather than because they were hard-coded to be:

- **timing** matters because the balance clears the debit only during part of the month
- **amount** matters because `P(success) = P(balance ≥ a)` falls as `a` rises

Month-end clustering of insufficient-funds failures is never injected. It falls out of the
process, which is the only way it can be honest evidence.

<div class="diagram">
<svg viewBox="0 0 720 240" role="img" aria-label="A month of balance for a salaried customer: a spike when salary lands, a sharp drop as auto-debited obligations fire hours later, then a slow decline through the month, with a debit amount marked as a horizontal threshold">
  <text x="24" y="22" font-size="12" font-weight="600">One salaried customer, one month</text>

  <line class="line" x1="60" y1="186" x2="690" y2="186"/>
  <line class="line" x1="60" y1="40"  x2="60"  y2="186"/>

  <path class="line-accent" d="M 60 176 L 96 176 L 100 56 L 118 62 L 124 138 L 150 142
                               L 190 150 L 240 156 L 300 163 L 360 169 L 420 174
                               L 480 178 L 540 181 L 600 183 L 660 184"/>

  <line class="line" x1="60" y1="158" x2="690" y2="158" stroke-dasharray="5 4"/>
  <text class="mono" x="596" y="153" font-size="10" font-weight="600">debit amount</text>

  <text class="dim mono" x="86"  y="202" font-size="9">1st</text>
  <text class="dim mono" x="118" y="202" font-size="9">2nd</text>
  <text class="dim mono" x="300" y="202" font-size="9">10th</text>
  <text class="dim mono" x="480" y="202" font-size="9">20th</text>
  <text class="dim mono" x="650" y="202" font-size="9">30th</text>
  <text class="dim mono" x="20"  y="46"  font-size="9">₹</text>

  <text class="dim" x="104" y="46" font-size="9.5">salary credited</text>
  <text class="dim" x="128" y="120" font-size="9.5">auto-debited obligations fire within hours</text>
  <text class="dim" x="60" y="224" font-size="10.5">The window where the balance clears the debit is narrow, and it is not where intuition puts it.</text>
</svg>
</div>

Four outflow processes act on the account, and each was added because leaving it out made the
world measurably too kind:

<div class="table-scroll">

| Outflow | Timing | Why it is modelled |
| --- | --- | --- |
| Auto-debited obligations | Within hours of the credit, overnight | EMIs and utilities fire at midnight. Without this the post-payday window is wide and artificially rich |
| Manual obligations | 0–3 days after the credit | Rent and bills paid by hand |
| **Competing autopay mandates** | Clustered in the first week | The customer's *other* standing instructions bill when subscriptions bill — direct competition for the same balance at the same moment |
| Everyday spend | Many small events per day | Indian consumers make many low-value UPI payments rather than a few large ones, and the granularity decides whether a partial collection can ever land |

</div>

## Randomness is addressed, not consumed

The naive way to seed a simulation is one generator consumed in call order. It reproduces, and
it is useless for comparing policies: the moment one policy acts differently, every subsequent
draw shifts, and two runs on "the same seed" meet different customers and different outages.

Here every variate is addressed by `(stream, entity, index)` and derived from the seed by
hashing. Nothing is consumed; nothing shifts.

```python
def test_the_world_is_invariant_to_what_a_policy_does():
    busy, idle = World.generate(11, ORIGIN), World.generate(11, ORIGIN)
    ...hammer `busy` with a completely different action sequence...
    assert np.array_equal(busy.population.exogenous_balance,
                          idle.population.exogenous_balance)
```

The world still *reacts* — a customer contacted three times is likelier to revoke. That works
by holding the uniform variate fixed and moving the threshold it is compared against, so the
reaction is genuine while the randomness stays common.

This is what makes the paired comparison in the [evaluation protocol](/analysis/evaluation)
able to detect a few percent of uplift instead of losing it in between-seed variance. It has
to be designed in; it cannot be added afterwards.

::: warning A bug that hides in plain sight
Stream names are hashed with `blake2b`, not Python's built-in `hash()`, which is salted per
process. Addressing a stream with `hash()` produces runs that reproduce perfectly within one
invocation and differ across invocations — invisible for exactly as long as you only ever look
at one run.
:::

## The calibration gate

Base rates are pinned to published market statistics, fixed **before** any policy existed, and
checked in CI on every push and across seeds.

```
$ make world

  metric                          value            band   status
  per_execution_approval          0.240   [0.20, 0.42]   ok
  first_attempt_approval          0.357   [0.26, 0.50]   ok
  total_recovery_naive            0.553   [0.20, 0.68]   ok
  insufficient_funds_share        0.796   [0.50, 0.90]   ok
  technical_failure_share         0.045   [0.03, 0.30]   ok
  unrecoverable_share             0.130   [0.09, 0.24]   ok
  revocation_rate                 0.123   [0.04, 0.45]   ok
  issuer_uptime                   0.977   [0.88, 1.00]   ok
```

The gate is checked for being able to bite. Bands must be proper intervals, none may span
almost the whole unit range, each must carry a source, and a deliberately kinder world has to
be detected:

```python
def test_a_kinder_world_is_detected():
    generous = WorldConfig(unrecoverable_share=0.0)
    assert not measure(42, generous).ok
```

## Two things calibration settled

### C9 is narrower than the documentation implied

[C9](/constraints/critical#c9-the-mandate-is-the-collateral) — a failed first presentation
revokes the mandate — is the highest-risk claim in the build, and it comes from a single PSP's
documentation. Applied to the first presentation of *every* cycle, it produced a **58%
monthly revocation rate**. The market reports roughly 20 million revocations against 808
million executions.

The broad reading is not consistent with the data. C9 is therefore applied to newly registered
mandates only, and that scope is a config field rather than a hard-coded assumption:

```python
first_failure_revokes: bool = True
new_registration_share: float = 0.12
```

The option-value term shrinks but does not vanish — customer-initiated revocation after
repeated failed debits keeps it alive, and that behaviour is independently evidenced by the
same revocation statistics.

### The amount lever is narrower than the design claimed

The [allocator](/system/allocator#the-amount-lever) argues that `a · P(balance ≥ a)` has an
interior maximum, so a partial collection can beat a full one. Measured against this world:

<div class="table-scroll">

| Debit ÷ good-day balance | Mandates | Prefer a partial collection |
| --- | --- | --- |
| under 0.05 | 592 | 0.0% |
| 0.05 – 0.15 | 215 | 0.0% |
| 0.15 – 0.35 | 109 | 0.0% |
| 0.35 – 0.80 | 53 | 9.4% |
| over 0.80 | 296 | 15.2% |

</div>

A ₹499 debit against an account holding either ₹5,000 or ₹20 cannot be helped by shrinking it.
The lever pays where the debit is comparable to what the account actually carries — **36% of
the book, and 45% of the value at risk**.

That is a real qualification of the design's most differentiated feature, and it is better to
have found it here than in a panel room. The test now asserts the *gradient* rather than a
global share, because the global share was a bar that had been invented rather than measured.

### And one band that was replaced

`total_recovery_naive` was originally capped at 0.45, taken from a rule of thumb. The world
failed it — and on inspection the rule of thumb had no source behind it. Nobody publishes
mandate-level recovery under a four-attempt cap; what is published is *per-execution* approval,
around 30%.

So the sourced metric was added and bound tightly, and the unsourced one was relabelled as
what it actually is:

```python
Band("total_recovery_naive", 0.20, 0.68,
     "UNSOURCED — degeneracy guard only, see the note in calibrate.py"),
```

Replacing a band because it had no source is legitimate. Widening one because the world failed
it is the exact failure this harness exists to prevent, so the distinction is recorded in the
source rather than left to memory.

## The unrecoverable segment

15% of the book cannot be recovered by any policy, split across four causes that each produce a
different error code and demand a different response:

<div class="table-scroll">

| Cause | Correct response |
| --- | --- |
| `ACCOUNT_CLOSED` | Stop permanently |
| `ALREADY_REVOKED` | Retrying is abusive, not merely wasteful |
| `VALIDITY_LAPSED` | Re-registration, not a retry |
| `INTENDS_TO_CHURN` | Recoverable in principle, and not worth the mandate |

</div>

If the stop list comes out empty at the end of a run, the world was too kind and the result
means nothing.

## Reproducing it

```bash
python -m mandate_recovery.sim.generate --seed 42
python -m mandate_recovery.sim.calibrate --seed 42
```

Seeds 0–7 train, seeds 100–109 evaluate, and the split is a constant in the source so it can
be checked rather than promised.
