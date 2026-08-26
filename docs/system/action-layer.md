# The money path

::: tip Status
Built, tested and running in CI — including a real `kill -9` against a live batch. `make demo`.
:::

Everything upstream of this point can be wrong and the damage is a bad decision. Everything
here can be wrong and the damage is a double debit against a real customer. That asymmetry is
why this layer was built on day 2, before any policy existed to call it.

<div class="stat-grid">
  <div class="stat ok"><span class="v">0</span><span class="k">duplicate effects after kill</span></div>
  <div class="stat ok"><span class="v">0</span><span class="k">notifications cancelled</span></div>
  <div class="stat"><span class="v">5 / 5</span><span class="k">mandates, one effect each</span></div>
  <div class="stat"><span class="v">4</span><span class="k">modules</span></div>
</div>

## Controls

<div class="table-scroll">

| Control | Guarantee | Where |
| --- | --- | --- |
| **Write-ahead log** | Intent is `fsync`ed before any side effect | `journal.py` |
| **Hash chain** | The log is tamper-evident and verified on every read | `journal.py` |
| **Torn-tail recovery** | A process killed mid-write still starts | `journal.py` |
| **Idempotency keys** | A replayed decision can never double-debit | `executor.py` |
| **Interrogating recovery** | The in-doubt window is resolved by asking, never by retrying | `executor.py` |
| **Blast-radius cap** | Hard limit on executions and rupees per run | `executor.py` |
| **Kill switch** | An operator halts the agent without a deploy | `executor.py` |
| **Constraint re-check** | An action the rules forbid is refused at the boundary | `executor.py` |
| **Dry run by default** | Running the system by accident cannot move money | `executor.py` |
| **Replay** | Any run reconstructible from the journal alone | `replay.py` |

</div>

## The journal

One append-only file, one JSON object per line — greppable and diffable by anyone reviewing an
incident without a special tool. Each record carries the hash of its predecessor.

```json
{"seq":7,"kind":"INTENT","ts":"2026-09-01T00:00:00+05:30","run_id":"night-batch",
 "body":{"idem_key":"c7fe76d8…","op":"raise_pdn","mandate_id":"MND_0002",
         "execute_at":"2026-09-02T00:00:00+05:30","amount_paise":69900,"mode":"LIVE"},
 "prev_hash":"a41b…","hash":"9c02…"}
```

Every append flushes and `fsync`s before returning. That is the entire promise of a
write-ahead log: an append that returns before the bytes are durable is a promise the log
cannot keep, and the failure it permits is an effect that outlives its own record.

### A torn tail is not corruption

These two look similar in a hex dump and could not be more different in meaning:

<div class="table-scroll">

| Situation | What it means | Response |
| --- | --- | --- |
| Final line has no terminating newline | A process was killed mid-write. Normal operation for a WAL. | Truncate to the last valid record, and `fsync` the truncation so a second crash cannot resurrect it |
| A record fails verification anywhere else | The file was edited | Refuse to load |

</div>

The distinction is what makes automatic recovery safe: **a torn write cannot end in a
newline**, so anything that does was appended deliberately. Refusing on tamper is deliberate
too — a journal that has been edited cannot prove what happened, and silently repairing it
would destroy the only evidence that it was edited.

```python
def test_a_complete_trailing_line_is_tamper_not_a_torn_write(tmp_path):
    with pytest.raises(TamperError, match="not a torn write"):
        Journal(path).open()
```

Rewriting a record's own hash is not enough to pass, either — it must also chain to the record
before it, which cannot be fixed without rewriting the entire suffix.

## Idempotency

```python
def idempotency_key(mandate_id, cycle_id, attempt_index, action: Commit) -> str:
    return sha256(f"pdn|{mandate_id}|{cycle_id}|{attempt_index}"
                  f"|{action.execute_at.isoformat()}|{action.amount_paise}")
```

The identity of a **decision**, not of an attempt to perform it. It deliberately excludes the
run id and the wall clock, so a restarted run reaches the same key and the second attempt is a
no-op — which is exactly what has to happen after a crash.

The amount is in the key because the amount is a decision variable
([the amount lever](/system/allocator#the-amount-lever)); committing ₹299 and committing ₹499
are different decisions and must not collide.

::: warning A declined debit still counts as applied
C1 counts presentations, not successes. An attempt that was declined for insufficient funds
consumed the slot, so the ledger records it as applied and it is never silently repeated.
Only an effect the gateway *never performed* leaves the decision free to be taken again.
:::

## The in-doubt window

This is the failure the whole layer is designed around, and it is specific rather than generic.

[C6](/constraints/) makes the pre-debit notification a prerequisite for presenting a debit.
[C8](/constraints/critical#c8-commitments-are-serialized) makes a *second* notification cancel
the first. Put together, a crash between raising the notification and recording that you raised
it leaves an effect the log cannot account for — and the obvious recovery strategy is
catastrophic.

<div class="diagram">
<svg viewBox="0 0 720 300" role="img" aria-label="The in-doubt window between raising a notification and recording it, and the two possible recovery strategies: retrying, which cancels the pending notification, versus asking the gateway, which adopts it">
  <text x="24" y="24" font-size="12" font-weight="600">Crash inside the in-doubt window</text>

  <rect class="box" x="24"  y="44" width="132" height="40" rx="6"/>
  <text x="40" y="62" font-size="10.5" font-weight="600">WAL: INTENT</text>
  <text class="dim" x="40" y="76" font-size="9">fsynced</text>

  <rect class="box-accent" x="176" y="44" width="132" height="40" rx="6"/>
  <text x="192" y="62" font-size="10.5" font-weight="600">raise PDN</text>
  <text class="dim" x="192" y="76" font-size="9">effect lands</text>

  <rect class="box" x="392" y="44" width="132" height="40" rx="6"/>
  <text x="408" y="62" font-size="10.5" font-weight="600">WAL: EFFECT</text>
  <text class="dim" x="408" y="76" font-size="9">never reached</text>

  <path class="line-accent" d="M 156 64 L 176 64"/>
  <path class="line" d="M 308 64 L 392 64" stroke-dasharray="4 3"/>

  <line class="line-accent" x1="350" y1="34" x2="350" y2="96" stroke-width="2.5"/>
  <text x="330" y="112" font-size="10" font-weight="600" fill="#b91c1c">kill -9</text>

  <line class="line" x1="24" y1="132" x2="696" y2="132" stroke-dasharray="3 3"/>
  <text class="dim" x="24" y="152" font-size="10.5">The gateway did work the journal cannot account for. Two ways out:</text>

  <rect class="box" x="24" y="170" width="320" height="104" rx="8"/>
  <text x="42" y="192" font-size="11" font-weight="600" fill="#b91c1c">retry anything unfinished</text>
  <text class="dim" x="42" y="212" font-size="9.5">raises a second notification</text>
  <text class="dim" x="42" y="228" font-size="9.5">→ C8 cancels the first</text>
  <text class="dim" x="42" y="244" font-size="9.5">→ execution slips by a day</text>
  <text class="dim" x="42" y="262" font-size="9.5">→ looks like a network blip in the logs</text>

  <rect class="box-accent" x="376" y="170" width="320" height="104" rx="8"/>
  <text x="394" y="192" font-size="11" font-weight="600">ask the gateway</text>
  <text class="dim" x="394" y="212" font-size="9.5">lookup(idem_key)</text>
  <text class="dim" x="394" y="228" font-size="9.5">→ found: adopt it, write the outcome late</text>
  <text class="dim" x="394" y="244" font-size="9.5">→ not found: close it, never performed</text>
  <text class="dim" x="394" y="262" font-size="9.5">→ no path that assumes</text>
</svg>
</div>

The left-hand branch is a compliance-shaped bug wearing the costume of a transient error.
Nothing crashes, nothing alerts, and a day of the aperture is gone.

### Which is why the gateway interface looks like this

```python
class PaymentGateway(Protocol):
    def raise_pdn(self, idem_key, mandate_id, execute_at, amount_paise) -> GatewayResult: ...
    def present(self, idem_key, mandate_id, sequence_id, amount_paise) -> GatewayResult: ...
    def lookup(self, idem_key) -> GatewayResult | None: ...
```

`lookup` is not a convenience. A gateway that can only be told to do things, and never asked
what it has already done, **cannot be recovered from safely** — after a crash the caller has
exactly two options, ask or guess, and guessing here means either a double debit or a
cancelled notification.

### A crash writes no outcome record

When the effect call raises, the executor deliberately writes nothing:

```python
try:
    result = self.gateway.raise_pdn(key, ctx.mandate_id, action.execute_at, action.amount_paise)
except Exception:
    # Deliberately not writing an EFFECT record. The effect may well have landed,
    # and claiming otherwise would be a lie the log cannot take back.
    raise
```

Recording "this failed" would be a guess written down as a fact. Silence is the honest state,
and it is the state `recover()` knows how to resolve.

## The ledger is derived, not stored

Applied keys, in-doubt intents and blast-radius counters are all rebuilt by replaying the
journal. There is no second store to fall out of sync with the first.

It also means ceilings survive a restart within the same run — otherwise a crash loop would
spend the full blast radius once per restart.

## Blast radius and kill switch

```python
BlastRadius(max_executions=500, max_paise_attempted=rupees(5_00_000))
```

Breaching either raises `CeilingExceeded` and stops the run. Deliberately an exception rather
than a return value: **a ceiling a caller can ignore is a warning.** The check happens before
the effect, which is asserted directly rather than assumed.

The kill switch is file-backed, so halting a misbehaving agent needs neither a deploy nor a
restart nor access to the code. It is checked before every effect, so engaging it stops new
work while anything in flight completes.

## Constraint layer, re-consulted at the boundary

```python
verdict = is_permitted(action, state, clock)
if not verdict.allowed:
    self._skip(run_id, key, "VETOED", f"[{verdict.rule_id}] {verdict.reason}")
    return Outcome("VETOED", key, ...)
```

The policy is supposed to have checked already. This is defence in depth, and it means a bug
in the allocator cannot become an illegal debit — the veto is journalled with the rule that
fired, so a refused action leaves as much evidence as an executed one.

## The demonstration

```
$ make demo

1 · START THE BATCH, THEN KILL IT MID-FLIGHT
  worker pid 6592 killed uncatchably (exit 1)
  journal: 2 effect(s) recorded, 1 in doubt
  gateway: 3 notification(s) actually raised
  the gap is the point — the gateway did work the journal cannot account for

2 · RESTART AND RECONCILE
  intents in doubt      1
  adopted from gateway  1
  never performed       0
  resolved by asking the gateway, never by retrying

3 · RE-RUN THE IDENTICAL BATCH
  MND_0000  DUPLICATE already applied as pdn-000001
  MND_0001  DUPLICATE already applied as pdn-000002
  MND_0002  DUPLICATE already applied as pdn-000003
  MND_0003  APPLIED   ok
  MND_0004  APPLIED   ok

4 · VERDICT
  [PASS]  hash chain verifies                      21 records
  [PASS]  one effect per mandate, no more          5 of 5
  [PASS]  notifications raised in total            5, expected 5
  [PASS]  re-run raised only the outstanding ones  2, expected 2
  [PASS]  already-done work was skipped            3 duplicates, expected 3
  [PASS]  no mandate was committed twice           max intents for one mandate: 1
  [PASS]  nothing left in doubt                    0
  [PASS]  no notification cancelled (C8)           []
```

Not a simulation. The orchestrator spawns a worker process, waits until it is parked at the
exact instant where the notification has landed at the gateway but its outcome has not been
written, and terminates it uncatchably — no cleanup, no flush, no "sorry, I died" record,
because in production it would not get one. A fresh process then reconciles against the same
journal and the same gateway records.

It runs in CI on every push. A demonstration that only works on one laptop is a liability, and
one that quietly stops working gets discovered live.

## Replay

```bash
$ make replay

chain verified: 21 records, no breaks

run night-batch   mode=LIVE
  decisions          5
  intents            5
  effects performed  5  (5 ok)
  value attempted    ₹3,495.00
  skipped/DUPLICATE  3
```

Reading verifies the chain, so a replay that prints anything at all has already proved the log
was not edited.

Each `DECISION` record carries a receipt: the mandate and attempt, the justification string,
policy and model versions, and — once the allocator exists — the belief vector, the bid, the
clearing price and every constraint verdict. Fields the allocator will populate default to
`None` rather than to invented values, so a receipt never implies a computation that did not
happen.

## Two bugs worth recording

**A check that could not fail.** The demo's verdict table originally contained
`new_raises == len(statuses) - duplicates - 0 or True` — which passes unconditionally. A green
row asserting nothing is worse than no row, because it buys false confidence. Replaced with
eight checks that each assert something falsifiable, including "no mandate was committed
twice", computed from the journal rather than from the demo's own bookkeeping.

**An audit trail that lied about itself.** Recovery originally filed resolved outcomes under a
synthetic `recovery` run id. A run-scoped replay then filtered that record out and reported
the intent as permanently in doubt — the audit trail claiming the system had lost track of an
effect it had in fact reconciled. On a payments system that is how a clean incident becomes a
two-day investigation.

Outcomes are now filed against the run that intended them, carrying the original amount so the
blast-radius counters do not under-count exactly the spend a crash made hardest to see.

It is worth being precise about how that one surfaced: **it passed every test that existed.**
It was found by running `replay` and reading the output. There is now a regression test with
the story in its docstring.

**Next:** [Verification](/engineering/verification).
