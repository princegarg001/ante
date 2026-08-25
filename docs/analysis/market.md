# Market data

The figures the simulator is calibrated against, each with its source and how far it can be
pushed. Simulator credibility rests entirely on this page, so the caveats are as important as
the numbers.

## Headline figures

<div class="table-scroll">

| Figure | Value | Source | How to use it |
| --- | --- | --- | --- |
| UPI Autopay mandate revocations | ~20 million / month, driven by low balances | Business Standard, Sept 2025 | Usable as reported, cited as press |
| New mandate registrations | ~50 M in July 2025, versus ~26 M in July 2024 | Same | Usable |
| Mandate executions | ~808 M in July 2025 | Same | Usable |
| Approval rate, largest remitter bank | ~30% of auto-debits approved | Same | **Verify** before quoting |
| Business decline rate, top-50 banks | ~74% | Same | **Do not quote — see below** |
| UPI Autopay technical failure rate | 8–15% | productgrowth.in | Directional, cite as an industry blog |
| Card mandate failure rate | 2–3% | Same | Directional |

</div>

## The number not to put on a slide

The ~74% business decline figure is ambiguous in the reporting between two readings:

1. 74% of *all* auto-debit attempts are business-declined, or
2. 74% *of declines* are business declines rather than technical failures.

Those are very different claims, and a Razorpay panel will know which one is correct. Until the
primary figure is available, the safe formulation is:

> "Industry reporting puts auto-debit approval rates at the largest remitter bank around 30%,
> with the bulk of failures being business declines — insufficient funds — rather than
> technical failures."

That says what matters without asserting a denominator that cannot be defended.

## Two failure rates that are constantly conflated

<div class="table-scroll">

| | Technical failure rate | Business decline rate |
| --- | --- | --- |
| Measures | Timeouts, downtime, routing errors | Bank declined for insufficient funds or policy |
| Reported value | 8–15% | Dominant share of failures |
| Recoverable by retrying soon? | Often, yes | Only if the balance changes |
| Right response | Retry quickly | Retry when money is likely to have arrived, or lower the amount |

</div>

A system calibrated against the 8–15% figure alone would be badly wrong, because it would treat
the dominant failure mode as if it were transient. The distinction is why
`TRANSIENT_ISSUER` and `INSUFFICIENT_FUNDS` are separate cause classes with entirely different
downstream handling.

## What the numbers imply for the simulator

The market data drives three design requirements.

**Failure is the normal case.** Approval near 30% at the largest remitter bank means a
simulator with a 90% base success rate is not modelling this market. Base rates are tuned so
recovery is uncomfortable.

**Insufficient funds must emerge, not be injected.** Because failure is dominated by balance
timing, the simulator models income as a marked point process — salary on the 1st or the 7th,
irregular arrivals for gig income — with compound-Poisson spend between arrivals. A debit
succeeds when `balance(t) ≥ amount`. Month-end clustering then falls out of the process
instead of being hand-coded, and both the timing lever and the
[amount lever](/system/allocator#the-amount-lever) become meaningful for the right reason.

**Revocation must be a live hazard.** Twenty million monthly revocations is the market telling
you that aggressive recovery destroys mandates. The simulator raises revocation hazard with
repeated failed debits and with excessive contact, so a policy that over-collects is punished
in the metric that matters — mandates surviving at cycle end.

## Calibration targets

<div class="table-scroll">

| Quantity | Target | Reason |
| --- | --- | --- |
| First-attempt approval on due date | 30–40% | Matches reported approval rates |
| Total collection after recovery | below ~45% | Real involuntary recovery lands far lower than intuition suggests |
| Genuinely unrecoverable share | 12–18% | Closed accounts, revoked and expired mandates, real churn intent |
| Revocation rate under an aggressive policy | material | Otherwise the option-value term never bites |

</div>

::: warning The failure mode to watch for
A simulator that flatters the agent is the most likely way this project produces a meaningless
result. If recovery comes out above roughly 45%, the world is wrong — not the policy. The
correct response is to make the simulator harsher, not to celebrate.
:::

## Error taxonomy

Cause classes are grounded in the codes the rails actually emit, drawn from Razorpay's UPI
error documentation and NPCI response codes.

<div class="table-scroll">

| Class | Representative codes | Retry-recoverable |
| --- | --- | --- |
| `TRANSIENT_ISSUER` | `bank_technical_error`, `gateway_technical_error`, U-series | Yes, quickly |
| `INSUFFICIENT_FUNDS` | `insufficient_funds`, Z9 | Yes — timing and amount are everything |
| `LIMIT_BREACH` | per-transaction or daily cap | Yes — lower amount or a different slot |
| `PDN_MISSING` | `PRE_DEBIT_NOTIFICATION_NOT_FOUND` / `_NOT_SENT` (HTTP 422) | Yes — **and it is your own bug** |
| `MANDATE_EXPIRED` | validity lapsed | No — needs re-registration |
| `MANDATE_REVOKED` | customer cancelled | No — retrying is abusive |
| `AFA_REQUIRED` | above ceiling | No — needs customer action |
| `VPA_INVALID` | `invalid_vpa`, `vpa_resolution_failed` | No — needs customer action |
| `TERMINAL` | account closed or frozen | No — stop permanently |

</div>

`PDN_MISSING` has its own class precisely because it is self-inflicted. A system that detects
and reports its *own* compliance failures is a better artifact than one that cannot see them.

## Sources

- Business Standard — *UPI autopay revocations hit 20 mn per month on low customer balance*, September 2025
- productgrowth.in — UPI AutoPay design guide
- Razorpay — *Master Recurring Payments with UPI 2.0 Autopay: 2026 Guide*
- Razorpay Docs — UPI error codes
- NPCI — UPI error and response codes

::: tip On vendor numbers
Several recovery-uplift figures circulating online originate in vendor marketing. They are
treated as directional and cited as vendor claims where used. The only numbers defended hard
in this project are the ones its own simulator produces, under a seed anyone can re-run.
:::
