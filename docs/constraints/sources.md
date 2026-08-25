# Verification status

Every regulatory claim in this project is currently sourced from secondary material. This page
tracks the work of confirming each against the instrument itself, and it exists because
getting a real constraint wrong in front of a payments panel is worse than not claiming it.

<div class="stat-grid">
  <div class="stat"><span class="v">0</span><span class="k">rows confirmed primary</span></div>
  <div class="stat"><span class="v">22</span><span class="k">rows secondary</span></div>
  <div class="stat"><span class="v">2</span><span class="k">rows derived</span></div>
  <div class="stat"><span class="v">3</span><span class="k">rows single-sourced</span></div>
</div>

## Why this page is public

It would be straightforward to write these constraints as settled fact. Most submissions will.
The reason not to is that three of the rules driving the entire design come substantially from
one payment service provider's developer documentation, and a PSP documenting its own
implementation is not the same as a regulator publishing a rule.

Stating that openly costs nothing if the rules hold, and costs far less than being corrected
in a panel room if they do not. The `git` history of this page is intended to be part of the
evidence: each row moving from `SECONDARY` to `PRIMARY` is a verification that actually
happened, on a date, against a document.

## Checklist

### Highest risk — verify first

These three drive the architecture. If any is wrong, a section of the design is wrong with it.

<div class="table-scroll">

| Row | Claim | Why it is risky | Needed |
| --- | --- | --- | --- |
| <span class="rule reg">C9</span> | Failed first presentation revokes the mandate | Single PSP source. Drives the entire option-value term | NPCI UPI Autopay operating circular, not a PSP FAQ |
| <span class="rule reg">C8</span> | Only one pending notification per mandate | Single PSP source. Drives the serialization of the whole policy | Confirm NPCI-level, not a Decentro implementation detail |
| <span class="rule reg">C5</span> | Notification aperture upper bound is 48 h | PSPs disagree: 24–48 h, 36–48 h and 48–72 h are all quoted | Determine whether the ceiling is NPCI regulation or PSP policy |

</div>

::: tip Say which in the pitch
If the 48-hour ceiling turns out to be PSP convention rather than regulation, that is worth
stating explicitly rather than quietly dropping. "The floor is regulatory, the ceiling is our
PSP's" is a more credible sentence than either overstating or omitting it.
:::

### Standard verification

<div class="table-scroll">

| Task | Source needed | Confirms |
| --- | --- | --- |
| Download NPCI *Guidelines on usage of UPI and API* (21 May 2025) | npci.org.in | C1, C2, C4 |
| Confirm the wording of "per sequence number" in the retry cap | Same | Whether the budget is per mandate per cycle, or per presentation sequence id |
| Download RBI *Digital Payments – E-mandate Framework, 2026* (21 Apr 2026) | rbi.org.in | C13–C24, and clause numbers for each |
| Confirm AFA-free ceilings and the eligible category list | Same | C15, C16 |
| Confirm the FASTag / NCMC notification exemption | Same | C11 |
| Record retrieval date and URL for every row | — | Traceability |

</div>

### Market statistics

<div class="table-scroll">

| Figure | Status | Action |
| --- | --- | --- |
| ~20 M monthly revocations | Press, attributed to industry data | Usable as reported, cited as press |
| ~30% approval at the largest remitter bank | Press | Verify |
| ~74% business decline across top-50 banks | **Denominator ambiguous** | Do not quote until resolved — see [Market data](/analysis/market) |
| 8–15% UPI Autopay technical failure rate | Industry blog | Cite as directional, not audited |

</div>

## Sources consulted

Retrieved 24 August 2026.

**Regulatory analysis**
- SCC Online — *Your guide to UPI changes starting August 1, 2025*
- SCC Online — *RBI notifies Digital Payments — E-mandate Framework, 2026*
- KPMG India — RBI Digital Payments E-Mandate Framework 2026

**Implementation documentation**
- Razorpay Docs — UPI error codes, recurring payments UPI APIs, subscriptions test mode, webhook payloads
- Decentro Docs — UPI Autopay Pre-Debit Notification API
- Setu, PayU, Juspay, Yuno developer documentation — notification timing windows

**Market**
- Business Standard — *UPI autopay revocations hit 20 mn per month on low customer balance*, Sept 2025
- productgrowth.in — UPI AutoPay design guide
- Razorpay — *Master Recurring Payments with UPI 2.0 Autopay: 2026 Guide*

## How the register is maintained

The register lives in `COMPLIANCE.md` in the repository and is mirrored here. When a row is
confirmed, three things change together:

1. Status becomes `PRIMARY` and the clause reference is recorded.
2. If the value changed, the constant changes in `mandate_recovery/constraints/rules.py`.
3. The corresponding assertion in `tests/test_regulatory_constants.py` changes.

That third step is deliberate. Regulatory constants are pinned to literals in a test file that
cites the circular, so a value cannot drift without someone editing an assertion that names
its source. The reason this exists is documented under [mutation testing](/engineering/mutation)
— the need for it was discovered rather than anticipated.
