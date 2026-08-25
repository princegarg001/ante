# The problem

Recurring collection on Indian rails fails far more often than most people building on it
expect, and the mechanisms that are supposed to recover those failures are themselves the
mechanism destroying the mandate book.

## Failure is the normal case, not the exception

UPI Autopay debits are stateless: every execution requires a real-time approval against a live
bank balance. There is no stored credential with a float behind it, no issuer willing to
authorise on thin funds. If the money is not in the account at the instant of presentation,
the debit declines.

<div class="stat-grid">
  <div class="stat"><span class="v">~30%</span><span class="k">approval, largest remitter bank</span></div>
  <div class="stat"><span class="v">8–15%</span><span class="k">technical failure rate</span></div>
  <div class="stat"><span class="v">2–3%</span><span class="k">card mandate failure rate</span></div>
  <div class="stat"><span class="v">~20 M</span><span class="k">revocations per month</span></div>
</div>

Those first two numbers measure different things and are frequently conflated. The 8–15%
figure is the *technical* failure rate — timeouts, downtime, routing. The approval rate is the
*business* outcome, and it is dominated by insufficient funds. A system tuned against the
first number will be badly calibrated against the second. See [Market data](/analysis/market)
for the sourcing and the caveats.

## The recovery mechanism is eating the asset

Roughly twenty million UPI Autopay mandates are revoked every month, attributed largely to low
customer balances. That number is worth sitting with, because revocation is not the same as
failure. A failed debit costs a cycle. A revoked mandate costs every future cycle, and
re-registration requires the customer to authenticate again — a step most of them never take.

The causal chain is unglamorous:

1. A debit is presented into an account that cannot cover it.
2. It declines.
3. The merchant retries, because retrying is what recovery software does.
4. It declines again, and the customer — now receiving repeated debit notifications for money
   they do not have — cancels the mandate from their UPI app.

Every step is individually reasonable. The aggregate is an industry spending its own mandate
book to chase single cycles.

## Why the obvious fix is illegal

The natural engineering response is Stripe's: build a model over hundreds of features, predict
the moment of highest success probability, and retry there — repeatedly, across two weeks,
until it works or the customer churns.

Indian regulation forecloses that at four separate points.

<div class="diagram">
<svg viewBox="0 0 720 210" role="img" aria-label="Timeline showing the commit aperture between 48 and 24 hours before execution, and the blind period afterwards">
  <text x="24" y="26" font-size="12" font-weight="600">One bet, from decision to outcome</text>

  <line class="line" x1="24" y1="120" x2="690" y2="120"/>

  <rect class="band-window" x="70" y="62" width="200" height="42" rx="4"/>
  <text x="98" y="80" font-size="11" font-weight="600">COMMIT APERTURE</text>
  <text class="dim" x="98" y="95" font-size="10">raise the notification here, or not at all</text>

  <rect class="box" x="290" y="62" width="270" height="42" rx="4"/>
  <text x="330" y="80" font-size="11" font-weight="600">BLIND</text>
  <text class="dim" x="330" y="95" font-size="10">amount and time are fixed; no revision possible</text>

  <rect class="box-accent" x="580" y="62" width="96" height="42" rx="4"/>
  <text x="600" y="86" font-size="11" font-weight="600">EXECUTE</text>

  <line class="line-accent" x1="70"  y1="112" x2="70"  y2="128"/>
  <line class="line-accent" x1="270" y1="112" x2="270" y2="128"/>
  <line class="line-accent" x1="580" y1="112" x2="580" y2="128"/>

  <text class="mono dim" x="46"  y="146" font-size="10">T−48h</text>
  <text class="mono dim" x="248" y="146" font-size="10">T−24h</text>
  <text class="mono dim" x="566" y="146" font-size="10">T</text>

  <text class="dim" x="24" y="180" font-size="11">The decision must be taken inside a 24-hour-wide window. Information arriving after it</text>
  <text class="dim" x="24" y="196" font-size="11">cannot be acted on, and a customer opt-out during the blind period burns the slot for nothing.</text>
</svg>
</div>

**The retry budget is four.** One execution plus three retries per mandate per cycle. After the
fourth the cycle is over. There is no eighth attempt to schedule.

**Execution windows are legislated.** Peak hours — 10:00–13:00 and 17:00–21:30 IST — are barred
outright. That leaves 16.5 hours a day, in three unequal blocks, and the largest of them
covers the early morning when salary credits land.

**The decision precedes the action by a day.** A debit must be notified 24 to 48 hours ahead,
with the amount and the time fixed at notification. The agent cannot react to anything
learned inside that window.

**Commitments are serialized.** Only one notification may be pending per mandate; raising a new
one cancels the previous. You cannot lay down three bets and wait to see which lands.

## What the problem actually is

Strip out the timing question and what remains is a resource allocation problem with an
unusual cost structure:

> Given a batch of failed mandates, a hard budget of four presentations each, a shared and
> throttled supply of execution slots, a 24-hour blind commitment, and a mandate that can be
> destroyed by using it — which mandates get a slot, at what amount, and which get nothing?

The last clause is the one that is usually missing. Because a failed first presentation
revokes the mandate, spending a slot is not merely a cost — it risks the entire remaining
lifetime value of the subscription. For a ₹499 monthly plan with fourteen months of expected
life left, the agent is risking roughly fourteen times the amount it is trying to collect.

That ratio is why stopping is worth money, and why an agent that only maximises recovery is
the wrong agent.

**Next:** [Prior art](/guide/prior-art) — what the existing products do, and precisely where the
transplant fails.
