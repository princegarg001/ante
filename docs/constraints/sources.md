# Verification status

Every regulatory claim in this project, and how firmly it is established. This
page exists because getting a real constraint wrong in front of a payments panel is worse
than not claiming it — and because the honest position on some of these rows is more
interesting than a confident one would have been.

**Verification pass: 31 August 2026.**

<div class="stat-grid">
  <div class="stat"><span class="v">0</span><span class="k">rows primary</span></div>
  <div class="stat ok"><span class="v">17</span><span class="k">rows attributed</span></div>
  <div class="stat"><span class="v">5</span><span class="k">rows provider-only</span></div>
  <div class="stat"><span class="v">1</span><span class="k">row disputed</span></div>
</div>

## The status vocabulary changed

Before this pass, every row said `SECONDARY`. That hid the distinction that actually
matters: a provision quoted identically by four independent legal analyses and traceable to
a numbered circular is not in the same evidential position as one appearing in exactly one
payment provider's API documentation.

<div class="table-scroll">

| Status | Meaning |
| --- | --- |
| <span class="pill primary">primary</span> | The instrument itself has been read, and the clause is cited |
| <span class="pill attributed">attributed</span> | Numbered circular identified; quoted consistently by independent sources; PDF not obtained |
| <span class="pill provider">provider</span> | Established only from payment-provider API documentation. May be that provider's implementation rather than regulation |
| <span class="pill disputed">disputed</span> | Sources give materially different values |
| <span class="pill derived">derived</span> | Follows arithmetically from another row |

</div>

**Nothing is `primary` yet.** NPCI operating circulars are distributed to member banks
rather than published; the RBI notification is public and is the next thing to obtain in
full.

## The instruments, now identified

<div class="table-scroll">

| Instrument | Number | Date |
| --- | --- | --- |
| RBI, *Digital Payments – E-mandate Framework, 2026* | **RBI/DPSS/2026-27/396** | 21 April 2026 |
| NPCI, *Guidelines on usage of UPI API* | **NPCI/UPI/OC/215A/2025-26** | 21 May 2025 |

</div>

The RBI framework was issued under sections 10(2) read with 18 of the Payment and
Settlement Systems Act 2007, is effective immediately, and repeals eight circulars spanning
21 August 2019 to 22 August 2024 — including `DPSS.CO.PD.No.447/02.14.003/2019-20` and
`DPSS.CO.PD.No.1324/02.23.001/2019-20`.

Three RBI rows now carry clause numbers: **clause 4** (registration, AFA, validity period),
**clause 9** (grievance redressal and unauthorised-transaction liability) and **clause 10**
(*"No charges shall be levied upon the customer for availing the e-mandate facility."*).

## The substantive correction: the 48-hour ceiling is not regulation

This is what the pass was for.

The design has always treated the notification window as **two-sided** — you cannot notify
later than 24 hours before the debit, and you cannot notify earlier than 48. The two-sided
aperture is one of the three constraints the whole allocator is built around.

The floor is regulation. NPCI validates a 24-hour minimum. **The ceiling is not.** It comes
from payment providers, and they do not agree with each other:

<div class="table-scroll">

| Provider | Stated window |
| --- | --- |
| Decentro | 24–48 h |
| Setu / PayU | 36–48 h |
| Others | 48–72 h |

</div>

Three different ceilings cannot all be the same regulation.

**What this changes, and what it does not.** The aperture is still real: a merchant
integrating through a given PSP genuinely cannot notify earlier than that provider's window
allows, so the two-sided commitment structure the allocator plans against is the environment
a merchant actually faces. The constant stays at 48 hours — the tightest commonly quoted
ceiling, and therefore the conservative choice.

What changes is **the sentence that may be said out loud**. "NPCI mandates a 48-hour
ceiling" is not supportable. "The floor is regulatory; the ceiling is our PSP's" is. Saying
the first to a room that has read the circular would be a self-inflicted wound, and there is
now [a test whose entire purpose is to stop it being said](https://github.com/princegarg001/ante/blob/main/tests/test_regulatory_constants.py).

## Two rows that remain single-sourced

### C8 — one pending notification per mandate

Only one provider's documentation states that raising a new notification cancels previous
pending ones. Nothing contradicts it, and it is consistent with there being a single
`presentations_sequence_id` per mandate. But it has one source.

It is load-bearing: it is the reason retries are **serialized**, and therefore the reason
this is a sequential decision problem rather than a knapsack. This is the highest remaining
evidential risk in the design, and it is worth stating plainly rather than hoping nobody asks.

### C9 — first-presentation failure revokes the mandate

Every secondary reference traces back to the same provider documentation.

Independently of that, [calibration rejected the broad reading on evidence](/system/simulator#two-things-calibration-settled):
applied to the first presentation of *every* cycle it produced a 58% monthly revocation rate,
against a market reporting roughly 20 million revocations on 808 million executions. The
simulator scopes it to newly registered mandates, and the scope is a configuration field.

So the position is: single-sourced, narrowed by evidence, and parameterised so the design
degrades rather than collapses if it turns out narrower still — or absent.

::: tip Why this is a good position rather than a weak one
Two of the three constraints the thesis rests on are single-sourced, and the project says so
on its own documentation site. That is a stronger place to be than a register that reads as
settled fact and turns out not to be. The design was built to survive both being wrong: C9
is a config flag, and C8's serialization is what the rails impose in practice regardless of
who imposes it.
:::

## Still outstanding

- [ ] Obtain `RBI/DPSS/2026-27/396` in full and move the RBI rows to `primary`, with a clause number on every row rather than three
- [ ] Obtain `NPCI/UPI/OC/215A/2025-26` — likely needs a member-bank or PSP contact
- [ ] Resolve C8 and C9 against the NPCI UPI Autopay operating circular rather than provider FAQs
- [ ] Confirm whether C1's *"per sequence number"* means per mandate per cycle or per presentation sequence id. The design assumes the former
- [ ] Resolve the ~74% business-decline denominator, or continue not quoting it

## On the number nobody should quote yet

The ~74% business-decline figure is ambiguous between "74% of all auto-debit attempts are
business-declined" and "74% *of declines* are business rather than technical". Those are
very different claims and a payments panel will know which is right. The safe formulation,
used everywhere in this project:

> Industry reporting puts auto-debit approval rates at the largest remitter bank around 30%,
> with the bulk of failures being business declines — insufficient funds — rather than
> technical failures.

## Sources consulted

**Instruments identified, not yet obtained in full**
RBI/DPSS/2026-27/396 · NPCI/UPI/OC/215A/2025-26

**Legal analysis** — Agrud Partners (clause-level) · Mondaq · SCC Online · KPMG India ·
Conventus Law · LexOrbis · India Law

**Provider documentation** — Decentro · Setu · PayU · Juspay · Yuno · Razorpay Docs

**Market** — Business Standard (Sept 2025) · productgrowth.in
