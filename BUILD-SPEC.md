# Mandate Recovery Engine — Advanced Build Spec

**Track 03 · AI Revenue Recovery · Razorpay AI Buildathon · Deadline 5 September 2026**

Supersedes `mandate-recovery-build-plan.md` where they conflict. Read `COMPLIANCE.md` first —
it is the source of every constraint referenced here as `C1`…`C24`.

Razorpay's stated bar for this track, verbatim:

> "Don't just identify the problem. Show measured money recovered across a batch, with
> compliant escalation, stopping rules, and an audit trail."

Every section below maps to a clause of that sentence. Nothing here is decoration.

---

## 0. What the research changed

Seven deltas against the original plan. Five are corrections; two are new capabilities.

| # | Original plan said | Research says | Consequence |
|---|---|---|---|
| D1 | Non-peak "hours only", unspecified | Peak = **10:00–13:00** and **17:00–21:30 IST**. Non-peak = 16.5 h/day in three blocks. | The slot grid is now concrete and asymmetric. The 00:00–10:00 block is 10 h wide and contains the post-salary morning. |
| D2 | Notify `>= now + 24h` | Notify within **[T−48h, T−24h]** (C5) | One-sided lock becomes a **two-sided aperture**. Commitment is a scheduling problem, not just a delay. |
| D3 | (not modelled) | **One pending PDN per mandate** (C8) | Retries are **serialized**. You cannot allocate 3 slots up front. This is why it is an MDP. |
| D4 | (not modelled) | **First-presentation failure auto-revokes the mandate** (C9) | Option value enters the objective. Attempt #1 risks the whole LTV. |
| D5 | (not modelled) | **PDN failure ⇒ debit rejected, no charge attempted** (C6) | Notification delivery is a stochastic node in the plan, not a side effect. |
| D6 | Failure rate 8–15% | 8–15% is the *technical* failure rate. Business declines dominate; approval at the largest remitter bank ~30%. | The simulator must be far harsher. A recovery rate above ~35% means your world is fake. |
| D7 | "Everyone else built a timing model" | Confirmed — Stripe Smart Retries is explicitly "predicts the optimal *time* to retry", 500+ attributes, ~8 attempts / 2 weeks. | The reframe holds, and you can now **run their policy** and show it violating Indian law. See §8.3. |

---

## 1. The sharpened thesis

The original framing — "everyone built a timing model, I built an allocator" — is correct but
undersells it. With D3 and D4 the accurate framing is:

> **You get one irrevocable, blind, 24-hours-ahead bet at a time, at most four of them, and
> losing the first one can destroy the asset you are collecting against.**
>
> That is not a retry schedule. That is sequential decision-making under a serialization
> constraint with option value at stake.

**Pitch line:** *"Everyone else optimises when to retry. In India you can't. You get one blind
bet at a time, and the mandate is the collateral. I built the thing that decides whether to
place it."*

The three claims that make it defensible in a panel room:

1. **Serialized commitment** (C8) — the reason a scheduler is the wrong shape of program.
2. **Option value** (C9 + revocation hazard) — the reason the agent stops, and the reason
   stopping is worth money rather than being a safety feature.
3. **Amount is an action, not a constant** (C19) — the reason it beats every timing model
   even when the timing model is perfect. See §5.4. This is the single most differentiated
   idea in the build.

---

## 2. Formal model

### 2.1 Per-mandate state

At each decision epoch the agent holds, for mandate *i*:

```python
@dataclass(frozen=True)
class MandateState:
    mandate_id: str
    status: Literal["LIVE", "REVOKED", "EXPIRED", "PAUSED"]   # C12
    cause: CauseClass                    # from diagnosis, §5.2
    attempts_used: int                   # 0..4, C1
    is_first_presentation: bool          # C9 — the option-value flag
    amount_due_paise: int
    max_amount_paise: int                # mandate cap, C19
    category: Literal["standard", "insurance", "mf_sip", "cc_bill"]  # C15/C16 ceiling
    cycle_end: datetime                  # recovery horizon closes here
    pending_pdn: PDN | None              # C8 — at most one
    contacts_used: int
    issuer_id: str
    belief: np.ndarray                   # posterior over latent liquidity type, §5.3
    history: tuple[Observation, ...]
```

`belief` is what makes this a POMDP rather than an MDP, and it is the honest part: the agent
never sees the customer's balance, only the outcomes of its own bets.

### 2.2 Action space

```python
COMMIT(t, a)        # schedule PDN now for execution at t, amount a
                    #   requires: t in NonPeak(C3), t-now in [24h,48h] (C5),
                    #             no pending PDN or cancel it (C8),
                    #             a <= min(max_amount, amount_due),
                    #             a <= ceiling(category) (C15/C16)
CANCEL_PENDING()    # withdraw the in-flight commitment, free the mandate
NOTIFY_ONLY(t, tmpl)# dunning contact that is not a debit — costs a contact, not a slot
REQUEST_AFA()       # escalate above ceiling, or after AFA_REQUIRED
REQUEST_REMANDATE() # mandate dead — ask for re-registration
ESCALATE_HUMAN()    # hand to collections with a generated summary
STOP(reason)        # refuse to spend further this cycle
WAIT()              # explicit no-op: hold the aperture open, buy information
```

`WAIT()` being an explicit action rather than an absence of action is deliberate — under D2
the aperture moves, so "do nothing today" is a real decision with a real cost, and the audit
log should record it as one.

### 2.3 Transition

For a `COMMIT(t, a)` taken at time τ:

```
1. PDN accepted?        w.p. p_pdn(τ, t)      # C6, C7 — the 23:50 cut-off lives here
      no  -> no presentation, no attempt consumed, calendar time lost
2. Customer opts out in [τ, t]?  w.p. h_opt(·)  # C18 — slot burned for nothing
3. Presentation at t:   success w.p. P(balance(t) >= a | belief, issuer_up(t))
      yes -> collect a; if a < amount_due, residual carries or is written off
      no  -> attempts_used += 1
             if is_first_presentation:  status = REVOKED     # C9 — lose LTV
             revocation hazard increases
4. If attempts_used == 4 or now > cycle_end: cycle closed.
```

### 2.4 Objective

Per mandate, maximise expected **net value including the surviving asset**:

```
V_i = E[ Σ  a_t · 1{success_t} ]                     # rupees recovered this cycle
    − c_exec · E[#presentations]                     # cost of attempts
    − c_contact · E[#contacts]                       # cost of customer patience
    + L_i · P(mandate LIVE at cycle_end)             # option value — the term that stops it
```

`L_i` is the mandate's continuation value: expected discounted net revenue over remaining
cycles. Estimate it as `amount_due × expected_remaining_cycles × margin × survival`. For a
₹499/month OTT plan with 14 months expected remaining life, `L_i` is roughly **14×** the
amount you are chasing. That ratio is the whole argument for stopping, and you should put it
on a slide exactly that way.

### 2.5 The coupling across mandates

Individually each mandate is a small POMDP. They are **weakly coupled** through shared
scarce resources:

```
maximise   Σ_i V_i(π_i)
subject to Σ_i n_{i,w}(π_i)  ≤  B_w      for each non-peak window w   # C4, TPS
           Σ_i E[spend_i]    ≤  BlastRadius                            # ops ceiling
```

This is a **weakly-coupled constrained MDP** — the canonical solution is Lagrangian
relaxation, which decomposes it into independent per-mandate problems plus a price. That is
§3, and it is the technical core of the submission.

---

## 3. The solver — a market for retry slots

### 3.1 Lagrangian decomposition

Relax the window capacity constraints with multipliers `λ_w ≥ 0`:

```
L(λ) = Σ_i  max_{π_i} [ V_i(π_i) − Σ_w λ_w · n_{i,w}(π_i) ]  +  Σ_w λ_w B_w
```

The inner maximisation is now **independent per mandate** and small enough to solve exactly.
`λ_w` has a direct, sayable meaning: **the rupee price of one execution slot in window w.**

Outer loop: projected subgradient ascent on `λ`.

```python
for k in range(K):                       # K ≈ 20 is plenty
    plans = [solve_mandate(m, lam) for m in mandates]     # vectorised / parallel
    usage = aggregate_window_usage(plans)
    for w in windows:
        lam[w] = max(0.0, lam[w] + step(k) * (usage[w] - B[w]))
```

Converges in seconds for 5,000 mandates. On termination, `λ` is the **shadow price vector**
and each mandate's plan is its best response to those prices.

### 3.2 The index — what each mandate bids

For mandate *i* and window *w*, the **bid** is the marginal value of being granted one slot:

```
bid_i(w) = V_i(best plan using a slot in w) − V_i(best plan using no slot in w)
```

Grant slots in descending bid order until `B_w` is exhausted. This is a Whittle-index-style
policy for a restless-bandit problem, and it makes every decision explainable in one sentence:

> *"MND_00412 bid ₹73 for the 06:30 slot on the 2nd. The clearing price was ₹91. It did not
> get the slot and was re-planned into the 14:00 window at a bid of ₹64."*

**Ship the auction book.** A table of bids, clearing prices and unfilled demand per window is
an artifact no other submission will have, and it is immediately legible to a payments person
because it is a market.

### 3.3 The inner solve — finite-horizon backward induction

Per mandate, over a discretised clock:

- **Slot grid**: 30-minute buckets, non-peak only (C3) → **33 buckets/day**, horizon capped at
  14 days or `cycle_end` → ≤ 462 slots.
- **Amount grid**: 5 levels — `{1.00, 0.75, 0.50, 0.30, 0.15} × amount_due`, clipped to
  `max_amount` and to the AFA ceiling (C15/C16).
- **DP state**: `(slot_index, attempts_used, pending_commitment, belief_bucket)`.
- **Recursion**: backward induction from `cycle_end`, taking `max` over `COMMIT(t,a)`, `WAIT`,
  `STOP` at each node.

Size: `462 × 4 × 2 × |B|`. With `|B| = 8` belief buckets that's ~30k nodes per mandate,
sub-millisecond in numpy. 5,000 mandates × 20 dual iterations is comfortably under a minute
on a laptop. **Do not reach for RL.** Exact DP is faster, deterministic, auditable, and it
gives you the value function you need for the bids in §3.2. RL here would be a downgrade you
would have to defend.

### 3.4 Why this beats a scheduler — the sentence for the panel

> "A scheduler picks times. This runs an internal auction where mandates bid the marginal
> rupees a slot is worth to them, priced against a regulatory supply limit, and it refuses to
> sell a slot below the option value of the mandate that would spend it."

---

## 4. Architecture

```
                    ┌──────────────────────────────┐
                    │  0. WORLD SIMULATOR          │
                    │  latent balance process      │
                    │  + issuer downtime + churn   │
                    └────────────┬─────────────────┘
                                 │ FailureEvent
   ┌──────────────────┐          ▼
   │ RAZORPAY TEST    │  ┌──────────────────────────────┐
   │ MODE WEBHOOKS    │─▶│  1. INGEST  signature verify │
   │ payment.failed   │  │     → canonical FailureEvent │
   │ subscription.*   │  └────────────┬─────────────────┘
   └──────────────────┘               ▼
                        ┌──────────────────────────────┐
                        │  2. DIAGNOSE  rules ratchet  │
                        │     + LLM adjudicator        │
                        └────────────┬─────────────────┘
                                     ▼
                        ┌──────────────────────────────┐
                        │  3. BELIEF FILTER            │
                        │  posterior over latent       │
                        │  liquidity type              │
                        └────────────┬─────────────────┘
                                     ▼
                        ┌──────────────────────────────┐
                        │  4. PREDICT  P(succ | t, a,  │
                        │     belief, issuer) + isotonic│
                        └────────────┬─────────────────┘
                                     ▼
   ┌──────────────────┐   ┌──────────────────────────────┐
   │  5. CONSTRAINTS  │──▶│  6. POLICY                    │
   │  C1..C24 pure fn │   │  per-mandate DP + dual prices │
   │  model-checked   │◀──│  → bids → slot auction        │
   └──────────────────┘   └────────────┬─────────────────┘
                                       ▼
                        ┌──────────────────────────────┐
                        │  7. ACT  WAL · idempotency   │
                        │  ceilings · kill switch      │
                        │  hash-chained receipts       │
                        └────────────┬─────────────────┘
                                     ▼
                        ┌──────────────────────────────┐
                        │  8. EVAL  vs 4 baselines     │
                        │  + oracle bound + regret     │
                        └──────────────────────────────┘
```

Build order is **5 → 7 → 0 → 8 → 6 → 3/4 → 2 → 1**. Constraints and the action layer come
first because they are what makes the rest trustworthy, and because they are the parts that
cannot be faked on day 12.

---

## 5. Component implementation

### 5.1 `sim/` — the world simulator

The most important component, because every number you report is graded against it. Build it
adversarially.

**Latent customer model.** Not IID noise, not a random walk. A **marked point process**:

```python
class LatentCustomer:
    liquidity_type: int         # 0..7: salaried-1st, salaried-7th, gig-irregular,
                                #       student-parental, business-lumpy, thin-file, ...
    income_days: list[int]      # e.g. [1] or [7] or Poisson-scattered for gig
    income_amount: Gamma
    spend_rate: float           # compound Poisson drawdown
    buffer_paise: int           # baseline balance floor
    churn_intent: float         # hidden; increases with contacts and failures
```

Balance is simulated forward as: income arrivals on `income_days`, compound-Poisson spend
between them. A debit of amount `a` at time `t` succeeds iff `balance(t) >= a` **and** the
issuer is up. That single line is what makes both timing *and* amount matter, and it is why
§5.4 works.

**Correlated failure structure** (all required):
- Issuer downtime as a **Markov-modulated** on/off process per issuer, clustered in time.
- Insufficient funds clustered at month-end / pre-salary — falls out of the balance process
  automatically. Do not hand-code it.
- Revocation spikes after repeated failed debits and after excessive contact.
- A **hard stop-list**: closed accounts, revoked mandates, expired mandates, genuine churn.
  Target ~12–18% of the failed batch as genuinely unrecoverable. If your stop list is empty,
  the simulator is flattering you.

**Calibration targets** (from `COMPLIANCE.md` §C):
- Overall first-attempt approval on due date: **~30–40%**.
- Post-recovery total collection: **must land below ~45%**. If your agent recovers 80%, throw
  the simulator away.
- Revocation rate under a naive aggressive policy: material, so the option-value term bites.

**Reproducibility.** `python -m sim.generate --seed 42` reproduces the batch byte-for-byte.
Seeds 0–7 train, 100–109 evaluate. **The policy never sees an eval seed.** State this in the
README and make the seed split a constant in code so it can be checked.

### 5.2 `diagnose/` — rules ratchet + LLM

Cause classes (unchanged from the plan, plus two the research surfaced):

| Class | Trigger | Retry-recoverable |
|---|---|---|
| `TRANSIENT_ISSUER` | `bank_technical_error`, `gateway_technical_error`, U-series | Yes, quickly |
| `INSUFFICIENT_FUNDS` | `insufficient_funds`, Z9 | Yes — timing and amount are everything |
| `LIMIT_BREACH` | per-txn/daily cap | Yes — lower amount or different slot |
| `PDN_MISSING` | `PRE_DEBIT_NOTIFICATION_NOT_FOUND/_NOT_SENT` (C6) | **Yes — and it's your own bug** |
| `MANDATE_EXPIRED` | validity lapsed (C21) | No — re-registration |
| `MANDATE_REVOKED` | customer cancelled (C18) | No — retrying is abusive |
| `AFA_REQUIRED` | above ceiling (C15/C16) | No — needs customer action |
| `VPA_INVALID` | `invalid_vpa`, `vpa_resolution_failed` | No — needs customer action |
| `TERMINAL` | account closed / frozen | No — stop permanently |

`PDN_MISSING` deserves its own class precisely because it is self-inflicted. An agent that
detects and reports *its own* compliance failures is a better artifact than one that cannot.

**The ratchet.** Deterministic rules run first and produce `(class, confidence)`. The LLM only
adjudicates free-text or unmapped responses and returns strict JSON. Then:

```python
if rule.confidence >= HIGH and llm.klass != rule.klass:
    log_disagreement(rule, llm)      # pitch artifact
    return rule.klass                # rule always wins
if rule.klass in TERMINAL_CLASSES:
    return rule.klass                # one-way ratchet: LLM can never un-terminal a class
```

The LLM can *downgrade* recoverability, never upgrade it. Say that sentence out loud in the
pitch. Ship `diagnose/disagreements.jsonl` in the repo.

### 5.3 `belief/` — the latent-type filter

The agent cannot see balance. Give it a **discrete Bayesian filter over the 8 liquidity
types**, updated from its own observations:

```python
def update(belief, obs):                       # obs = (t, amount, outcome)
    like = np.array([P_success(k, obs.t, obs.amount) for k in TYPES])
    post = belief * (like if obs.outcome else 1 - like)
    return post / post.sum()
```

Exact, 8 floats per mandate, no training required, and it produces a genuinely useful
inference: after two failures on the 3rd and one success on the 9th, the posterior collapses
onto "salaried, paid around the 7th" and the agent starts committing to the 8th at 06:30.
**Put that belief trajectory in the demo video.** It is the moment the system stops looking
like a rules engine.

> Keep the agent's type space *structurally different* from the simulator's generative
> parameters — different bucket boundaries, no shared constants. Otherwise you are grading a
> model against features you handed it, and the panel will find it.

### 5.4 `predict/` — P(success | t, a, belief) and why amount is the whole game

Train a gradient-boosted tree on **logged transactions only** — never on latent state.

Features: hours since last failure, attempt index, day-of-month, day-of-week, minutes into
the non-peak window, issuer 7-day success rate, customer historical recovery rate,
`amount / customer_median_debit`, belief vector (8 dims), PDN opened flag, cycle days
remaining.

**Calibration, not accuracy.** Isotonic regression on a held-out fold. Report reliability
diagram + Brier score + Expected Calibration Error. You are feeding an expected-value
calculation; 0.3 must mean 30%.

**The amount lever.** Because success is `P(balance(t) >= a)`, the probability is *monotone
decreasing in a*, and expected collection is:

```
EV(a) = a · P(balance(t) >= a)
```

This has an **interior maximum**. Collecting ₹299 of a ₹499 debit at 70% beats collecting
₹499 at 30% (₹209 vs ₹150). Every timing-only model in the competition is stuck on the right
edge of this curve.

**Deliverable:** an `EV(a)` curve plot for a real simulated customer, with the timing-only
policy marked at `a = amount_due` and your policy marked at the optimum. One chart, and the
entire differentiation argument is made without a word.

Gate it properly: partial collection is only legal where the plan/mandate permits variable
amounts (C19), so make it a per-mandate flag and report results **both** with and without the
lever enabled. Some panels will consider partial collection a product decision, not an
algorithmic one — have the split-out ready.

### 5.5 `constraints/` — pure, and *verified* rather than tested

```python
def is_permitted(action: Action, state: MandateState, clock: datetime)
        -> Allow | Veto(rule_id, human_reason)
```

No ML, no LLM, no I/O, no clock reads inside. Every veto names the rule id (`C5`, `C8`, …).

Two levels of assurance, and the second is what wins:

**Level 1 — property tests (Hypothesis).** Assert over generated action sequences: never a
5th attempt (C1); never a peak-hour execution (C2/C3); never a commit outside `[T−48h, T−24h]`
(C5); never two pending PDNs (C8); never an amount above ceiling (C15/C16); never a debit
without an accepted PDN (C6).

**Level 2 — exhaustive model checking.** The reachable state space is *small*: status (4) ×
attempts (5) × pending (2) × clock bucket (462) × amount level (5). BFS the entire reachable
graph under all actions and assert zero violating states. This is a few hundred lines and an
afternoon.

The claim then upgrades from "we tested it" to:

> **"We enumerated all N reachable (state, action, clock) triples. Zero violate NPCI or RBI.
> Not sampled — enumerated."**

Print `N` in CI output. That is the compliance slide.

Write this component **before the policy exists.** Day 1–2.

### 5.6 `act/` — the money path

This is where a security instinct outperforms anyone's ML.

- **Idempotency keys** — `sha256(mandate_id | cycle | attempt_index | scheduled_ts)`. A
  replayed decision can never double-debit.
- **Write-ahead log** — intent durably fsync'd *before* any side effect. On restart, replay
  the WAL, reconcile against the ledger, resume.
- **Two-phase commit against the PDN** — because of C6/C8, the sequence is
  `WAL(intent) → PDN → record presentations_sequence_id → WAL(committed) → present`. A crash
  at any point must be recoverable, and a crash between PDN and presentation must **not**
  leak a second PDN (which would cancel the first — C8).
- **Blast radius cap** — hard limits on executions/run and ₹ attempted/run. Stop, not warn.
- **Per-customer contact cap** — independent of the retry cap. This is the guard against the
  agent discovering that spamming notifications raises recovery.
- **Kill switch** — one flag halts pending actions; in-flight drains safely.
- **Hash-chained decision receipts** — each entry carries `prev_hash` and records: input
  digest, model + policy version, belief vector, `λ_w` prices and the bid, every constraint
  verdict, chosen action, expected value, and the human-readable justification.
  `--replay <run_id>` must reconstruct the run bit-identically.
- **Dry-run is the default.** Real execution requires an explicit flag.

**The 30-second demo:** start a batch, `kill -9` mid-run, restart, show the WAL reconciling,
zero duplicate debits, zero orphan PDNs, hash chain intact. It ends the question of whether
you can be trusted with money code.

### 5.7 `ingest/` — real Razorpay test-mode webhooks

Thin but non-negotiable for credibility.

- Verify signatures with `X-Razorpay-Signature` over the **raw** body. Do not parse before
  verifying — Razorpay's docs call this out explicitly and it is a real bug class.
- Subscribe to `payment.failed`, `subscription.charged`, `subscription.pending`,
  `subscription.halted`, `subscription.cancelled`, `subscription.paused`.
- Map `error_code` / `error_description` into the taxonomy in §5.2.
- Test mode: use **"Charge this Now"** on the Dashboard and choose the outcome. Failure moves
  the subscription to `pending` and fires `subscription.pending`.
- **Two ambushes:** test-mode card tokens are valid **3 days only**, and webhook endpoints
  must be public URLs on port 80/443 — localhost is rejected, so use a tunnel.
- Replay-protect: store `x-razorpay-event-id`, reject duplicates.

Record a terminal capture of a real `payment.failed` flowing end-to-end into a decision. Ten
seconds of video, and the "is this only a simulation?" question never gets asked.

### 5.8 `llm/` — where the model is allowed to touch anything

Four uses. None of them move money.

1. **Diagnosis adjudication** (§5.2) — free-text only, ratchet-limited.
2. **Notification copy** — the PDN is mandatory and free (C20), so it is a **recovery
   instrument**, not overhead. Have the LLM author a template bank; select among templates
   with a contextual bandit (LinUCB). The LLM writes; the bandit chooses; the constraint layer
   approves the content against C13.
3. **Run narration** — render the structured receipt into English. Grounded in the receipt, so
   it cannot invent a decision that did not happen.
4. **(Stretch) Regulatory diff agent** — given new circular text, propose constraint-layer
   changes as a diff plus failing tests. Shows the compliance layer is maintainable rather
   than hand-carved. Cut this first if time is short.

**Say in the pitch:** *"The LLM classifies, writes copy, and explains. It never decides to
move money, and the rule layer can override it but it can never override the rule layer."*

---

## 6. Evaluation protocol

Held-out seeds 100–109. Ten seeds, all policies run on **the same** seeds.

### 6.1 Baselines — four, not three

| # | Baseline | Purpose |
|---|---|---|
| B0 | No retry | Floor |
| B1 | Fixed schedule +24h / +72h / +168h | The genuine industry heuristic and a strong opponent |
| B2 | Greedy EV — always take the highest-probability legal slot now | Proves the *allocation* framing earns its keep |
| B3 | **Stripe-style**: ~8 attempts over 2 weeks, timing-optimised, no Indian constraints | Proves the *regulatory* framing earns its keep |

**B3 is the slide that gets remembered.** Run it, count its violations, and put up a bar chart:

> *Stripe's published default policy, run against Indian rules: N NPCI/RBI violations,
> M mandates auto-revoked. Ours: zero.*

You are not attacking Stripe — their policy is correct for their market. You are showing the
transplant fails, which is exactly the insight the track is asking for.

### 6.2 Variance reduction — do this or your uplift will be noise

Use **common random numbers**: identical latent customers, identical income arrivals,
identical issuer downtime across all policies on a given seed. Only the policy differs. Then
compare **paired** differences per seed.

Report mean ± 95% bootstrap CI on the paired difference, plus a Wilcoxon signed-rank test.
Without pairing, a 3% uplift on 10 seeds is statistically invisible and an experienced panel
will ask.

### 6.3 The oracle bound — the most honest number you can report

Run a **clairvoyant policy** that sees the latent balance trajectory and still obeys every
constraint. It is the true ceiling. Then report:

```
recovery_efficiency = (yours − B1) / (oracle − B1)
```

> *"We capture 71% of the recovery that is achievable by any policy that obeys Indian law,
> against 34% for the fixed-schedule heuristic."*

That is a far stronger and far more defensible statement than "+12% vs baseline", and almost
nobody at a student hackathon reports a regret bound.

### 6.4 Metric table

| Metric | Why |
|---|---|
| Rupees recovered | Headline, vs all four baselines |
| Recovery cost | Attempts, notifications, contacts |
| **Net value** | Recovered − cost. The number that matters. |
| **Mandates surviving at cycle end** | Directly measures the option-value thesis (C9) |
| Slot efficiency | ₹ recovered per execution slot spent |
| **Recovery efficiency vs oracle** | §6.3 |
| Stop list | Count and ₹ deliberately refused, by reason |
| **Realized regret of stopping** | Of the money refused, how much was actually recoverable — the simulator knows |
| Constraint violations | Must be exactly zero. Any non-zero is a bug, not a tradeoff. |
| Calibration | Brier + ECE + reliability diagram |
| Diagnosis accuracy | Confusion matrix vs ground truth |
| Failure list | Cases handled badly, and why |

### 6.5 A statistical guarantee on the stop list

The stop list is the most distinctive artifact in the build. Upgrade it from a log to a claim.

Use **conformal risk control** on a calibration split: choose the stop threshold `τ` such that
the false-stop rate — money refused that was in fact recoverable — is bounded at level α with
distribution-free finite-sample validity.

> *"At most 5% of the rupees we refuse to chase were actually collectable, and that bound
> holds without assuming our model is correct."*

That sentence is doing something no other submission will be doing: putting a guarantee on a
refusal. It is ~80 lines of code (split-conformal thresholding) and it is the highest
prestige-per-hour item in this document.

### 6.6 Red team — attack your own agent

Ship `redteam/`. Each test asserts the agent *fails to exploit* a loophole:

- **Notification spam** — reward function credits recovery only; does the contact cap hold?
- **Amount slicing** — can it split one debit into many small ones to game success rate?
  (C1 counts presentations, so it must not.)
- **Peak-hour drift** — feed a timezone-confused clock; does C2/C3 still veto?
- **PDN thrash** — can it cancel and re-issue PDNs to fish for a better slot? (C8 cost.)
- **Terminal-class laundering** — can a crafted free-text bank response get the LLM to
  reclassify `TERMINAL` as recoverable? (Ratchet must hold.)
- **Clock skew / replay** — replayed webhook, duplicate event id, WAL replay after crash.

Razorpay is hiring into a risk-adjacent org. Showing you attacked your own agent and it held
is a stronger signal than any accuracy number.

---

## 7. Repo layout

```
mandate-recovery/
├── README.md              # thesis, results table, honest limitations
├── ARCHITECTURE.md        # diagram + component contracts
├── COMPLIANCE.md          # constraints + sources + verification status  ← exists
├── BUILD-SPEC.md          # this file
├── sim/                   # latent balance process, issuer downtime, churn
├── ingest/                # webhook consumer, signature verify, normaliser
├── diagnose/              # rules ratchet + LLM adjudicator + disagreements.jsonl
├── belief/                # discrete Bayesian liquidity-type filter
├── predict/               # GBDT + isotonic calibration + EV(a) curves
├── constraints/           # pure fns + property tests + exhaustive model checker
├── policy/                # per-mandate DP, dual ascent, slot auction, baselines B0–B3
├── act/                   # WAL, idempotency, ceilings, kill switch, receipts
├── eval/                  # harness, CRN, oracle, bootstrap CIs, conformal stop bound
├── audit/                 # hash-chained log + --replay
├── redteam/               # adversarial suite
├── tests/                 # runs in CI
└── demo/                  # make demo
```

`make demo` must reproduce the headline table from a clean clone. If a reviewer cannot
reproduce your number in one command, your number does not exist.

---

## 8. Twelve days — 24 Aug to 5 Sept

The original plan assumed fourteen. You have **twelve**. Freeze is day 10, not day 13.

| Day | Date | Build | Gate |
|---|---|---|---|
| 1 | 25 Aug | Constraint layer C1–C24 + property tests + **exhaustive model checker** | CI prints "N states enumerated, 0 violations" |
| 2 | 26 Aug | Action layer: WAL, idempotency, ceilings, kill switch, hash-chained receipts | `kill -9` → restart → zero duplicates |
| 3 | 27 Aug | Simulator: latent balance process, issuer downtime, churn, stop-list | Base rates match §5.1 targets |
| 4 | 28 Aug | Eval harness + B0/B1 baselines + CRN + oracle policy | Results table prints with CIs |
| 5 | 29 Aug | Belief filter + P(success) model + isotonic calibration | Reliability diagram + `EV(a)` curve |
| 6 | 30 Aug | **Policy: per-mandate DP** | Beats B1 on a single seed |
| 7 | 31 Aug | **Dual ascent + slot auction** + B2/B3 baselines | Auction book prints; B3 violation count published |
| 8 | 1 Sept | Diagnosis: rules ratchet + LLM + disagreement log | Confusion matrix |
| 9 | 2 Sept | Razorpay test-mode webhooks + signature verification; red-team suite | Real `payment.failed` → decision; red team passes |
| 10 | 3 Sept | **CODE FREEZE.** Conformal stop bound, README, ARCHITECTURE, 10-seed run | Clean clone → `make demo` |
| 11 | 4 Sept | Pitch video, architecture diagram | Recorded |
| 12 | 5 Sept | Buffer + submit | Submitted |

**Cut order if you slip** — cut from the bottom, never from the top:

1. LLM adjudicator → rules-only diagnosis (thesis survives intact)
2. Notification-copy bandit → fixed best template
3. Regulatory diff agent → already optional
4. B3 Stripe baseline → keep if at all possible, it is your best slide
5. Conformal stop bound → keep if at all possible, it is your most distinctive claim

**Never cut:** constraint layer, model checker, action layer, simulator honesty, oracle bound.

---

## 9. Five-minute pitch

| Time | Content |
|---|---|
| 0:00–0:35 | **The constraint.** 3 retries. Non-peak only, 10–13 and 17–21:30 barred. Notify in a 48–24h window. One pending notification per mandate. First failure can revoke the mandate. |
| 0:35–1:05 | **B3 slide.** Stripe's published default, run against Indian rules: N violations, M mandates destroyed. "The Western playbook is not suboptimal here. It is illegal." |
| 1:05–1:45 | **The reframe.** One blind bet at a time, mandate as collateral. Not a timing model — a priced allocator with option value. Show the auction book. |
| 1:45–2:35 | **Live demo.** Batch runs, decisions with reason strings, belief posterior collapsing onto a salary date, `EV(a)` curve choosing a partial collection. |
| 2:35–3:05 | **Crash demo.** `kill -9`, restart, zero double-charges, zero orphan PDNs, hash chain intact. |
| 3:05–4:00 | **Numbers.** Net value vs four baselines, 10 paired seeds with CIs. Recovery efficiency vs oracle. Then the **stop list** with the conformal bound. |
| 4:00–4:35 | **What it gets wrong.** Two real weaknesses, named. |
| 4:35–5:00 | **What I'd build with production data.** |

Razorpay explicitly asks candidates to "explain what broke during development and how they
recovered from it." Prepare a real answer — an actual bug with a real diagnosis. Do not
improvise this; it is a graded question.

---

## 10. Ways this dies

- **Building the policy before the constraint layer.** Then compliance is a filter you bolted
  on, and it shows in the commit history. Day 1 or it is fake.
- **A simulator that flatters you.** Above ~45% total recovery and the world is wrong. Tune
  until the numbers are uncomfortable.
- **Grading the model on features you generated it from.** Keep the agent's belief space
  structurally different from the simulator's parameters.
- **Quoting an unverified regulatory number.** Especially C9 and the 74% decline figure.
  Getting a real constraint wrong in front of this panel is fatal; getting it right and
  showing your verification trail is a large win. Work §D of `COMPLIANCE.md`.
- **Unpaired seeds.** Your uplift disappears into variance and you cannot claim it.
- **LLM in the money path.** It classifies, writes, explains. Never decides.
- **Scope creep** into checkout abandonment or B2B receivables. One loss class, closed
  completely.
- **Still building on day 12.** Freeze on day 10.
