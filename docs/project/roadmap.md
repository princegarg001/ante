# Status & roadmap

Built in the open against a 5 September 2026 deadline. This page says what runs, not what is
planned — the distinction is the point.

## Current state

<div class="stat-grid">
  <div class="stat ok"><span class="v">146</span><span class="k">tests passing</span></div>
  <div class="stat ok"><span class="v">2.5 M</span><span class="k">triples verified</span></div>
  <div class="stat ok"><span class="v">11 / 11</span><span class="k">mutants killed</span></div>
  <div class="stat"><span class="v">2 / 12</span><span class="k">days elapsed</span></div>
</div>

<div class="table-scroll">

| Component | State |
| --- | --- |
| `core/` — IST clock, non-peak slot grid, domain types, paise money | ✅ **done** |
| `constraints/` — C1–C24 as pure functions, every veto citing its rule | ✅ **done** |
| `constraints/modelcheck.py` — exhaustive verification | ✅ **done** |
| `tests/mutation.py` — mutation testing of the compliance suite | ✅ **done** |
| `tests/test_purity.py` — AST guards on the decision path | ✅ **done** |
| CI compliance gate on every push | ✅ **done** |
| `act/` — WAL, idempotency, ceilings, kill switch, hash-chained receipts | ✅ **done** |
| `demo/crash_demo.py` — real `kill -9`, zero double-debits | ✅ **done** |
| `sim/` — latent balance process, issuer downtime, churn | not started |
| `eval/` — harness, baselines, oracle bound | not started |
| `belief/`, `predict/`, `policy/` — the allocator | not started |
| `ingest/` — Razorpay test-mode webhooks | not started |
| `diagnose/` — rules ratchet + LLM adjudicator | not started |

</div>

## Twelve days

The original plan assumed fourteen. Research began on 24 August, leaving twelve. Freeze is
day 10, not day 13.

<div class="table-scroll">

| Day | Date | Build | Gate |
| --- | --- | --- | --- |
| 1 | 25 Aug | Constraint layer C1–C24, property tests, **exhaustive model checker** | ✅ CI prints states enumerated, 0 violations |
| 2 | 26 Aug | Action layer: WAL, idempotency, ceilings, kill switch, hash-chained receipts | ✅ real `kill -9` → restart → zero duplicates, in CI |
| 3 | 27 Aug | Simulator: latent balance process, issuer downtime, churn, stop-list | Base rates match the calibration targets |
| 4 | 28 Aug | Eval harness, B0/B1 baselines, common random numbers, oracle policy | Results table prints with confidence intervals |
| 5 | 29 Aug | Belief filter, P(success) model, isotonic calibration | Reliability diagram and EV(a) curve |
| 6 | 30 Aug | **Policy: per-mandate DP** | Beats B1 on a single seed |
| 7 | 31 Aug | **Dual ascent and slot auction**, B2/B3 baselines | Auction book prints; B3 violation count published |
| 8 | 1 Sept | Diagnosis: rules ratchet, LLM adjudicator, disagreement log | Confusion matrix |
| 9 | 2 Sept | Razorpay test-mode webhooks, signature verification, red-team suite | Real `payment.failed` → decision |
| 10 | 3 Sept | **Code freeze.** Conformal stop bound, docs, 10-seed run | Clean clone → `make demo` |
| 11 | 4 Sept | Pitch video, architecture diagram | Recorded |
| 12 | 5 Sept | Buffer, submit | Submitted |

</div>

### Why the constraint layer came first

Data flows from the simulator through diagnosis and prediction into the policy. The build order
runs almost backwards, and deliberately:

- A policy built before its guardrails is a policy whose guardrails were fitted to it.
- A compliance layer written on the final day is a log file, and it reads like one. The commit
  history is part of the evidence.
- `is_permitted` is a pure function of its arguments and depends on nothing, so it can be
  exhaustively verified before a single probability model exists.

## If the schedule slips

Cut from the bottom, never from the top.

<div class="table-scroll">

| Order | Cut | Cost |
| --- | --- | --- |
| 1 | LLM adjudicator → rules-only diagnosis | Thesis survives intact |
| 2 | Notification-copy bandit → fixed best template | Minor |
| 3 | Regulatory diff agent | Already optional |
| 4 | B3 Stripe baseline | Keep if at all possible — it is the best slide |
| 5 | Conformal stop bound | Keep if at all possible — it is the most distinctive claim |

</div>

**Never cut:** the constraint layer, the model checker, the action layer, simulator honesty,
the oracle bound.

## Open risks

<div class="table-scroll">

| Risk | Mitigation |
| --- | --- |
| **Every regulatory row is still `SECONDARY`** | [Verification checklist](/constraints/sources); three highest-risk rows identified and prioritised |
| C9 may be narrower than documented | Option-value term is parameterised so the design degrades rather than collapses |
| A simulator that flatters the agent | Calibration targets fixed in advance; above ~45% recovery is treated as a bug in the world |
| Uplift lost in variance | Common random numbers and paired per-seed comparison |
| Grading a model on features it was generated from | Agent belief space given a different structure from the simulator's parameters |

</div>

## Repository

[github.com/princegarg001/ante](https://github.com/princegarg001/ante)

```bash
make install   # pip install -e ".[dev]"
make test      # unit, property, stateful, purity — 30s
make verify    # exhaustive constraint verification — 15s
make mutants   # mutation testing — 6 min
make check     # the full compliance gate
```
