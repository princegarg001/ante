# Evaluation protocol

::: tip Status
Built. B0, B1, B3 and the lawful clairvoyant are measured on held-out seeds —
see **[Results](/analysis/results)**. B2 arrives with the probability model on day 5.
:::

Razorpay's bar for this track is *measured money recovered across a batch*. This page is how
that measurement is designed so the number survives scrutiny.

## Held-out seeds and honest training

Seeds 0–7 train. Seeds 100–109 evaluate. **The policy never sees an evaluation seed**, and the
split is a constant in code so it can be checked rather than trusted.

The probability model trains on logged transactions only — never on the simulator's latent
state. The agent's belief space is deliberately given a *different* structure from the
simulator's generative parameters: different bucket boundaries, no shared constants.
Otherwise the evaluation grades a model against features it was handed, and a reviewer will
find it.

## Four baselines

<div class="table-scroll">

| # | Baseline | What it establishes |
| --- | --- | --- |
| B0 | No retry | The floor |
| B1 | Fixed schedule at +24h / +72h / +168h | The genuine industry heuristic, and a strong opponent |
| B2 | Greedy EV — always take the highest-probability legal slot now | Whether the *allocation* framing earns its keep |
| B3 | **Stripe-style** — ~8 attempts across two weeks, timing-optimised, no Indian constraints | Whether the *regulatory* framing earns its keep |

</div>

B1 is not a straw man. Published data suggests moving a first retry from +2h to +24h improves
recovery by around 6.5%, and the +24/+72/+168 pattern is what competent teams actually ship.
If the allocator beats it by 3%, the claim is 3% — not "dramatically".

### B3 is the experiment worth running

Stripe's published default policy, executed against the constraint layer, reporting:

- the count of NPCI/RBI violations it generates, and
- the number of mandates it destroys through
  [first-presentation revocation](/constraints/critical#c9-the-mandate-is-the-collateral).

Not as criticism — their policy is correct for card rails. The point is that quantifying the
failure of the transplant is the cleanest possible statement of why this problem needs its own
design.

## Variance reduction, or the uplift is noise

All policies run on **the same** seeds, with **common random numbers**: identical latent
customers, identical income arrivals, identical issuer downtime. Only the policy differs.

Comparisons are then made on **paired** per-seed differences, reported as mean with a 95%
bootstrap confidence interval, plus a Wilcoxon signed-rank test.

::: warning Why this is not optional
Without pairing, a 3% uplift across ten seeds disappears into between-seed variance and cannot
be claimed at all. An experienced panel will ask how the comparison was constructed, and
"unpaired, ten seeds" is not an answer that survives the follow-up.
:::

## The oracle bound

A **clairvoyant policy** that sees the latent balance trajectory and still obeys every
constraint gives the true ceiling for any lawful policy. Then:

```
recovery_efficiency = (yours − B1) / (oracle − B1)
```

> *"We capture 71% of the recovery achievable by any policy that obeys Indian law, against 34%
> for the fixed-schedule heuristic."*

That is a stronger and far more defensible statement than "+12% versus baseline", because it
bounds how much is left rather than implying there is more.

## Metrics

<div class="table-scroll">

| Metric | Why it is reported |
| --- | --- |
| Rupees recovered | The headline, against all four baselines |
| Recovery cost | Attempts, notifications, contacts consumed |
| **Net value** | Recovered minus cost. The number that actually matters |
| **Mandates surviving at cycle end** | Directly measures the option-value thesis |
| Slot efficiency | Rupees recovered per execution slot spent |
| **Recovery efficiency versus oracle** | How much of the achievable was achieved |
| Stop list | Count and value deliberately refused, by reason |
| **Realised regret of stopping** | Of the money refused, how much was actually recoverable — the simulator knows |
| Constraint violations | Must be exactly zero. Non-zero is a bug, not a trade-off |
| Calibration | Brier score, expected calibration error, reliability diagram |
| Diagnosis accuracy | Confusion matrix against ground truth |
| Failure list | Cases handled badly, and why |

</div>

Two of these are unusual and deliberate. **Mandates surviving** turns the option-value argument
into a measurement rather than a claim. **Realised regret of stopping** reports the cost of the
system's own caution — the money it refused that would in fact have arrived.

## A statistical guarantee on the stop list

The stop list is the most distinctive artifact here. Every other submission optimises recovery;
this one reports the money it chose not to chase and defends each decision.

**Conformal risk control** upgrades that from a log into a claim. On a calibration split, choose
the stop threshold `τ` such that the false-stop rate — money refused that was in fact
recoverable — is bounded at level `α`, with distribution-free finite-sample validity.

> *"At most 5% of the rupees we refuse to chase were actually collectable, and that bound holds
> without assuming our model is correct."*

Roughly eighty lines of split-conformal thresholding, and it is the highest
prestige-per-hour item in the plan.

## Red team

The system is attacked on the assumption that a reward-maximising agent will find any loophole
available to it. Each test asserts the agent *fails* to exploit one:

<div class="table-scroll">

| Attack | Guard under test |
| --- | --- |
| Notification spam — reward credits recovery only | `OPS-CONTACT` cap |
| Amount slicing — split one debit into many small ones | C1 counts presentations |
| Peak-hour drift — timezone-confused clock | C2/C3 evaluated in IST |
| Notification thrash — cancel and re-issue to fish for a better slot | C8 cost is explicit |
| Terminal-cause laundering — craft a bank response so the LLM reclassifies `TERMINAL` | The one-way ratchet |
| Clock skew and replay — duplicate webhook, WAL replay after crash | Idempotency keys |

</div>

Razorpay is hiring into a risk-adjacent organisation. Demonstrating that the agent was attacked
and held is a stronger signal than any accuracy number.

## Simulator honesty

The evaluation is only as good as the world it runs in, so the world is constrained rather
than tuned:

<div class="table-scroll">

| Band | Measured | Source |
| --- | ---: | --- |
| Approval **per execution** | 0.240 | reported ~30% at the largest remitter bank |
| First-attempt approval | 0.357 | first attempts sit above the all-execution average |
| Insufficient-funds share of failures | 0.796 | business declines dominate |
| Unrecoverable share of the book | 0.130 | closed, revoked, lapsed, genuine churn |

</div>

The bands are fixed before any policy exists and gated in CI across seeds, and the gate is
asserted to be capable of failing — a deliberately kinder world must be detected. One band was
replaced during construction because it turned out to have no source behind it, and the
reasoning is recorded rather than left to memory. Details in
[the world simulator](/system/simulator#the-calibration-gate) and [Market data](/analysis/market).
