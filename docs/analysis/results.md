# Results

::: tip Status
Baselines, the calibrated survival model and the lawful clairvoyant are measured. **The
allocator is not built yet** — its row is deliberately absent rather than estimated. It
arrives on days 6–7, and it has to beat B2's 14.4% of headroom, not B1.
:::

Ten held-out seeds, 1,500 mandates each, paired on common random numbers. Reproduce with
`make results`.

<div class="stat-grid">
  <div class="stat"><span class="v">986</span><span class="k">failed mandates / seed</span></div>
  <div class="stat"><span class="v">₹5.65L</span><span class="k">at risk per seed</span></div>
  <div class="stat ok"><span class="v">₹1.68L</span><span class="k">headroom to the oracle</span></div>
  <div class="stat"><span class="v">10</span><span class="k">held-out seeds</span></div>
</div>

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

| Policy | Recovered | Net value | Rate | ₹/attempt | Survived | Stops | Illegal |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| B0 · no retry | ₹0 | ₹0 | 0.0% | 0 | 81.5% | 0 | 0 |
| **B1 · fixed +24/+72/+168h** | ₹1,66,482 | **₹1,62,799** | 29.4% | 90 | 78.9% | 70 | 0 |
| **B2 · greedy EV, no budget reasoning** | ₹1,89,927 | **₹1,86,848** | 33.6% | 123 | 78.8% | 70 | 0 |
| B3 · Stripe-style, 8 attempts / 2 weeks | ₹1,16,410 | ₹1,12,549 | 20.6% | 60 | 79.8% | 0 | **746** |
| *oracle · clairvoyant, lawful* | *₹3,31,609* | *₹3,30,356* | *58.7%* | *530* | *80.6%* | *169* | *0* |
| **the allocator** | — | — | — | — | — | — | — |

</div>

**Net value** is the headline, not recovered rupees. Recovered alone rewards a policy for
burning attempts and customer patience to get them; net value subtracts ₹2 per presentation
and ₹0.50 per contact. Those are modelling choices, stated rather than buried.

## The comparison is paired

<div class="table-scroll">

| vs B1 | Mean difference | 95% bootstrap CI | p | Seeds won |
| --- | ---: | :---: | ---: | ---: |
| B0 · no retry | −₹1,62,799 | [−170,023, −156,802] | 0.0020 | 0/10 |
| **B2 · greedy EV** | **+₹24,049** | [+19,071, +28,892] | 0.0020 | **10/10** |
| B3 · Stripe-style | −₹50,250 | [−53,986, −46,384] | 0.0020 | 0/10 |
| oracle | +₹1,67,558 | [+162,506, +172,920] | 0.0020 | 10/10 |

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
| Illegal actions proposed | **C5 × 1,106**, RATCHET × 552, C2 × 296 |
| Mandates affected | **746 — 76% of the batch** |
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

When the allocator lands, the number to report is:

```
recovery efficiency = (allocator − B1) / (oracle − B1)
```

"We captured N% of what any lawful policy could have" bounds what is left on the table.
"We beat the heuristic by N%" does not.

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
