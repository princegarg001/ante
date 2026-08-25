# Mandate Recovery Engine — Build Plan

**Track 03 · Razorpay AI Buildathon · Deadline 5 September 2026**

A constrained-budget recovery agent for failed UPI Autopay / e-mandate debits.

---

## 1. The thesis

Every "smart retry" product on the market — Stripe Smart Retries, Recurly Intelligent
Retries, Slicker, Gr4vy — solves the same problem: *when should I retry this failed card
payment?* They assume retries are cheap and roughly unlimited. Stripe's own default is
around eight attempts across two weeks.

**In India that assumption is illegal.**

NPCI caps UPI Autopay at **one original execution plus three retries per mandate per
cycle**. After the fourth attempt the cycle is dead. Executions must also land in
non-peak windows, and PSPs are throttled to a moderated TPS.

So the real Indian problem is not *when to retry*. It is:

> Given exactly 3 non-refundable retry slots, a set of permitted execution windows, a
> regulatory notification obligation, and a heterogeneous batch of failed mandates —
> **how do I allocate those slots to maximise recovered rupees minus recovery cost?**

That is a constrained sequential decision problem, not a timing predictor. Building it
correctly requires modelling scarcity, opportunity cost, and stopping rules. That is the
part that will make a panel sit up, and it is the part no imported Western playbook
covers.

**Positioning line for the pitch:** *"Retry budgets are scarce and regulated in India.
Everyone else built a timing model. I built an allocator."*

---

## 2. Hard constraints — the regulatory spine

These are not decoration. Encode them as a constraint layer the policy engine cannot
override, and surface violations as test failures. **Verify every one of these yourself
against primary sources before you ship — do not take my numbers on faith.**

| Constraint | Value | Source to verify |
|---|---|---|
| Retry cap | 1 execution + 3 retries per mandate per cycle | NPCI circular, Aug 2025 |
| Execution window | Non-peak hours only | NPCI circular, Aug 2025 |
| Throughput | Moderated TPS, rate limiters applied | NPCI circular, Aug 2025 |
| Pre-debit notification | ≥ 24 hours before every debit | RBI E-mandate Framework 2026, §pre-transaction |
| Notification contents | Merchant name, amount, debit date/time, mandate reference, reason for debit | RBI 2026 |
| Post-debit notification | Required after every debit | RBI 2026 |
| AFA-free ceiling | ₹15,000 per transaction | RBI 2026 |
| Raised ceiling | ₹1,00,000 for insurance premiums, mutual funds, credit card bills | RBI 2026 |
| Customer opt-out | Must be possible per-transaction and per-mandate, via AFA | RBI 2026 |
| Customer charges | Zero — cannot bill the customer for e-mandate facility | RBI 2026 |
| Grievance redressal | Mechanism must exist | RBI 2026 |

**The 24-hour notification rule is the sharpest constraint in the whole system.** It means
you cannot decide to retry at T and execute at T. Every retry must be *committed* at least
24 hours in advance, with the amount and time fixed and disclosed. Your agent is therefore
planning under a 24-hour lookahead lock — it cannot react to information arriving inside
that window, and if the customer opts out during it, the slot is consumed for nothing.

Almost no submission will model this. Model it and say so out loud.

**Second-order consequence worth building:** because notification is mandatory and free,
the notification *is* a recovery instrument, not just compliance overhead. A well-timed,
well-worded pre-debit notice raises the probability the customer tops up before the debit.
Your agent should treat notification copy and timing as part of the action space, not as a
side effect.

---

## 3. Architecture

Seven components. Each one independently runnable and independently demoable — so if you
run out of time, you ship fewer components fully built rather than seven at 60%.

```
                        ┌─────────────────────────┐
                        │   1. WORLD SIMULATOR    │
                        │  synthetic mandate book │
                        │  + failure generator    │
                        └───────────┬─────────────┘
                                    │ failed debit events
                                    ▼
┌──────────────────┐    ┌─────────────────────────┐
│  RAZORPAY TEST   │───▶│   2. INGESTION LAYER    │
│  MODE WEBHOOKS   │    │  normalise → canonical  │
│  payment.failed  │    │  FailureEvent schema    │
│  subscription.*  │    └───────────┬─────────────┘
└──────────────────┘                │
                                    ▼
                        ┌─────────────────────────┐
                        │   3. DIAGNOSIS LAYER    │
                        │  rules floor + LLM      │
                        │  → cause + confidence   │
                        └───────────┬─────────────┘
                                    │
                                    ▼
                        ┌─────────────────────────┐
                        │  4. RECOVERY PROBABILITY│
                        │  P(success | cause,     │
                        │    slot, customer, t)   │
                        └───────────┬─────────────┘
                                    │
                                    ▼
   ┌────────────────┐   ┌─────────────────────────┐
   │ 5. CONSTRAINT  │──▶│   6. POLICY ENGINE      │
   │    LAYER       │   │  allocate 3 slots to    │
   │ NPCI + RBI     │◀──│  maximise EV − cost     │
   │ hard veto      │   │  + stopping rules       │
   └────────────────┘   └───────────┬─────────────┘
                                    │ Decision + reason
                                    ▼
                        ┌─────────────────────────┐
                        │   7. ACTION LAYER       │
                        │ idempotency · ceilings  │
                        │ contact caps · WAL      │
                        │ kill switch · audit log │
                        └───────────┬─────────────┘
                                    │
                                    ▼
                        ┌─────────────────────────┐
                        │   EVALUATION HARNESS    │
                        │  recovered · spent ·    │
                        │  refused · violations   │
                        └─────────────────────────┘
```

### 3.1 World Simulator

The foundation. Everything else is graded against it, so build it first and make it
adversarial against yourself.

Generate a mandate book of 2,000–5,000 subscriptions with:

- **Correlated failure modes, not IID noise.** Issuer downtime clusters in time.
  Insufficient-funds clusters around the 1st–3rd and salary dates. Mandate revocations
  spike after a price change.
- **Latent customer state the agent cannot see.** Each simulated customer has a hidden
  balance trajectory and a hidden intent-to-churn. Recovery probability emerges from that
  latent state; your agent only sees failure codes and history. This is what makes the
  evaluation honest — you are not grading a model against features you handed it.
- **Realistic base rates.** UPI Autopay failure rates are reported around 8–15% versus
  2–3% for card mandates, because UPI debits are stateless and require real-time bank
  approval. Cite your source in the README and let the panel check it.
- **Adversarial cases you deliberately cannot recover:** closed accounts, revoked
  mandates, genuine churn intent, expired mandates. If your stop-list is empty, your
  simulator is too kind and the panel will know.

Seed-controlled and reproducible. `python -m sim.generate --seed 42` must reproduce the
exact batch in your results table. Ship the seed.

### 3.2 Ingestion Layer

Normalises two sources into one `FailureEvent`:

1. Simulator output (bulk, for evaluation).
2. **Real Razorpay test-mode webhooks** (thin, for credibility).

Razorpay's test mode lets you trigger a charge on a subscription and *choose the outcome*,
which fires the real failure webhooks. Subscribe to `payment.failed`,
`subscription.charged`, `subscription.pending`, `subscription.halted`. Verify webhook
signatures properly — that alone signals you have shipped a webhook consumer before.

Note two test-mode limits so they don't ambush you: card tokens are valid 3 days, and
webhook endpoints must be public URLs on port 80/443 (use a tunnel; localhost is rejected).

### 3.3 Diagnosis Layer

Classify each failure into a cause class. Cause determines everything downstream.

| Class | Meaning | Recoverable by retry? |
|---|---|---|
| `TRANSIENT_ISSUER` | Bank system down, timeout | Yes, quickly |
| `INSUFFICIENT_FUNDS` | Balance short | Yes, but timing is everything |
| `LIMIT_BREACH` | Daily/per-txn cap hit | Yes, different slot or lower amount |
| `MANDATE_EXPIRED` | Validity lapsed | No — needs re-registration |
| `MANDATE_REVOKED` | Customer cancelled | No — retry is abusive |
| `AFA_REQUIRED` | Above ceiling, needs PIN | No — needs customer action |
| `TERMINAL` | Account closed/frozen | No — stop permanently |

**Architecture: deterministic rule floor + LLM adjudication on top.** Rules map known
error codes with certainty. The LLM handles ambiguous or free-text bank responses and
outputs structured JSON with a confidence score. If the LLM disagrees with a
high-confidence rule, **the rule wins and you log the disagreement**. That disagreement log
is a great pitch artifact — it shows you know where LLMs belong in a money system.

Never let the LLM classify something as recoverable that the rule layer marked terminal.
One-way ratchet.

### 3.4 Recovery Probability Model

`P(success | cause, slot_time, customer_history, amount, issuer, attempt_index)`

Gradient-boosted trees on simulator-generated history is the right call — interpretable
feature importances, fast to train, no GPU. Logistic regression as a baseline you report
alongside it.

Key features: hours since last failure, attempt index, day-of-month, issuer recent success
rate, customer's historical recovery rate, amount relative to customer's typical debit,
whether a pre-debit notice was opened.

**Calibration matters more than accuracy here.** You are feeding this into an expected-value
calculation, so a probability of 0.3 must actually mean 30%. Report a reliability diagram
and Brier score, not just AUC. Very few students will do this and it is exactly the kind of
rigour a fintech panel recognises.

### 3.5 Constraint Layer

A pure function: `is_permitted(action, mandate_state, clock) -> Allow | Veto(reason)`.

No ML. No LLM. Deterministic, unit-tested to death, and structurally impossible for the
policy engine to bypass — the policy proposes, the constraint layer disposes. Every veto is
logged with the rule that fired.

Write the constraint tests **first**, before the policy engine exists. Property-based tests
(Hypothesis) that assert *no generated sequence of decisions ever produces a 5th attempt,
a peak-hour execution, or a debit without a 24-hour-prior notice.*

That test suite is a slide in your pitch deck.

### 3.6 Policy Engine — the core

Given a mandate with `k` retries remaining and a set of permitted future slots, choose an
allocation.

Formally, per mandate:

```
maximise   Σ  P(success | slot_i) × amount × (1 - discount(delay_i))
           i∈S
subject to |S| ≤ retries_remaining
           slot_i ∈ permitted_windows
           slot_i ≥ now + 24h              (notification lock)
           Σ cost(slot_i) ≤ mandate_cost_budget
           contact_count ≤ contact_cap
```

Then across the batch, allocate the global execution budget (TPS throttling means you
cannot fire everything at once) by expected marginal value per slot.

**Action space** — richer than retry/don't-retry, and this breadth is what makes it an
*agent* rather than a scheduler:

- `RETRY_AT(t)` — schedule a debit in a permitted window
- `RETRY_REDUCED(t, amount)` — partial collection where the plan permits it
- `NOTIFY_AND_WAIT(t, template)` — spend the free notification, not a retry slot
- `REQUEST_AFA` — escalate to customer authentication
- `REQUEST_REMANDATE` — mandate is dead, ask for re-registration
- `ESCALATE_HUMAN` — hand to a collections agent with a summary
- `STOP(reason)` — refuse to spend anything further

**Stopping rules are a first-class feature.** Stop when: cause is terminal; expected value
of the best remaining slot is below cost; customer has been contacted `n` times this cycle;
churn-intent signal exceeds threshold. Every stop is logged with the reason and the money
deliberately left on the table.

Every decision emits a human-readable justification string. Those strings are your pitch
video.

**Baselines you must beat, and report honestly:**

1. **No retry** — floor.
2. **Fixed schedule**: retry at +24h, +72h, +168h. This is the sensible industry heuristic
   and it is genuinely strong; published Paddle data shows retrying at 24h instead of 2h
   improved recovery by around 6.5%. If you beat it by 3% you say 3%, not "dramatically".
3. **Greedy EV** — always take the highest-probability slot immediately, no budget
   reasoning. Beating this is what proves the allocation framing earns its keep.

If your policy loses to a baseline on some segment, **put that in the README**. Panels trust
people who report losses.

### 3.7 Action Layer — build this before the policy

This is where your security instinct is worth more than anyone else's ML.

- **Idempotency keys** on every execution. A replayed decision must never double-debit.
  Demo this: kill the process mid-batch, restart, show zero duplicates.
- **Write-ahead log.** Intent is durably recorded *before* the side effect, so a crash
  between decision and execution is recoverable and auditable.
- **Spend ceiling and blast radius cap.** Agent cannot exceed `N` executions or `₹X`
  attempted value per run. Hard stop, not a warning.
- **Per-customer contact cap.** Independent of retry cap. Protects against the agent
  discovering that spamming notifications raises recovery.
- **Kill switch.** Single flag halts all pending actions; in-flight actions drain safely.
- **Append-only audit log.** Every decision, every veto, every action, every outcome —
  timestamped, hash-chained, replayable. `--replay <run_id>` must reconstruct the run.
- **Dry-run mode as the default.** Executing for real requires an explicit flag.

Demo script for the video: start a run, `kill -9` it halfway, restart, show the audit log
reconciling and zero double-charges. Thirty seconds, and it ends the conversation about
whether you can be trusted with money code.

---

## 4. Evaluation protocol

Run on a **held-out simulator seed the policy never trained on**. Report:

| Metric | Why it matters |
|---|---|
| **Rupees recovered** | The headline. vs all three baselines. |
| **Recovery cost** | Attempts consumed, notifications sent, contacts burned. |
| **Net value** | Recovered − cost. The number that actually matters. |
| **Slot efficiency** | Recovered per retry slot spent. Directly measures the allocation thesis. |
| **Stop list** | Count and value deliberately refused, with reason breakdown. |
| **Constraint violations** | Must be exactly zero. Any non-zero is a bug, not a tradeoff. |
| **Calibration** | Brier score + reliability diagram for the probability model. |
| **Diagnosis accuracy** | Confusion matrix vs simulator ground truth. |
| **Failure list** | Cases the system handled badly, and why. |

Run 10 seeds, report mean and variance. A single lucky seed proves nothing and a panel that
has seen a hundred submissions knows it.

**The stop list is your most distinctive artifact.** Every other submission optimises
recovery. Yours reports the money it chose not to chase and can defend each decision. That
is what a payments risk person actually wants to see.

---

## 5. Repo structure

```
mandate-recovery/
├── README.md                 # thesis, results table, honest limitations
├── ARCHITECTURE.md           # the diagram + component contracts
├── COMPLIANCE.md             # every constraint + primary source citation
├── sim/                      # world simulator, seeded
├── ingest/                   # webhook consumer + normaliser
├── diagnose/                 # rules floor + LLM adjudicator
├── predict/                  # probability model + calibration
├── constraints/              # pure functions, property-tested
├── policy/                   # allocator + baselines
├── act/                      # idempotency, WAL, ceilings, kill switch
├── eval/                     # harness, metrics, plots
├── audit/                    # append-only log + replay tool
├── tests/                    # constraint properties run in CI
└── demo/                     # one-command reproduction
```

`make demo` must reproduce your headline table from a clean clone. If a reviewer cannot
reproduce your number in one command, your number does not exist.

---

## 6. Fourteen days

| Days | Build | Gate |
|---|---|---|
| **1–2** | Simulator + canonical schema + eval harness + no-retry baseline | You can already print a results table |
| **3–4** | Constraint layer + property tests in CI | Tests prove no illegal sequence is reachable |
| **5–6** | Action layer: idempotency, WAL, ceilings, audit, kill switch | Crash-restart demo works |
| **7** | Razorpay test-mode webhook ingestion, signature verification | Real `payment.failed` flows through your pipeline |
| **8–9** | Diagnosis layer, rules + LLM, disagreement logging | Confusion matrix vs ground truth |
| **10** | Probability model + calibration | Reliability diagram |
| **11–12** | Policy engine + all three baselines, tune | Beat fixed-schedule on net value across 10 seeds |
| **13** | **Code freeze.** README, ARCHITECTURE, COMPLIANCE, results | Clean clone → `make demo` works |
| **14** | Pitch video, architecture diagram, buffer | Submitted |

**Day 13 is a freeze, not a suggestion.** A polished repo with a narrow scope beats a broad
one with a bad README every time. If you are behind on day 11, cut the LLM adjudicator and
ship rules-only diagnosis — the allocation thesis survives intact.

---

## 7. Five-minute pitch structure

| Time | Content |
|---|---|
| 0:00–0:40 | **The constraint.** 3 retries, non-peak only, 24h notice lock. State that Stripe's playbook is illegal here. |
| 0:40–1:20 | **The reframe.** Not a timing model — a budget allocator under regulatory constraints. |
| 1:20–2:30 | **Live demo.** Batch runs. Show decisions with reason strings scrolling. |
| 2:30–3:10 | **The crash demo.** `kill -9`, restart, zero double-charges, audit log reconciles. |
| 3:10–4:10 | **The numbers.** Net value vs three baselines, 10 seeds, with variance. Then the stop list. |
| 4:10–4:40 | **What it gets wrong.** Name two real weaknesses. |
| 4:40–5:00 | **What I'd build next** with production data. |

Spending 30 seconds on your own weaknesses is counterintuitive and it works. It is the
clearest available signal that your other numbers are honest.

---

## 8. Research reading list

**Regulatory — read these first, they are the spine**

- RBI, *Digital Payments – E-mandate Framework, 2026* (21 April 2026) — get the primary
  circular from rbi.org.in, not a summary blog. Consolidates and repeals eight prior
  circulars.
- NPCI circulars on UPI Autopay execution (Aug 2025) — retry caps, non-peak windows, TPS
  moderation. npci.org.in.
- Razorpay's own guide: https://razorpay.com/blog/master-recurring-payments-upi-autopay-guide/
  — useful because it is how your evaluators think about the problem.

**Razorpay integration**

- Test Subscriptions: https://razorpay.com/docs/payments/subscriptions/test/
- Webhooks overview: https://razorpay.com/docs/webhooks/
- Webhook FAQs: https://razorpay.com/docs/webhooks/faqs/

**Prior art — know it so you can say why yours differs**

- Stripe, *How we built it: Smart Retries*: https://stripe.com/blog/how-we-built-it-smart-retries
- Recurly Intelligent Retries: https://docs.recurly.com/recurly-subscriptions/docs/retry-logic
- Gr4vy on retry logic (2026): https://gr4vy.com/posts/payment-retry-logic-explained-smart-retries-for-failed-transactions-in-2026/
- US Patent 11,915,247 & 11,587,093, *Optimized dunning using machine-learned model* —
  patents are unusually detailed on system design and free to read.

**Indian market context**

- UPI Autopay design guide: https://productgrowth.in/insights/fintech/upi-autopay-guide/
  (source for the 8–15% vs 2–3% failure rate gap)
- Business Standard on ~20 million monthly UPI Autopay revocations driven by low balances —
  good framing for why this problem is worth ₹ at scale.

**Technical grounding**

- Constrained MDPs / budgeted sequential decision-making — the correct formal frame for
  your allocator. Altman's *Constrained Markov Decision Processes* is the canonical text;
  you only need chapters 1–3.
- Probability calibration: Niculescu-Mizil & Caruana, *Predicting Good Probabilities with
  Supervised Learning* (ICML 2005). Short, directly applicable.
- Hypothesis (Python property-based testing): https://hypothesis.readthedocs.io

**Caveat on sources:** several of the recovery-uplift numbers circulating online come from
vendor marketing (Slicker, PaymentCollect, and similar). Treat them as directional, cite
them as vendor claims if you use them, and never present them as peer-reviewed. Your own
simulator numbers are the only ones you should defend hard.

---

## 9. Ways this dies

- **Scope creep into checkout abandonment and B2B receivables.** The brief lists them.
  Resist. One loss class, closed completely.
- **A simulator that flatters you.** If your agent recovers 80%, your world is fake.
  Real-world involuntary recovery lands far lower. Tune until the numbers are uncomfortable.
- **Bolting compliance on at the end.** It has to be the constraint layer from day 3, or
  the audit trail will be a log file you wrote on day 13 and it will show.
- **LLM in the money path.** The LLM classifies and explains. It never decides to move
  money. Say this explicitly in the pitch.
- **Unverified regulatory numbers.** Every figure in section 2 must be traced to a primary
  circular *by you*. Getting a real constraint wrong in front of a Razorpay panel is fatal;
  getting it right is a large credibility win.
- **Missing the deadline because you were still building on day 14.** Freeze on 13.
