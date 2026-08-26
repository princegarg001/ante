# Architecture

Nine components, each independently runnable and independently demonstrable. The build order
is deliberate and it is not the order data flows in.

## The pipeline

<div class="diagram">
<svg viewBox="0 0 760 470" role="img" aria-label="System architecture: simulator and webhooks feed ingestion, diagnosis, belief filter and prediction; the policy proposes actions which the constraint layer vetoes or permits before the action layer executes them">
  <!-- sources -->
  <rect class="box" x="24" y="20" width="150" height="52" rx="6"/>
  <text x="40" y="42" font-size="11" font-weight="600">0 · SIMULATOR</text>
  <text class="dim" x="40" y="58" font-size="9.5">latent balance · downtime</text>

  <rect class="box" x="196" y="20" width="150" height="52" rx="6"/>
  <text x="212" y="42" font-size="11" font-weight="600">RAZORPAY TEST</text>
  <text class="dim" x="212" y="58" font-size="9.5">payment.failed webhooks</text>

  <path class="line" d="M 99 72 L 99 92 L 185 92" />
  <path class="line" d="M 271 72 L 271 92 L 185 92" />
  <path class="line-accent" d="M 185 92 L 185 108" />

  <!-- pipeline -->
  <rect class="box" x="110" y="108" width="150" height="44" rx="6"/>
  <text x="126" y="128" font-size="11" font-weight="600">1 · INGEST</text>
  <text class="dim" x="126" y="143" font-size="9.5">verify sig → FailureEvent</text>
  <path class="line-accent" d="M 185 152 L 185 172"/>

  <rect class="box" x="110" y="172" width="150" height="44" rx="6"/>
  <text x="126" y="192" font-size="11" font-weight="600">2 · DIAGNOSE</text>
  <text class="dim" x="126" y="207" font-size="9.5">rules ratchet + LLM</text>
  <path class="line-accent" d="M 185 216 L 185 236"/>

  <rect class="box" x="110" y="236" width="150" height="44" rx="6"/>
  <text x="126" y="256" font-size="11" font-weight="600">3 · BELIEF</text>
  <text class="dim" x="126" y="271" font-size="9.5">posterior over liquidity</text>
  <path class="line-accent" d="M 185 280 L 185 300"/>

  <rect class="box" x="110" y="300" width="150" height="44" rx="6"/>
  <text x="126" y="320" font-size="11" font-weight="600">4 · PREDICT</text>
  <text class="dim" x="126" y="335" font-size="9.5">P(success | t, a) calibrated</text>
  <path class="line-accent" d="M 260 322 L 330 322"/>

  <!-- policy + constraints -->
  <rect class="box" x="330" y="288" width="170" height="68" rx="6"/>
  <text x="346" y="310" font-size="11" font-weight="600">6 · POLICY</text>
  <text class="dim" x="346" y="326" font-size="9.5">per-mandate DP</text>
  <text class="dim" x="346" y="340" font-size="9.5">dual prices → slot auction</text>

  <rect class="box-accent" x="536" y="288" width="170" height="68" rx="6"/>
  <text x="552" y="310" font-size="11" font-weight="600">5 · CONSTRAINTS</text>
  <text class="dim" x="552" y="326" font-size="9.5">C1–C24, pure functions</text>
  <text class="dim" x="552" y="340" font-size="9.5">exhaustively verified</text>

  <path class="line-accent" d="M 500 308 L 536 308"/>
  <text class="dim" x="502" y="303" font-size="8.5">proposes</text>
  <path class="line-accent" d="M 536 340 L 500 340"/>
  <text class="dim" x="502" y="354" font-size="8.5">vetoes</text>

  <path class="line-accent" d="M 415 356 L 415 384"/>

  <!-- act -->
  <rect class="box" x="290" y="384" width="250" height="46" rx="6"/>
  <text x="306" y="405" font-size="11" font-weight="600">7 · ACT</text>
  <text class="dim" x="306" y="420" font-size="9.5">WAL · idempotency · ceilings · kill switch · receipts</text>

  <path class="line" d="M 415 430 L 415 450 L 620 450 L 620 364"/>
  <rect class="box" x="560" y="384" width="146" height="0" rx="6"/>
  <text class="dim" x="430" y="464" font-size="9.5">outcomes feed back into belief and the auction</text>
</svg>
<p class="cap">The policy proposes; the constraint layer disposes. There is no path from policy to action layer that bypasses the veto.</p>
</div>

## Component contracts

<div class="table-scroll">

| # | Component | Input → Output | Status |
| --- | --- | --- | --- |
| 0 | `sim/` | seed → mandate book + failure events | **built** |
| 1 | `ingest/` | webhook or sim event → `FailureEvent` | planned |
| 2 | `diagnose/` | `FailureEvent` → `(CauseClass, confidence)` | planned |
| 3 | `belief/` | observations → posterior over liquidity type | planned |
| 4 | `predict/` | `(t, amount, belief, issuer)` → calibrated `P(success)` | planned |
| 5 | `constraints/` | `(action, state, clock)` → `Allow \| Veto` | **built** |
| 6 | `policy/` | batch of states + prices → allocation | planned |
| 7 | `act/` | permitted action → durable, idempotent effect | **built** |
| 8 | `eval/` | policy + seeds → metrics vs baselines | planned |

</div>

Current status is tracked in detail on [Status & roadmap](/project/roadmap).

## Build order

Data flows `0 → 1 → 2 → 3 → 4 → 6 → 7`. The build order is:

```
5 → 7 → 0 → 8 → 6 → 3/4 → 2 → 1
```

Constraints and the action layer come first, before any policy exists. Three reasons:

**They are what makes everything else trustworthy.** A policy built before its guardrails is a
policy whose guardrails were fitted to it. Building the constraint layer first means the policy
is developed against a boundary it cannot negotiate with.

**They cannot be faked late.** A compliance layer written on the final day is a log file, and it
reads like one. The commit history is part of the evidence.

**They are the components with no dependencies.** `is_permitted` is a pure function of its
arguments. It can be exhaustively verified before a single probability model exists.

## The layering rule

The system has exactly one privileged direction:

> The policy proposes. The constraint layer disposes. The action layer executes only what the
> constraint layer permitted, and records what it did.

Concretely:

- `constraints/` imports nothing from `policy/`, `predict/` or `belief/`. It is a leaf.
- `policy/` may only reach the action layer through actions that carry a permit.
- The LLM appears in `diagnose/` and in copy generation. It never appears in `act/`.

That last point is worth stating explicitly because it is the question a payments panel will
ask: *the model classifies, writes and explains; it never decides to move money, and the rule
layer can override it but it can never override the rule layer.*

## Repository layout

```
ante/
├── mandate_recovery/
│   ├── core/               # IST clock, paise money, domain types
│   ├── constraints/        # C1–C24 + exhaustive model checker   ✅ built
│   ├── belief/             # discrete Bayesian liquidity filter
│   ├── predict/            # GBDT + isotonic calibration
│   ├── policy/             # per-mandate DP, dual ascent, auction, baselines
│   ├── act/                # WAL, idempotency, ceilings, receipts  ✅ built
│   ├── diagnose/           # rules ratchet + LLM adjudicator
│   ├── ingest/             # webhook consumer + normaliser
│   ├── sim/                # world simulator
│   └── eval/               # harness, metrics, oracle bound
├── tests/                  # unit, property, stateful, mutation, purity
├── docs/                   # this site
└── .github/workflows/      # compliance gate on every push
```

**Next:** [The allocator](/system/allocator).
