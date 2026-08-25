# Constraint register

Every rule the engine enforces, with its source and verification status. This page is the
authority; the code in `mandate_recovery/constraints/rules.py` implements it, and the rule
identifiers below appear verbatim in every veto the system emits.

::: warning Nothing here is law until it says PRIMARY
Rows marked <span class="pill secondary">secondary</span> were established from law-firm
notes, PSP developer documentation or press reporting. They must be re-confirmed against the
NPCI or RBI circular before any of it is quoted as fact. Progress is tracked in
[Verification status](/constraints/sources).
:::

## Two kinds of rule

The register separates **regulatory** constraints from **operational** ones, and the code keeps
them in separate registries.

<div class="table-scroll">

| Kind | Meaning | Consequence of violation |
| --- | --- | --- |
| <span class="rule reg">REGULATORY</span> | Traces to an NPCI or RBI instrument | Illegal |
| <span class="rule ops">OPERATIONAL</span> | Merchant policy, blast-radius control | Expensive |

</div>

The split is not cosmetic. The headline compliance claim counts regulatory rules only, so it
cannot be inflated by counting internal guards. When an action violates several rules at once,
`is_permitted` reports the regulatory one — the veto a regulator would care about, not the one
operations would.

## The register

### Execution budget and windows

<div class="table-scroll">

| ID | Constraint | Value | Source | Status |
| --- | --- | --- | --- | --- |
| <span class="rule reg">C1</span> | Retry cap | 1 execution + 3 retries per mandate per sequence number | NPCI UPI/API Guidelines, notified 21 May 2025, enforced 1 Aug 2025 | <span class="pill secondary">secondary</span> |
| <span class="rule reg">C2</span> | Peak hours — execution barred | 10:00–13:00 and 17:00–21:30 IST | Same | <span class="pill secondary">secondary</span> |
| <span class="rule reg">C3</span> | Non-peak — execution permitted | 00:00–10:00, 13:00–17:00, 21:30–24:00 IST (16.5 h/day) | Derived from C2 | <span class="pill derived">derived</span> |
| <span class="rule reg">C4</span> | Throughput | Initiator PSPs must execute at a "moderated TPS"; rate limits apply | Same | <span class="pill secondary">secondary</span> |

</div>

### Pre-debit notification

<div class="table-scroll">

| ID | Constraint | Value | Source | Status |
| --- | --- | --- | --- | --- |
| <span class="rule reg">C5</span> | Notification aperture | Must be raised within **[T−48h, T−24h]**. NPCI validates the 24 h minimum | PSP developer docs (Decentro, Setu, PayU, Juspay) | <span class="pill secondary">secondary</span> |
| <span class="rule reg">C6</span> | Notification is a prerequisite | Without an accepted notification the execution API is rejected (`PRE_DEBIT_NOTIFICATION_NOT_FOUND` / `_NOT_SENT`, HTTP 422). No charge is attempted | PSP developer docs | <span class="pill secondary">secondary</span> |
| <span class="rule reg">C7</span> | Late cut-off | A notification received at or after 23:50 IST is rejected for a T+1 execution | Decentro docs | <span class="pill secondary">secondary</span> |
| <span class="rule reg">C8</span> | One pending notification | Creating a new one marks all previous pending notifications for that mandate `Cancelled` | Decentro docs | <span class="pill secondary">secondary</span> |
| <span class="rule reg">C10</span> | Instant exemption | No notification required if presentation occurs within 5 minutes of registration | Decentro docs | <span class="pill secondary">secondary</span> |
| <span class="rule reg">C11</span> | Category exemption | FASTag and RuPay NCMC auto-replenishment are exempt from the 24 h notification | RBI E-mandate Framework 2026 | <span class="pill secondary">secondary</span> |
| <span class="rule reg">C13</span> | Notification contents | Merchant name, amount, date and time of debit, mandate reference, reason for debit | RBI 2026 | <span class="pill secondary">secondary</span> |
| <span class="rule reg">C14</span> | Post-debit notification | Required after every debit | RBI 2026 | <span class="pill secondary">secondary</span> |

</div>

### Mandate lifecycle

<div class="table-scroll">

| ID | Constraint | Value | Source | Status |
| --- | --- | --- | --- | --- |
| <span class="rule reg">C9</span> | First-presentation failure | If the **first** presentation fails, the mandate is automatically revoked | Decentro docs | <span class="pill secondary">secondary</span> |
| <span class="rule reg">C12</span> | Mandate must be LIVE | Notification may only be raised against a `LIVE` mandate | PSP docs | <span class="pill secondary">secondary</span> |
| <span class="rule reg">C17</span> | AFA on lifecycle events | Registration, modification and withdrawal each require an additional factor of authentication | RBI 2026 | <span class="pill secondary">secondary</span> |
| <span class="rule reg">C18</span> | Opt-out | Customer may modify or withdraw at any time, subject to AFA; the pre-transaction notice must carry an opt-out | RBI 2026 | <span class="pill secondary">secondary</span> |
| <span class="rule reg">C21</span> | Validity period | Every e-mandate must specify one | RBI 2026 | <span class="pill secondary">secondary</span> |

</div>

### Amounts

<div class="table-scroll">

| ID | Constraint | Value | Source | Status |
| --- | --- | --- | --- | --- |
| <span class="rule reg">C15</span> | AFA-free ceiling | ₹15,000 per recurring transaction | RBI 2026 | <span class="pill secondary">secondary</span> |
| <span class="rule reg">C16</span> | Raised ceiling | ₹1,00,000 for insurance premiums, mutual-fund SIPs, credit-card bills | RBI 2026 | <span class="pill secondary">secondary</span> |
| <span class="rule reg">C19</span> | Variable mandates | Customer sets a maximum transaction value; any amount up to that cap may be debited without re-authentication | RBI 2026 | <span class="pill secondary">secondary</span> |
| <span class="rule reg">C20</span> | Zero customer charges | No charge may be levied on the customer for the e-mandate facility | RBI 2026 | <span class="pill secondary">secondary</span> |

</div>

### Scope and duties

<div class="table-scroll">

| ID | Constraint | Value | Source | Status |
| --- | --- | --- | --- | --- |
| <span class="rule reg">C22</span> | Grievance redressal | A mechanism must exist | RBI 2026 | <span class="pill secondary">secondary</span> |
| <span class="rule reg">C23</span> | Acquirer duty | Acquirers must ensure merchant compliance | RBI 2026 | <span class="pill secondary">secondary</span> |
| <span class="rule reg">C24</span> | Applicability | All PSPs and participants processing recurring domestic and cross-border transactions via cards, PPIs and UPI | RBI 2026 | <span class="pill secondary">secondary</span> |
| <span class="rule reg">RATCHET</span> | Terminal-cause ratchet | No debit retry against a revoked, expired or otherwise terminal mandate | Derived from C18 and merchant duty | <span class="pill derived">derived</span> |

</div>

### Operational guards

<div class="table-scroll">

| ID | Guard | Purpose |
| --- | --- | --- |
| <span class="rule ops">OPS-ALIGN</span> | Execution must sit on the 30-minute slot grid | Determinism and auditability |
| <span class="rule ops">OPS-AMT</span> | Amount positive and not above the amount due | Sanity |
| <span class="rule ops">OPS-PARTIAL</span> | Partial collection requires a variable-amount mandate | Gates the amount lever per mandate |
| <span class="rule ops">OPS-CYCLE</span> | Execution must land before the cycle closes | Recovery horizon |
| <span class="rule ops">OPS-CONTACT</span> | Per-customer contact cap | Guards against the agent learning that spamming raises recovery |
| <span class="rule ops">OPS-NOPEND</span> | Nothing pending to cancel | Sanity |
| <span class="rule ops">OPS-PAST</span> | No action scheduled in the past | Sanity |

</div>

## Framework dates

- **RBI, Digital Payments – E-mandate Framework, 2026** — notified 21 April 2026, effective
  immediately, consolidating and repealing the prior circulars.
- **NPCI, Guidelines on usage of UPI and API** — notified 21 May 2025, implementation deadline
  31 July 2025, enforcement from 1 August 2025.

**Next:** [The three that matter](/constraints/critical) — most of this register is a filter.
Three rows are not.
