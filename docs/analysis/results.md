# Results

::: tip Status
Complete: baselines, the calibrated survival model, the allocator, and the lawful
clairvoyant, all measured on held-out seeds.
:::

Ten held-out seeds, 1,500 mandates each, paired on common random numbers. Reproduce with
`make results`.

<div class="stat-grid">
  <div class="stat"><span class="v">986</span><span class="k">failed mandates / seed</span></div>
  <div class="stat"><span class="v">₹5.65L</span><span class="k">at risk per seed</span></div>
  <div class="stat ok"><span class="v">29.2%</span><span class="k">of lawful headroom captured</span></div>
  <div class="stat ok"><span class="v">146,846</span><span class="k">hash-chained receipts</span></div>
</div>

::: tip Every rupee here has a receipt
This run was driven through the audited money path: intent written and `fsync`ed before
every effect, outcome written after, each presentation addressable by its own idempotency
key. 146,846 hash-chained records across 60 runs, and `make replay` reconstructs any of
them. The audit layer records the run without changing it — there is
[a test asserting the numbers are identical with it on and off](/system/action-layer#the-evaluation-runs-through-this).
:::

## What is being measured

Not total collection — that would mostly measure how many debits happened to clear on their
due date. The experiment is scoped to *recovery*:

1. Every mandate's original execution fires on its due date. That is the merchant's scheduled
   debit, not the agent's decision.
2. The failures become the batch.
3. From there each mandate allows at most three further presentations, under every constraint
   in the [register](/constraints/).
4. Recovered rupees are what the agent collected **from that batch**. Original successes are
   excluded — they were never at risk.

Of the 986 failed mandates, **220 (₹1.28L) are never actionable**: already revoked or expired
before the first decision epoch, or the cycle closed before the notification aperture could
open. No policy is offered these, and folding them into a stop list would credit a policy with
a judgement it never made.

## The table

<div class="table-scroll">

| Policy | Recovered | Net value | Rate | ₹/att | Surv | Stops | Escal | Illegal |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| B0 · no retry | ₹0 | ₹0 | 0.0% | 0 | 81% | 0 | 0 | 0 |
| **B1 · fixed +24/+72/+168h** | ₹1,66,482 | **₹1,62,799** | 29.4% | 90 | 79% | 316 | 0 | 0 |
| **B2 · greedy EV, no budget reasoning** | ₹1,96,704 | **₹1,93,680** | 34.8% | 130 | 78% | 327 | 0 | 0 |
| B3 · Stripe-style, 8 attempts / 2 weeks | ₹1,16,410 | ₹1,12,549 | 20.6% | 60 | 80% | 0 | 0 | **966** |
| **allocator · priced DP with option value** | ₹2,14,496 | **₹2,11,646** | **38.0%** | 151 | 78% | 121 | **256** | 0 |
| *oracle · clairvoyant, lawful* | *₹3,31,609* | *₹3,30,356* | *58.7%* | *530* | *81%* | *397* | *0* | *0* |

</div>

**Net value** is the headline, not recovered rupees. Recovered alone rewards a policy for
burning attempts and customer patience to get them; net value subtracts ₹2 per presentation
and ₹0.50 per contact. Those are modelling choices, stated rather than buried.

## The comparison is paired

<div class="table-scroll">

| vs B1 | Mean difference | 95% bootstrap CI | p | Seeds won |
| --- | ---: | :---: | ---: | ---: |
| B0 · no retry | −₹1,62,799 | [−170,023, −156,802] | 0.0020 | 0/10 |
| **B2 · greedy EV** | **+₹30,881** | [+25,731, +35,943] | 0.0020 | **10/10** |
| **allocator** | **+₹48,848** | [+42,644, +54,751] | 0.0020 | **10/10** |
| B3 · Stripe-style | −₹50,250 | [−53,986, −46,384] | 0.0020 | 0/10 |
| oracle | +₹1,67,558 | [+162,506, +172,920] | 0.0020 | 10/10 |

And the comparison that carries the thesis — same model, same belief, the only
difference is that one of them treats a retry slot as scarce and priced:

| | Mean difference | 95% bootstrap CI | p | Seeds won |
| --- | ---: | :---: | ---: | ---: |
| **allocator vs B2** | **+₹17,966** | [+11,174, +25,031] | 0.0020 | **10/10** |

</div>

Because [the world is invariant to what a policy does](/system/simulator#randomness-is-addressed-not-consumed),
every policy meets the identical book on a given seed. The statistic is therefore the per-seed
*difference*, whose variance is far smaller than either policy's own.

That is not a technicality. Unpaired, a three-percent uplift across ten seeds is
indistinguishable from between-seed noise — different customers, different salary dates,
different outages — no matter how carefully it was earned. `p = 0.0020` is the floor for a
signed-rank test on ten samples; it means every seed went the same way, not that the effect is
enormous.

## What the transplant costs

Stripe's published shape — roughly eight attempts across two weeks — moved to Indian rails
without modification:

<div class="table-scroll">

| | |
| --- | --- |
| Illegal actions proposed | **C12 × 1,770**, C5 × 1,106, RATCHET × 552, C2 × 296 |
| Mandates affected | **966 — 98% of the batch** |
| Net value versus the industry heuristic | **−₹50,250** |

</div>

The characteristic violation is C5, the notification aperture. A card-rails policy schedules
from *now*; here a debit must be notified 24–48 hours ahead, so its short-dated attempts
simply do not exist. Then C2 removes a share of the times it does choose.

The point is not that the policy is bad. It is correct for the rails it was designed for. The
point is that **it does not merely break the law here — it also collects a third less**,
because most of what it wants to do is unavailable. Quantifying that is the cleanest
available statement of why this problem needs its own design.

::: warning Measured fairly
Illegal proposals are refused and **not executed**, so B3 is scored on what it could lawfully
achieve. It is not being penalised twice. Violations are also reported per *mandate* rather
than only per proposal, because a policy that repeats one bad idea every epoch is not fifty
times worse than one that has it once — the raw count measures decision cadence as much as
policy quality.
:::

## Headroom

<div class="table-scroll">

| | Net value |
| --- | ---: |
| B1 · the industry heuristic | ₹1,62,799 |
| oracle · clairvoyant, lawful | ₹3,30,356 |
| **headroom available** | **₹1,67,558** |

</div>

The oracle sees the customer's balance trajectory and is still bound by every constraint —
the retry cap, the peak windows, the two-sided aperture, the serialization rule. It measures
the ceiling for *any lawful policy*, not for a policy with no rules.

Its most telling column is **₹530 recovered per attempt against B1's ₹90**. Knowing when the
money is there means one well-timed presentation instead of three speculative ones. That gap
is the allocation thesis, visible before the allocator exists.

```
recovery efficiency = (policy − B1) / (oracle − B1)
```

| Policy | Recovery efficiency |
| --- | ---: |
| B3 · Stripe-style | −30.0% |
| B2 · greedy EV | 18.4% |
| **allocator** | **29.2%** |

"We captured 29% of what any lawful policy could have" bounds what is left on the table.
"We beat the heuristic by 30%" does not.

## The stop list, scored

Every other submission optimises recovery. This one reports the money it chose **not** to
chase, and what that caution actually cost — measured against ground truth after the run,
never visible to any policy.

<div class="table-scroll">

| Policy | Refusals | Value refused | Right | Regret | Regret % |
| --- | ---: | ---: | ---: | ---: | ---: |
| B1 · fixed schedule | 316 | ₹1,85,139 | 100% | ₹0 | 0.0% |
| B2 · greedy EV | 327 | ₹1,90,978 | 100% | ₹0 | 0.0% |
| **allocator** | **377** | **₹2,08,013** | **91%** | **₹11,422** | **5.5%** |
| *oracle · clairvoyant* | *397* | *₹2,33,017* | *100%* | *₹857* | *0.4%* |

</div>

**"Right" is the share of refusals from which a clairvoyant could have collected nothing.**

The baselines score 100% for an uninteresting reason: they refuse *only* what is obviously
dead — a revoked mandate, a closed account. No judgement is involved, so none can be wrong.

The allocator additionally refuses live mandates it has decided are not worth chasing. That
is a judgement, it is right 91% of the time, and being wrong the other 9% costs ₹11,422 —
5.5% of the value it declined. The clairvoyant, which refuses only what is truly
unrecoverable, sets the floor at 0.4%.

That gap between 0.4% and 5.5% is the honest price of not being able to see the future, and
it is the number a payments risk person will want. It is reported rather than omitted.

## Compliant escalation

A retry is not the only available action, and for a large part of a failed batch it is the
wrong one.

<div class="table-scroll">

| Policy | Escalation ladder |
| --- | --- |
| B0, B1, B2, B3, oracle | stops only |
| **allocator** | **`RequestRemandate` × 225 · `EscalateHuman` × 31** |

</div>

A mandate above its AFA ceiling needs authentication. A lapsed or revoked one needs
re-registration — a customer conversation, not a payment. Above ₹1,000 outstanding it is
worth an operator's time and gets a written summary. A closed account needs nothing at all.

Collapsing all of those into `Stop` throws away the distinction that matters operationally,
and it is the difference between *stopping* and *escalating compliantly*.

## Which idea earned the number

The allocator adds three things at once — backward induction over the remaining budget, an
option-value term for the mandate, and a shadow price on scarce execution windows. It would
be easy to credit whichever sounds best. Turning the capacity constraint off answers it, and
the answer was not the expected one.

```
$ make ablate

  arm                                       net value    rate  attempts  stops
  B1 · fixed schedule                        ₹89,604    31.3%       956     45
  B2 · greedy EV, same model and belief    ₹1,12,134    38.8%       771     45
  allocator · capacity unlimited           ₹1,14,103    39.5%       836     45
  allocator · capacity loose               ₹1,22,435    42.3%       791     45
  allocator · capacity default             ₹1,27,109    43.9%       753     59
  allocator · capacity tight               ₹1,22,639    42.4%       777     52

  dynamic programme + option value, no price     +₹1,969
  adding the capacity price                     +₹13,005
```

**With capacity unlimited the allocator is barely distinguishable from greedy.** The dynamic
programme and the option-value term, on their own, are worth close to nothing here. Almost
all of the gain comes from the price.

That is a sharper claim than the one it replaced. The shadow price is not just rationing a
scarce resource — it acts as a **selectivity threshold**. An attempt must clear a price to be
worth making, so marginal opportunities are refused and the budget concentrates where the
expected value clearly beats it. The evidence is the shape of the response: value peaks at an
intermediate capacity and falls away on *both* sides. Too loose and nothing is filtered; too
tight and opportunities worth taking are priced out.

::: warning Caveat on this ablation
Run on five seeds, where the signed-rank test is not computed — the intervals are bootstrap
only. The direction is consistent across every arm and every seed, but the ₹1,969 figure in
particular deserves ten seeds before it is quoted as precise.
:::

## An ablation that had to be fixed first

The allocator's margin over B2 was originally **+₹53,497**. That number was wrong.

The survival model is trained with the pay-cycle belief among its features. The allocator
supplied them; B2 passed zeros. So part of the measured gap was *"I have information you do
not"* rather than *"allocation is worth something"* — which corrupts the one comparison the
whole project rests on.

With B2 given the identical belief, the gap is **+₹12,182** [+6,996, +16,953], p = 0.0059,
9/10 seeds. Sixty percent smaller, and the honest number. A comparison has to differ in
exactly one thing.

::: warning The oracle is a strong achievable policy, not a proven supremum
It commits to the single best reachable slot rather than searching every sequence of up to
three commitments, so a cleverer clairvoyant could do slightly better where partial
collections across several attempts beat one full collection. Reported as a close bound rather
than as "optimal".
:::

## Reproducing

```bash
make results     # 10 held-out seeds, 1,500 mandates, ~2 minutes
```

Seeds 0–7 are for training and 100–109 are held out; the split is a constant in the source so
it can be checked rather than promised. A smaller version runs in CI on every push.
