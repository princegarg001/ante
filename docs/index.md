---
layout: home

hero:
  name: Ante
  text: A retry-slot allocator for regulated rails
  tagline: >
    In India you get three retries, non-peak windows only, and a debit you must
    commit to twenty-four hours blind. Every other recovery product answers
    "when should I retry?" That question is not available here.
  actions:
    - theme: brand
      text: The problem
      link: /guide/problem
    - theme: alt
      text: Constraint register
      link: /constraints/
    - theme: alt
      text: GitHub
      link: https://github.com/princegarg001/ante

features:
  - title: Scarcity, not timing
    details: >
      NPCI caps execution at one attempt plus three retries per mandate per
      cycle. The problem is not when to spend a slot — it is whether this
      mandate deserves one, priced against every other mandate bidding for the
      same window.
  - title: Serialized commitment
    details: >
      Only one pre-debit notification may be pending per mandate; raising a new
      one cancels the old. Retries cannot be batch-allocated. Commit, wait
      blind, observe, re-plan.
  - title: The mandate is collateral
    details: >
      A failed first presentation revokes the mandate. The downside of a retry
      is not one failed cycle — it is the whole remaining lifetime value. That
      is what makes stopping worth money.
  - title: Verified, not asserted
    details: >
      2.5 million state-action-clock triples enumerated with zero violations,
      cross-checked against an independent restatement of the regulation, and
      the compliance suite is itself mutation-tested.
---

<div class="stat-grid">
  <div class="stat ok"><span class="v">2,511,760</span><span class="k">triples enumerated</span></div>
  <div class="stat ok"><span class="v">0</span><span class="k">violations found</span></div>
  <div class="stat ok"><span class="v">11 / 11</span><span class="k">mutants killed</span></div>
  <div class="stat"><span class="v">4</span><span class="k">attempts per cycle</span></div>
  <div class="stat"><span class="v">16.5 h</span><span class="k">permitted per day</span></div>
  <div class="stat"><span class="v">24–48 h</span><span class="k">commit aperture</span></div>
</div>

## The shape of the problem

Stripe Smart Retries, Recurly Intelligent Retries, Gr4vy — every recovery product on the
market answers the same question: *when should I retry this failed payment?* They assume
retries are cheap and roughly unlimited. Stripe's published default is around eight attempts
across two weeks.

That policy cannot be run in India.

<div class="diagram">
<svg viewBox="0 0 720 132" role="img" aria-label="A day of execution windows in IST, showing two peak windows in which execution is forbidden">
  <rect class="band-ok"   x="40"  y="40" width="226" height="34" rx="3"/>
  <rect class="band-peak" x="266" y="40" width="90"  height="34" rx="3"/>
  <rect class="band-ok"   x="356" y="40" width="120" height="34" rx="3"/>
  <rect class="band-peak" x="476" y="40" width="135" height="34" rx="3"/>
  <rect class="band-ok"   x="611" y="40" width="69"  height="34" rx="3"/>

  <line class="line" x1="40" y1="82" x2="680" y2="82"/>
  <text class="mono dim" x="40"  y="98" font-size="10">00:00</text>
  <text class="mono dim" x="248" y="98" font-size="10">10:00</text>
  <text class="mono dim" x="338" y="98" font-size="10">13:00</text>
  <text class="mono dim" x="458" y="98" font-size="10">17:00</text>
  <text class="mono dim" x="586" y="98" font-size="10">21:30</text>
  <text class="mono dim" x="650" y="98" font-size="10">24:00</text>

  <text x="40" y="28" font-size="12" font-weight="600">Execution windows, IST</text>
  <text class="dim" x="292" y="62" font-size="10">PEAK</text>
  <text class="dim" x="518" y="62" font-size="10">PEAK</text>
  <text class="dim" x="40" y="122" font-size="11">Permitted 16.5 h/day · 33 half-hour slots · peak execution is barred outright</text>
</svg>
</div>

## The reframe

> You get one irrevocable, blind, twenty-four-hours-ahead bet at a time, at most four of them,
> and losing the first one can destroy the asset you are collecting against.

That is not a retry schedule. It is sequential decision-making under a serialization
constraint with the mandate posted as collateral — which is the mechanical reason roughly
**twenty million UPI Autopay mandates are revoked every month**, mostly because someone
presented a debit into an empty account.

Everyone else optimises *when* to retry. Ante decides *whether to bet*.

<div style="margin-top:2.5rem">

**Start here** → [The problem](/guide/problem) · [The three constraints that change the algorithm](/constraints/critical) · [How the allocator works](/system/allocator)

</div>
