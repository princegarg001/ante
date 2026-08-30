# COMPLIANCE.md — The Regulatory Spine

Every constraint the engine enforces, with the instrument it comes from and how
firmly it is established. The rule identifiers below appear verbatim in every
veto the system emits.

Verification pass: **31 August 2026**.

---

## A. Status vocabulary

A single `SECONDARY` label hid an important distinction: a rule quoted
identically by four independent sources and traceable to a numbered circular is
not in the same position as one that appears in exactly one payment provider's
API documentation. The register now separates them.

| Status | Meaning |
|---|---|
| <code>PRIMARY</code> | The instrument itself has been read, and the clause is cited |
| <code>ATTRIBUTED</code> | Numbered circular identified; the provision is quoted consistently by independent legal and industry sources; the PDF itself not obtained |
| <code>PROVIDER</code> | Established only from payment-provider API documentation. May be that provider's implementation rather than regulation |
| <code>DISPUTED</code> | Sources give materially different values |
| <code>DERIVED</code> | Follows arithmetically from another row |

**Nothing here is `PRIMARY` yet.** The NPCI operating circulars are distributed
to member banks and are not publicly downloadable; the RBI notification is
public and is the next thing to obtain in full.

---

## B. The instruments

| Instrument | Number | Date | Notes |
|---|---|---|---|
| RBI, *Digital Payments – E-mandate Framework, 2026* | **RBI/DPSS/2026-27/396** | 21 April 2026 | Issued under ss. 10(2) r/w 18, Payment and Settlement Systems Act 2007. Effective immediately. Repeals eight circulars spanning 21 Aug 2019 – 22 Aug 2024 |
| NPCI, *Guidelines on usage of UPI API* | **NPCI/UPI/OC/215A/2025-26** | 21 May 2025 | Implementation deadline 31 July 2025; enforcement from 1 Aug 2025 |

Circulars repealed by the RBI framework include
`DPSS.CO.PD.No.447/02.14.003/2019-20` (21 Aug 2019) and
`DPSS.CO.PD.No.1324/02.23.001/2019-20` (10 Jan 2020), among six others.

---

## C. Constraint register

### Execution budget and windows — NPCI/UPI/OC/215A/2025-26

| # | Constraint | Value | Status |
|---|---|---|---|
| C1 | Retry cap | 1 execution + 3 retries per mandate **per sequence number** | `ATTRIBUTED` |
| C2 | Peak hours — execution barred | **10:00–13:00** and **17:00–21:30 IST** | `ATTRIBUTED` |
| C3 | Non-peak — execution permitted | 00:00–10:00, 13:00–17:00, 21:30–24:00 IST (16.5 h/day) | `DERIVED` from C2 |
| C4 | Throughput | Initiator PSPs must execute at a moderated TPS | `ATTRIBUTED` |

The circular's operative wording on Autopay is *"Execution of Autopay Mandate
shall be initiated by Initiator and the same has to be initiated in non-peak
hours."* Peak hours are defined as the period when UPI transactions reach their
highest transactions per second, observed from 10:00 to 13:00 and 17:00 to 21:30.

### Pre-debit notification

| # | Constraint | Value | Status |
|---|---|---|---|
| C5a | Notification **minimum** lead | ≥ 24 h before execution. NPCI validates this. | `ATTRIBUTED` |
| C5b | Notification **maximum** lead | 48 h | **`DISPUTED` — see §D.1** |
| C6 | Notification is a prerequisite | Without an accepted notification the execution API is rejected (`PRE_DEBIT_NOTIFICATION_NOT_FOUND` / `_NOT_SENT`, HTTP 422) | `PROVIDER` |
| C7 | Late cut-off | A notification at/after 23:50 IST is rejected for a T+1 execution | `PROVIDER` (single source) |
| C8 | One pending notification | A new notification marks previous pending ones `Cancelled` | **`PROVIDER` (single source) — see §D.2** |
| C10 | Instant exemption | No notification if presentation is within 5 minutes of registration | `PROVIDER` |
| C11 | Category exemption | FASTag and RuPay NCMC auto-replenishment exempt from the 24 h notification | `ATTRIBUTED` — RBI 2026 |

### RBI, Digital Payments – E-mandate Framework, 2026

| # | Constraint | Value | Clause | Status |
|---|---|---|---|---|
| C13 | Pre-transaction notification | *"at least twenty-four hours prior to the actual charge or debit"*, carrying merchant name, transaction amount, debit date/time, e-mandate reference number and reason for debit | — | `ATTRIBUTED` |
| C14 | Post-transaction notification | Merchant name, amount, debit date/time, transaction and e-mandate reference numbers, reason, **and grievance-redressal details** | — | `ATTRIBUTED` |
| C15 | AFA-free ceiling | ₹15,000 per recurring transaction | — | `ATTRIBUTED` |
| C16 | Raised AFA-free ceiling | ₹1,00,000 for insurance premiums, mutual-fund subscriptions, credit-card bills | — | `ATTRIBUTED` |
| C17 | AFA on lifecycle events | Registration, modification and withdrawal each require AFA | **cl. 4** | `ATTRIBUTED` |
| C18 | Opt-out | Per transaction or per mandate; the opt-out itself requires validation by AFA | — | `ATTRIBUTED` |
| C19 | Variable mandates | Customer may specify the maximum value for any single recurring transaction | — | `ATTRIBUTED` |
| C20 | Zero customer charges | *"No charges shall be levied upon the customer for availing the e-mandate facility."* | **cl. 10** | `ATTRIBUTED` |
| C21 | Validity period | Every e-mandate must specify a definite validity period | **cl. 4** | `ATTRIBUTED` |
| C22 | Grievance redressal | Mechanism must exist; unauthorised-transaction liability applies | **cl. 9** | `ATTRIBUTED` |
| C24 | Applicability | *"all payment system providers and payment system participants engaged in the processing of recurring transactions"* — cards, PPIs and UPI, domestic and cross-border | — | `ATTRIBUTED` |

**The first transaction always requires AFA.** Where it is contemporaneous with
registration the two may be combined into a single authentication step. The
₹15,000 ceiling governs *subsequent* transactions.

### Mandate lifecycle

| # | Constraint | Value | Status |
|---|---|---|---|
| C9 | First-presentation failure revokes the mandate | — | **`PROVIDER` (single source) — see §D.3** |
| C12 | Mandate must be LIVE to notify | — | `PROVIDER` |
| RATCHET | No debit retry against a terminal cause | — | `DERIVED` from C18 and merchant duty |

### Operational guards (not regulation)

`OPS-ALIGN` slot grid · `OPS-AMT` amount sanity · `OPS-PARTIAL` variable-amount
gate · `OPS-CYCLE` cycle horizon · `OPS-CONTACT` contact cap · `OPS-NOPEND` ·
`OPS-PAST`. These are merchant policy and blast-radius control. The headline
compliance claim counts regulatory rules only.

---

## D. What this pass changed

### D.1 · The 48-hour ceiling is not regulation

This is the substantive correction.

NPCI validates a **24-hour minimum**. The upper bound does not come from the
circular — it comes from payment providers, and they do not agree with each
other:

| Provider | Stated window |
|---|---|
| Decentro | 24–48 h |
| Setu / PayU | 36–48 h |
| Others | 48–72 h |

Three different ceilings cannot all be the same regulation. The honest reading
is that **the floor is regulatory and the ceiling is the provider's**.

**What this does and does not change.** The aperture is still real — a merchant
integrating through a given PSP genuinely cannot notify earlier than that
provider's window allows, so the two-sided commitment structure the allocator
plans against is the environment a merchant actually faces. What changes is the
claim: it is a PSP-imposed aperture, not an NPCI one, and the pitch must say so.
Saying "NPCI mandates a 48-hour ceiling" to a room that knows the circular would
be a self-inflicted wound.

The constant stays at 48 h, which is the tightest commonly quoted ceiling and
therefore the conservative choice.

### D.2 · C8 remains single-sourced

Only one provider's documentation states that a new notification cancels
previous pending ones. Nothing contradicts it, and it is consistent with there
being a single `presentations_sequence_id` per mandate — but it has one source.

It is load-bearing: it is the reason retries are serialized and the reason this
is a sequential decision problem rather than a knapsack. Flagged as the highest
remaining evidential risk in the design.

### D.3 · C9 remains single-sourced, and the simulator already narrowed it

Every secondary reference to "the first presentation failing revokes the
mandate" traces back to the same provider documentation.

Independently, [calibration rejected the broad reading on evidence](https://princegarg001.github.io/ante/system/simulator):
applied to the first presentation of *every* cycle it produced a 58% monthly
revocation rate against a market reporting roughly 20 M revocations on 808 M
executions. The simulator therefore scopes it to newly registered mandates, and
the scope is a configuration field rather than an assumption buried in code.

So the position is: single-sourced, narrowed by evidence, and parameterised so
the design degrades rather than collapses if it turns out to be narrower still
or absent.

---

## E. What is still outstanding

- [ ] Obtain `RBI/DPSS/2026-27/396` in full from rbi.org.in and move the RBI rows to `PRIMARY` with clause numbers for every row, not just 4, 9 and 10.
- [ ] Obtain `NPCI/UPI/OC/215A/2025-26`. NPCI operating circulars are distributed to member banks rather than published, so this likely needs a member-bank or PSP contact.
- [ ] Resolve C8 and C9 against the NPCI UPI Autopay operating circular rather than provider FAQs.
- [ ] Confirm whether C1's "per sequence number" means per mandate per cycle, or per presentation sequence id — the design assumes the former.
- [ ] Resolve the ~74% business-decline figure's denominator, or continue not quoting it (§F).

---

## F. Market base rates

| Figure | Value | Source | Use |
|---|---|---|---|
| UPI Autopay revocations | ~20 M / month, driven by low balances | Business Standard, Sept 2025 | Usable, cited as press |
| New mandate registrations | ~50 M in July 2025 (vs ~26 M July 2024) | Same | Usable |
| Mandate executions | ~808 M in July 2025 | Same | Usable |
| Approval rate, largest remitter bank | ~30% of auto-debits approved | Same | Usable with attribution |
| Business decline rate, top-50 banks | ~74% | Same | **Do not quote — denominator ambiguous** |
| UPI Autopay technical failure rate | 8–15% | productgrowth.in | Directional, blog |
| Card mandate failure rate | 2–3% | Same | Directional, blog |

The 74% figure is ambiguous between "74% of all auto-debit attempts are
business-declined" and "74% *of declines* are business rather than technical".
Those are very different claims. Until the primary figure is available, use:

> "Industry reporting puts auto-debit approval rates at the largest remitter
> bank around 30%, with the bulk of failures being business declines —
> insufficient funds — rather than technical failures."

---

## G. Sources consulted

**Primary instruments identified (not yet obtained in full)**
- RBI/DPSS/2026-27/396 — *Digital Payments – E-mandate Framework, 2026*, 21 Apr 2026
- NPCI/UPI/OC/215A/2025-26 — *Guidelines on usage of UPI API*, 21 May 2025

**Legal analysis**
- Agrud Partners — clause-level analysis of the 2026 framework (cl. 4, 9, 10)
- Mondaq — *A Consolidated Regulatory Architecture for Recurring Transactions* (verbatim clause text)
- SCC Online; KPMG India; Conventus Law; LexOrbis; India Law

**Provider documentation**
- Decentro — UPI Autopay Pre-Debit Notification API
- Setu, PayU, Juspay, Yuno — notification timing windows
- Razorpay Docs — UPI error codes, recurring payments APIs, subscriptions test mode

**Market**
- Business Standard, Sept 2025 — revocation and approval statistics
- productgrowth.in — UPI AutoPay design guide
