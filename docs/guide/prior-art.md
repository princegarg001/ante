# Prior art

Knowing the existing products matters for two reasons: it is where the good ideas are, and it
is how you demonstrate that a different design is a considered choice rather than ignorance.

## What the incumbents built

<div class="table-scroll">

| Product | Approach | Scale of the retry budget |
| --- | --- | --- |
| **Stripe Smart Retries** | ML over 500+ attributes trained across the Stripe network, predicting the optimal moment to retry | ~8 attempts across ~2 weeks |
| **Recurly Intelligent Retries** | Learned retry schedules per card and issuer | Configurable, multi-attempt |
| **Gr4vy / Slicker and similar** | Retry orchestration with issuer-aware routing | Multi-attempt |

</div>

Stripe describe Smart Retries as predicting "the optimal time to retry a failed payment" and
report recovering $9 for every $1 customers spend on Billing. Deliveroo is cited as recovering
more than £100 million through it. These are serious systems and the results are real.

They are also all answering the same question, and it is a timing question.

## Why the transplant fails

The design is sound for the rails it was built for. Card networks permit repeated
authorisation attempts, the credential is stored, and issuers will approve against credit
rather than a live balance. Under those conditions the binding constraint genuinely is
*when*, and a model with 500 features is the right instrument.

Move it to UPI Autopay and every premise fails at once:

<div class="table-scroll">

| Premise | On Indian rails |
| --- | --- |
| Attempts are plentiful; take many | Four, total, then the cycle is dead |
| Retry at the predicted moment | Peak hours are barred; the predicted moment may be illegal |
| Decision and execution are simultaneous | Notification must precede execution by 24–48 hours |
| Attempts can run in parallel or overlap | One pending notification per mandate |
| A failed attempt costs an attempt | A failed first presentation costs the mandate |

</div>

An eight-attempt fortnight policy run against Indian rules does not merely underperform. It
generates constraint violations, and it burns mandates.

::: tip A planned experiment
The evaluation harness will include Stripe's published default policy as a fourth baseline,
run against the constraint layer, reporting the count of NPCI/RBI violations it generates and
the number of mandates it destroys. Not as criticism — their policy is correct for their
market — but because quantifying the failure of the transplant is the cleanest available
statement of why this problem needs its own design. See [Evaluation protocol](/analysis/evaluation).
:::

## What is worth borrowing

Rejecting the framing is not the same as rejecting the engineering.

**Calibration discipline.** Any probability feeding an expected-value calculation has to be
calibrated, not merely accurate. A model that ranks well but is systematically overconfident
will produce confident nonsense downstream. Reliability diagrams and Brier scores, not AUC.

**Issuer-level state.** Bank-level success rates move, cluster and recover. Treating the issuer
as a first-class feature with recent history is right on any rails.

**Feature breadth over cleverness.** Stripe's advantage is largely data, not architecture.
Where that data is unavailable, the honest substitute is an explicit model of the latent
state — a posterior over customer liquidity type — rather than pretending to features you do
not have.

## Where the academic framing sits

The structure here — many independent decision processes, weakly coupled through a shared
scarce resource — is a **weakly-coupled constrained Markov decision process**. The standard
approach is Lagrangian relaxation: relax the shared capacity constraint into a price, which
decomposes the problem into independent per-mandate problems, then adjust the price until
demand clears supply. The resulting index policy is Whittle-style.

This matters less as a citation than as a reason for confidence: the decomposition is
well-understood, it is provably near-optimal in the large-system limit, and it produces a
shadow price with a direct operational meaning — *the rupee value of one execution slot*.

That price is what makes every decision explainable in a sentence, which is discussed in
[The allocator](/system/allocator).

## Sources

- Stripe — *How we built it: Smart Retries*
- Recurly — retry logic documentation
- Gr4vy — payment retry logic, 2026
- US Patents 11,915,247 and 11,587,093 — optimised dunning using a machine-learned model
- Altman, *Constrained Markov Decision Processes*, chapters 1–3
- Niculescu-Mizil & Caruana, *Predicting Good Probabilities with Supervised Learning*, ICML 2005
