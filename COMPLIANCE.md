# COMPLIANCE.md — The Regulatory Spine

Every constraint the engine enforces, with the source it came from and its verification
status. **Nothing in this file is law until the row says `PRIMARY`.** Rows marked
`SECONDARY` were established from law-firm notes, PSP developer documentation, or press
reporting and must be re-confirmed against the NPCI / RBI circular PDF before the pitch.

Research date: 24 August 2026.

---

## A. Constraint table

| # | Constraint | Value | Source | Status |
|---|---|---|---|---|
| C1 | Retry cap | 1 original execution + 3 retries per mandate **per sequence number** | NPCI UPI/API Guidelines (notified 21-05-2025, enforced 01-08-2025) | SECONDARY |
| C2 | Peak hours — execution forbidden | **10:00–13:00** and **17:00–21:30 IST** | Same | SECONDARY |
| C3 | Non-peak windows — execution permitted | **00:00–10:00**, **13:00–17:00**, **21:30–24:00** IST (16.5 h/day) | Derived from C2 | DERIVED |
| C4 | Throughput | Initiator PSPs must execute mandate APIs at a "moderated TPS"; rate limits apply | Same | SECONDARY |
| C5 | Pre-debit notification (PDN) timing | Must be sent in the window **[T−48h, T−24h]** before execution time T. NPCI validates the 24 h minimum. | PSP developer docs (Decentro, Setu, PayU, Juspay) | SECONDARY |
| C6 | PDN is a **hard prerequisite** | If the PDN was not sent/accepted, the execution API is rejected (`PRE_DEBIT_NOTIFICATION_NOT_FOUND` / `_NOT_SENT`, HTTP 422). No charge is attempted. | PSP developer docs | SECONDARY |
| C7 | PDN late cut-off | A PDN received at or after **23:50:00** and before 00:00:00 is rejected for a T+1 debit. No such cut-off for T+2 or later. | Decentro docs | SECONDARY |
| C8 | **One pending PDN per mandate** | Creating a new PDN automatically marks all previous `Pending` PDNs for that mandate as `Cancelled`. | Decentro docs | SECONDARY |
| C9 | First-presentation failure | If the **first** presentation on a mandate fails, the mandate is **automatically revoked** per NPCI guidelines. | Decentro docs | SECONDARY — **verify hardest** |
| C10 | PDN exemption — instant | No PDN required if presentation occurs within **5 minutes** of mandate registration. | Decentro docs | SECONDARY |
| C11 | PDN exemption — categories | Auto-replenishment of NETC FASTag and RuPay NCMC are exempt from the 24 h PDN. | RBI E-mandate Framework 2026 | SECONDARY |
| C12 | Mandate must be LIVE | PDN may only be raised against a mandate in `LIVE` status. | PSP developer docs | SECONDARY |
| C13 | Pre-transaction notification content | Merchant name, transaction amount, date & time of debit, e-mandate reference number, reason for debit | RBI E-mandate Framework 2026 | SECONDARY |
| C14 | Post-transaction notification | Required after every debit | RBI 2026 | SECONDARY |
| C15 | AFA-free ceiling | **₹15,000** per recurring transaction | RBI 2026 | SECONDARY |
| C16 | Raised AFA-free ceiling | **₹1,00,000** for insurance premiums, mutual-fund SIPs, credit-card bill payments | RBI 2026 | SECONDARY |
| C17 | AFA on lifecycle events | Registration, modification and withdrawal of an e-mandate each require AFA | RBI 2026 | SECONDARY |
| C18 | Opt-out | Customer may modify or withdraw at any time, subject to AFA; pre-txn notice must carry an opt-out | RBI 2026 | SECONDARY |
| C19 | Variable mandates | Customer sets a maximum transaction value; any amount up to that cap may be debited without re-auth | RBI 2026 | SECONDARY |
| C20 | Zero customer charges | No charge may be levied on the customer for availing the e-mandate facility | RBI 2026 | SECONDARY |
| C21 | Validity period | Every e-mandate must specify one | RBI 2026 | SECONDARY |
| C22 | Grievance redressal | Mechanism must exist | RBI 2026 | SECONDARY |
| C23 | Acquirer duty | Acquirers must ensure merchant compliance with the framework | RBI 2026 | SECONDARY |
| C24 | Scope | Applies to all PSPs and participants processing recurring domestic **and cross-border** transactions via **cards, PPIs and UPI** | RBI 2026 | SECONDARY |

**Framework dates.** RBI *Digital Payments – E-mandate Framework, 2026* notified **21 April
2026**, effective immediately, consolidating and repealing the prior circulars. NPCI UPI/API
guidelines notified **21 May 2025**, implementation deadline 31 July 2025, enforcement from
**1 August 2025**.

---

## B. The three constraints that change the algorithm

Most of the table above is a filter. Three rows are not — they change what problem you are solving.

### B1 · The commit window is bounded on **both** sides — C5

The original plan modelled the notification rule as `slot >= now + 24h`. That is wrong in a
way that matters. The real rule is a **two-sided window**:

```
commit_time  in  [ T - 48h , T - 24h ]
```

You cannot notify a week early and you cannot notify late. So for every candidate execution
time T there is a 24-hour-wide interval during which the decision must be taken, and outside
which T is unreachable. The agent is not "planning with a 24 h lookahead lock" — it is
**scheduling irrevocable commitments into a rolling 24-hour-wide aperture**.

### B2 · Only one commitment may be in flight per mandate — C8

A new PDN cancels the previous pending PDN. Therefore a mandate can have **at most one
scheduled execution outstanding at any instant**. You cannot pre-allocate all three retry
slots up front and let them run.

This is the single most important structural fact in the system, and it is why this is a
sequential decision problem rather than a knapsack. The allocation is *serialized*: commit,
wait ≥24 h blind, observe, re-plan. Every submission that solves "pick 3 slots at once" is
solving a problem that does not exist.

### B3 · The first presentation is played for the whole mandate — C9

If the first presentation on a mandate fails, the mandate is revoked and the customer must
re-register with AFA. The downside of attempt #1 is not one failed cycle — it is the entire
remaining lifetime value of the subscription.

This forces **option value** into the objective function. A retry is a bet with the mandate
posted as collateral. It is also the mechanical explanation for the ~20 million UPI Autopay
revocations per month attributed to low balances: the ecosystem is presenting debits into
empty accounts and destroying its own mandate book.

> If C9 turns out to apply only to the initial presentation of a *newly registered* mandate
> and not to the first presentation of each cycle, the option-value term shrinks but does not
> vanish — customer-initiated revocation after repeated failed debits keeps it alive. Build
> the term either way; parameterise its size so the claim degrades gracefully.

---

## C. Market base rates (for simulator calibration — cite carefully)

| Figure | Value | Source | Caution |
|---|---|---|---|
| UPI Autopay technical failure rate | 8–15% | productgrowth.in design guide | Blog, not audited. Directional. |
| Card mandate failure rate | 2–3% | Same | Same |
| UPI Autopay mandate revocations | ~20 million / month, driven by low balance | Business Standard, Sept 2025 | Press citing industry data |
| New mandate registrations | ~50 M in July 2025 (vs ~26 M July 2024) | Same | |
| Mandate executions | ~808 M in July 2025 | Same | |
| SBI auto-debit approval rate | ~30% approved | Same | **Verify** |
| Top-50-bank business decline rate | ~74% average | Same | **Verify denominator — see below** |

**Read this before you quote the 74% number.** The reporting is ambiguous between "74% of
all auto-debit attempts are business-declined" and "74% *of declines* are business declines
rather than technical." Those are very different claims and a Razorpay panel will know which
one is right. Until you have the primary figure, use the safe form:

> "Industry reporting puts auto-debit approval rates at the largest remitter bank around
> 30%, with the bulk of failures being business declines — insufficient funds — rather than
> technical failures."

Do not put 74% on a slide until you can source the denominator.

---

## D. Verification checklist — complete before the pitch

- [ ] Download the NPCI *Guidelines on usage of UPI and API* (21 May 2025) PDF from npci.org.in. Confirm C1, C2, C4.
- [ ] Confirm the exact wording of "per sequence number" in C1 — is the budget per mandate per cycle, or per presentation sequence id?
- [ ] Download the RBI *Digital Payments – E-mandate Framework, 2026* (21 April 2026) from rbi.org.in. Confirm C13–C24 and record clause numbers in this file.
- [ ] Confirm C5's 48 h upper bound is an **NPCI** rule and not a PSP convention. PSPs quote 24–48 h, 36–48 h and 48–72 h. If they disagree, the upper bound is probably PSP policy, not regulation. **Say which in the pitch.**
- [ ] Confirm C9 against NPCI's UPI Autopay operating circular, not a PSP FAQ. Highest-leverage and highest-risk claim in the build.
- [ ] Confirm C8 is NPCI-level and not a Decentro implementation detail.
- [ ] Record retrieval date and URL for every row.

When a row is confirmed, change its status to `PRIMARY` and add the clause reference. The
git diff of this file over the fortnight is itself a credibility artifact — it shows you checked.

---

## E. Sources consulted (24 Aug 2026)

- SCC Online — *Your guide to UPI changes starting August 1, 2025* — peak-hour definition, Autopay execution windows, API rate limits
- SCC Online — *RBI notifies Digital Payments — E-mandate Framework, 2026* (24 Apr 2026)
- KPMG India — RBI Digital Payments E-Mandate Framework 2026 insight
- Razorpay — *Master Recurring Payments with UPI 2.0 Autopay: 2026 Guide*
- Razorpay Docs — UPI error codes; recurring payments UPI APIs; subscriptions test mode; webhook payloads
- Decentro Docs — UPI Autopay Pre-Debit Notification API reference
- Setu / PayU / Juspay / Yuno developer docs — PDN timing windows
- Business Standard — *UPI autopay revocations hit 20 mn per month on low customer balance* (Sept 2025)
- productgrowth.in — UPI AutoPay design guide
