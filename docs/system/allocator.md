# The allocator

::: tip Status
Designed, not yet built. The formal model below is settled and the constraint layer it runs
against is complete and verified. Implementation is scheduled for days 6–7 — see
[Status & roadmap](/project/roadmap).
:::

The core of the system. Given a batch of failed mandates, each with a hard budget of at most
four presentations, and a shared throttled supply of execution slots, decide which mandates
get a slot, at what time, at what amount, and which get nothing.

## Why it is not a scheduler

A scheduler picks times. That framing fails here for a structural reason:
[C8](/constraints/critical) permits only one outstanding commitment per mandate, so the three
retries cannot be assigned up front. The decision is inherently sequential — commit, go blind
for at least 24 hours, observe, re-plan — and information arrives between bets.

What the system actually needs to decide, at each epoch, is whether *this* mandate's next bet
is worth more than the same slot given to some other mandate. That is a pricing question.

## The per-mandate problem

Each mandate is a small partially-observed Markov decision process. The agent never sees the
customer's balance; it maintains a posterior over their liquidity type and updates it from the
outcomes of its own bets.

**State**

```python
(cause, attempts_used, is_first_presentation, amount_due, max_amount,
 category, cycle_end, pending_pdn, contacts_used, issuer, belief)
```

**Objective**

```
V  =  E[ Σ  aₜ · 1{success at t} ]        rupees recovered
    −  c_exec · E[#presentations]         cost of attempts
    −  c_contact · E[#contacts]           cost of customer patience
    +  L · P(mandate LIVE at cycle end)   option value
```

The final term is what produces stopping behaviour. Without it, the optimal policy always
spends every remaining slot, because a slot has no salvage value. With it, a slot is only worth
spending when the expected collection exceeds the expected damage to the mandate — and for a
₹499 plan with fourteen months of life remaining, that bar is high. See
[C9](/constraints/critical#c9-the-mandate-is-the-collateral).

## Solving it

Exact backward induction over a discretised clock. Not reinforcement learning.

<div class="table-scroll">

| Dimension | Size |
| --- | --- |
| Slot grid | 30-minute buckets, **non-peak only** → 33/day, horizon capped at 14 days → ≤ 462 |
| Amount grid | 5 levels of `amount_due`, clipped to `max_amount` and the AFA ceiling |
| Attempts | 0–4 |
| Pending commitment | present / absent |
| Belief | 8 discrete liquidity types |

</div>

Roughly 30,000 nodes per mandate — sub-millisecond in vectorised NumPy.

::: warning Why not RL
Exact dynamic programming is faster here, deterministic, auditable, and it produces the value
function the slot auction needs. Reinforcement learning would be slower to train, harder to
explain, and impossible to verify — a downgrade that would then have to be defended.
:::

## Coupling: the slot auction

Individually the mandates are independent. They are **weakly coupled** through shared scarce
resources — throughput is moderated ([C4](/constraints/)), and an operational blast-radius cap
limits total attempted value per run.

```
maximise   Σᵢ Vᵢ(πᵢ)
subject to Σᵢ nᵢ,w(πᵢ)  ≤  B_w     for each non-peak window w
           Σᵢ E[spendᵢ] ≤  BlastRadius
```

Relax the capacity constraints with multipliers `λ_w ≥ 0`:

```
L(λ) = Σᵢ  max_{πᵢ} [ Vᵢ(πᵢ) − Σ_w λ_w · nᵢ,w(πᵢ) ]  +  Σ_w λ_w B_w
```

The inner maximisation is now independent per mandate. `λ_w` acquires a direct, sayable
meaning: **the rupee price of one execution slot in window w.**

```python
for k in range(K):                                    # K ≈ 20
    plans = [solve_mandate(m, lam) for m in mandates]  # independent, vectorisable
    usage = aggregate_window_usage(plans)
    for w in windows:
        lam[w] = max(0.0, lam[w] + step(k) * (usage[w] - B[w]))
```

Projected subgradient ascent. Converges in seconds for 5,000 mandates.

### The bid

For mandate *i* and window *w*, the bid is the marginal value of being granted a slot:

```
bidᵢ(w)  =  Vᵢ(best plan using a slot in w)  −  Vᵢ(best plan using none)
```

Grant slots in descending bid order until capacity is exhausted. This is a Whittle-index-style
policy for a restless bandit problem, and it makes every decision explainable in one sentence:

> *MND_00412 bid ₹73 for the 06:30 slot on the 2nd. The clearing price was ₹91. It did not get
> the slot and was re-planned into the 14:00 window at a bid of ₹64.*

<div class="diagram">
<svg viewBox="0 0 720 240" role="img" aria-label="Bids sorted descending against a clearing price line; bids above the line receive slots, bids below are re-planned or stopped">
  <text x="24" y="24" font-size="12" font-weight="600">Slot auction, one window</text>

  <line class="line" x1="60" y1="196" x2="690" y2="196"/>
  <line class="line" x1="60" y1="44"  x2="60"  y2="196"/>
  <text class="dim mono" x="18" y="50" font-size="9">₹ bid</text>

  <rect class="box-accent" x="80"  y="60"  width="36" height="136"/>
  <rect class="box-accent" x="126" y="76"  width="36" height="120"/>
  <rect class="box-accent" x="172" y="88"  width="36" height="108"/>
  <rect class="box-accent" x="218" y="104" width="36" height="92"/>
  <rect class="box-accent" x="264" y="118" width="36" height="78"/>
  <rect class="box" x="310" y="140" width="36" height="56"/>
  <rect class="box" x="356" y="152" width="36" height="44"/>
  <rect class="box" x="402" y="162" width="36" height="34"/>
  <rect class="box" x="448" y="172" width="36" height="24"/>
  <rect class="box" x="494" y="180" width="36" height="16"/>

  <line class="line-accent" x1="60" y1="130" x2="690" y2="130" stroke-dasharray="6 4"/>
  <text class="mono" x="596" y="125" font-size="10" font-weight="600">clearing price λ_w</text>

  <text class="dim" x="120" y="216" font-size="10">granted — capacity B_w</text>
  <text class="dim" x="360" y="216" font-size="10">re-planned into another window, or stopped</text>
</svg>
<p class="cap">Mandates bid the marginal rupees a slot is worth to them. The clearing price is the shadow price of regulatory capacity.</p>
</div>

**Ship the auction book.** A table of bids, clearing prices and unfilled demand per window is
an artifact that is immediately legible to a payments person, because it is a market.

## The amount lever

Almost every competing system treats the amount as fixed and optimises only the time. That
leaves money on the table, for a reason that is visible the moment you write down what
"success" means.

A debit of amount `a` at time `t` succeeds when the balance covers it:

```
P(success)  =  P( balance(t) ≥ a )
```

Probability is monotone decreasing in `a`. So expected collection is:

```
EV(a)  =  a · P( balance(t) ≥ a )
```

which has an **interior maximum**.

<div class="diagram">
<svg viewBox="0 0 720 250" role="img" aria-label="Expected value curve against collection amount, peaking at a partial amount below the full amount due">
  <text x="24" y="24" font-size="12" font-weight="600">Expected collection against amount attempted</text>

  <line class="line" x1="70" y1="200" x2="660" y2="200"/>
  <line class="line" x1="70" y1="48"  x2="70"  y2="200"/>

  <path class="line-accent" d="M 70 196 C 150 120, 210 92, 270 96 C 360 104, 450 152, 560 188 C 590 196, 620 199, 650 200"/>

  <line class="line" x1="270" y1="96" x2="270" y2="200" stroke-dasharray="4 3"/>
  <circle cx="270" cy="96" r="4.5" fill="var(--vp-c-brand-1)"/>
  <text class="mono" x="286" y="92" font-size="10" font-weight="600">₹299 × 70% = ₹209</text>

  <line class="line" x1="560" y1="188" x2="560" y2="200" stroke-dasharray="4 3"/>
  <circle cx="560" cy="188" r="4.5" fill="var(--ante-ink-dim)"/>
  <text class="mono dim" x="452" y="176" font-size="10">₹499 × 30% = ₹150</text>

  <text class="dim mono" x="252" y="216" font-size="9">optimum</text>
  <text class="dim mono" x="520" y="216" font-size="9">full amount due</text>
  <text class="dim" x="70" y="238" font-size="10.5">Timing-only policies are pinned to the right-hand edge of this curve.</text>
  <text class="dim mono" x="24" y="54" font-size="9">EV</text>
</svg>
</div>

Collecting ₹299 of a ₹499 debit at 70% beats collecting ₹499 at 30% — ₹209 against ₹150.

::: warning How wide this actually is — measured, not assumed
Building [the simulator](/system/simulator#the-amount-lever-is-narrower-than-the-design-claimed)
qualified this claim. The interior maximum is real, but it is **not** universal:

| Debit ÷ good-day balance | Prefer a partial collection |
| --- | --- |
| under 0.15 | 0.0% |
| 0.35 – 0.80 | 9.4% |
| over 0.80 | 15.2% |

A ₹499 debit against an account holding either ₹5,000 or ₹20 cannot be helped by shrinking
it. The lever pays where the debit is comparable to what the account actually carries — 36%
of the book, and 45% of the value at risk. The example above is drawn from that segment
rather than being typical.

Better to have found that here than in a panel room.
:::

Two further caveats, both handled in the design:

- Partial collection is only legal where the mandate is a variable-amount mandate
  ([C19](/constraints/)). It is gated per-mandate by the `OPS-PARTIAL` guard.
- Some reviewers will regard partial collection as a product decision rather than an
  algorithmic one. Results are therefore reported both with and without the lever enabled.

## What makes it an agent

The breadth of the [action space](/system/action-space) — not just retry or don't. The agent
can lower the amount, spend a free notification instead of a slot, escalate to authentication,
request re-registration, hand off to a human, wait deliberately, or refuse.

Every decision emits a human-readable justification carrying the bid, the clearing price, the
belief state and every constraint verdict. Those strings are the demo.

**Next:** [Action space](/system/action-space).
