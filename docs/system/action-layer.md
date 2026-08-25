# The money path

::: tip Status
Built and running in CI, including the crash demonstration. `make demo`.
:::

Everything upstream of this point can be wrong and the damage is a bad decision. Everything
here can be wrong and the damage is a double debit against a real customer.

## Requirements

<div class="table-scroll">

| Control | Guarantee |
| --- | --- |
| **Idempotency keys** | A replayed decision can never double-debit |
| **Write-ahead log** | Intent is durable before any side effect |
| **Two-phase commit against the notification** | A crash between notification and presentation cannot leak a second notification |
| **Blast-radius cap** | Hard limit on executions and rupees attempted per run |
| **Per-customer contact cap** | Independent of the retry cap |
| **Kill switch** | One flag halts pending actions; in-flight actions drain safely |
| **Hash-chained receipts** | Every decision reconstructible and tamper-evident |
| **Dry run by default** | Real execution requires an explicit flag |

</div>

## Idempotency

```python
key = sha256(f"{mandate_id}|{cycle}|{attempt_index}|{scheduled_ts}")
```

Deterministic in the decision, not in the moment of execution. Replaying the same decision
produces the same key and the same no-op.

## The commit sequence

[C6](/constraints/) makes the notification a prerequisite for execution, and
[C8](/constraints/critical#c8-commitments-are-serialized) makes a second notification destroy
the first. That combination means the ordering is not negotiable:

<div class="diagram">
<svg viewBox="0 0 720 200" role="img" aria-label="Commit sequence: write intent to the log, raise the notification, record the sequence id, mark committed, then present; each stage annotated with what a crash there implies">
  <rect class="box" x="20"  y="46" width="118" height="44" rx="6"/>
  <text x="34" y="66" font-size="10.5" font-weight="600">WAL: intent</text>
  <text class="dim" x="34" y="81" font-size="9">fsync before effect</text>

  <rect class="box" x="158" y="46" width="118" height="44" rx="6"/>
  <text x="172" y="66" font-size="10.5" font-weight="600">raise PDN</text>
  <text class="dim" x="172" y="81" font-size="9">the irrevocable step</text>

  <rect class="box" x="296" y="46" width="126" height="44" rx="6"/>
  <text x="310" y="66" font-size="10.5" font-weight="600">record seq id</text>
  <text class="dim" x="310" y="81" font-size="9">presentations_sequence_id</text>

  <rect class="box" x="442" y="46" width="118" height="44" rx="6"/>
  <text x="456" y="66" font-size="10.5" font-weight="600">WAL: committed</text>

  <rect class="box-accent" x="580" y="46" width="118" height="44" rx="6"/>
  <text x="600" y="72" font-size="10.5" font-weight="600">present</text>

  <path class="line-accent" d="M 138 68 L 158 68"/>
  <path class="line-accent" d="M 276 68 L 296 68"/>
  <path class="line-accent" d="M 422 68 L 442 68"/>
  <path class="line-accent" d="M 560 68 L 580 68"/>

  <line class="line" x1="20" y1="112" x2="698" y2="112" stroke-dasharray="3 3"/>
  <text class="dim" x="20" y="136" font-size="10">Crash before the PDN → replay is safe, nothing happened.</text>
  <text class="dim" x="20" y="154" font-size="10">Crash after the PDN, before the WAL commit → recovery must adopt the existing notification,</text>
  <text class="dim" x="20" y="170" font-size="10">never raise a second one, because a second notification cancels the first (C8).</text>
  <text class="dim" x="20" y="190" font-size="10">Crash after commit → the presentation is idempotent on its key.</text>
</svg>
</div>

The middle failure is the interesting one. Naive retry-on-startup logic would raise a fresh
notification, which under C8 cancels the pending one and silently pushes the execution out by
another day — a compliance-shaped bug that looks like a transient network error in the logs.

## Decision receipts

Every decision produces an append-only, hash-chained record:

<div class="table-scroll">

| Field | Why |
| --- | --- |
| `prev_hash` | Tamper evidence |
| Input digest | Which state produced the decision |
| Model and policy versions | Which code produced it |
| Belief vector | What the agent thought it knew |
| `λ_w` price and the bid | Why it won or lost the slot |
| Every constraint verdict | Which rules were consulted, not merely which fired |
| Chosen action and expected value | The decision itself |
| Human-readable justification | The sentence a reviewer reads |

</div>

`--replay <run_id>` reconstructs the run bit-identically. That is only possible because the
[constraint layer is pure](/engineering/decisions#nothing-reads-the-wall-clock) and every
domain type is frozen — properties that are enforced by AST guards rather than left to
discipline.

## The demonstration

```
$ make demo

1 · START THE BATCH, THEN KILL IT MID-FLIGHT
  worker pid 6592 killed uncatchably (exit 1)
  journal: 2 effect(s) recorded, 1 in doubt
  gateway: 3 notification(s) actually raised

2 · RESTART AND RECONCILE
  intents in doubt      1
  adopted from gateway  1
  resolved by asking the gateway, never by retrying

4 · VERDICT
  [PASS]  one effect per mandate, no more          5 of 5
  [PASS]  no mandate was committed twice           max intents for one mandate: 1
  [PASS]  nothing left in doubt                    0
  [PASS]  no notification cancelled (C8)           []
```

Not a simulation: a real subprocess, parked at exactly that instant, terminated
uncatchably. It runs in CI, because a demonstration that only works on one laptop is a
liability and one that quietly stops working gets discovered live.

Thirty seconds, and it settles the question of whether the system can be trusted with money —
which is the question a payments panel is actually asking.

**Next:** [Verification](/engineering/verification).
