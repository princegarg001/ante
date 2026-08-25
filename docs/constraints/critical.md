# The three that matter

Most of the [constraint register](/constraints/) is a filter — rules that reject certain
actions and otherwise leave the design untouched. Three rows are not filters. They change what
problem is being solved, and any design that treats them as filters is solving the wrong one.

## C5 · The commit aperture is two-sided

The first draft of this project modelled the notification rule as `execute_at >= now + 24h`:
a floor, a minimum delay. That is the natural reading, and it is wrong in a way that matters.

The actual rule is bounded at both ends:

```
commit_time  ∈  [ T − 48h ,  T − 24h ]
```

You cannot notify a week early, and you cannot notify late. For every candidate execution time
there is exactly one 24-hour-wide interval during which it can be chosen, and outside that
interval it is unreachable.

<div class="diagram">
<svg viewBox="0 0 720 200" role="img" aria-label="Two candidate execution times, each with its own commit aperture, showing that the apertures slide with the target">
  <text x="24" y="24" font-size="12" font-weight="600">Each target time has its own aperture</text>

  <line class="line" x1="24" y1="150" x2="694" y2="150"/>

  <rect class="band-window" x="70" y="52" width="180" height="26" rx="4"/>
  <text class="dim" x="86" y="69" font-size="10">aperture for T₁</text>
  <rect class="box-accent" x="392" y="52" width="70" height="26" rx="4"/>
  <text x="410" y="69" font-size="10" font-weight="600">T₁</text>
  <line class="line-accent" x1="250" y1="65" x2="392" y2="65" stroke-dasharray="3 3"/>

  <rect class="band-window" x="196" y="96" width="180" height="26" rx="4"/>
  <text class="dim" x="212" y="113" font-size="10">aperture for T₂</text>
  <rect class="box-accent" x="518" y="96" width="70" height="26" rx="4"/>
  <text x="536" y="113" font-size="10" font-weight="600">T₂</text>
  <line class="line-accent" x1="376" y1="109" x2="518" y2="109" stroke-dasharray="3 3"/>

  <text class="mono dim" x="52"  y="168" font-size="10">now</text>
  <text class="mono dim" x="300" y="168" font-size="10">+24h</text>
  <text class="mono dim" x="540" y="168" font-size="10">+48h</text>

  <text class="dim" x="24" y="190" font-size="11">Waiting a day does not preserve the option — it exchanges one reachable set of targets for another.</text>
</svg>
</div>

**Why it changes the design.** Under a one-sided rule, waiting is free: the option to execute at
any future time remains available. Under a two-sided rule, waiting *destroys* options. The set
of reachable execution times slides forward continuously, so declining to commit today is a
real decision with a real cost.

This is why `Wait` is an explicit action in the [action space](/system/action-space) rather
than the absence of one, and why it is recorded in the audit log as a decision that was taken.

## C8 · Commitments are serialized

Creating a new pre-debit notification automatically marks all previously pending notifications
for that mandate as cancelled. A mandate therefore has **at most one outstanding commitment at
any instant**.

This is the single most consequential structural fact in the system.

<div class="diagram">
<svg viewBox="0 0 720 218" role="img" aria-label="Comparison of parallel slot allocation, which is impossible, against the serialized commit-observe-replan loop that is required">
  <text x="24" y="22" font-size="12" font-weight="600">What most designs assume</text>
  <rect class="box" x="24" y="34" width="120" height="24" rx="4"/>
  <rect class="box" x="152" y="34" width="120" height="24" rx="4"/>
  <rect class="box" x="280" y="34" width="120" height="24" rx="4"/>
  <text class="dim" x="52" y="50" font-size="10">retry 1 @ +24h</text>
  <text class="dim" x="180" y="50" font-size="10">retry 2 @ +72h</text>
  <text class="dim" x="308" y="50" font-size="10">retry 3 @ +168h</text>
  <text x="420" y="51" font-size="11" font-weight="600" fill="#b91c1c">✕ not reachable</text>
  <text class="dim" x="24" y="76" font-size="10">Three slots chosen up front — a knapsack. C8 forbids it.</text>

  <line class="line" x1="24" y1="96" x2="694" y2="96"/>

  <text x="24" y="122" font-size="12" font-weight="600">What the rails actually permit</text>
  <rect class="box-accent" x="24"  y="136" width="96" height="26" rx="4"/>
  <text x="46" y="153" font-size="10" font-weight="600">COMMIT</text>
  <rect class="box" x="140" y="136" width="96" height="26" rx="4"/>
  <text class="dim" x="160" y="153" font-size="10">BLIND ≥24h</text>
  <rect class="box" x="256" y="136" width="96" height="26" rx="4"/>
  <text class="dim" x="278" y="153" font-size="10">OBSERVE</text>
  <rect class="box" x="372" y="136" width="96" height="26" rx="4"/>
  <text class="dim" x="392" y="153" font-size="10">RE-PLAN</text>

  <path class="line-accent" d="M 120 149 L 140 149"/>
  <path class="line-accent" d="M 236 149 L 256 149"/>
  <path class="line-accent" d="M 352 149 L 372 149"/>
  <path class="line-accent" d="M 420 166 L 420 180 L 72 180 L 72 164" stroke-dasharray="4 3"/>

  <text class="dim" x="24" y="206" font-size="11">A loop, at most four times around. This is why it is a sequential decision problem and not an assignment.</text>
</svg>
</div>

**Why it changes the design.** A batch allocator that assigns three slots per mandate up front
is solving a problem that does not exist on these rails. The real structure is: commit one
bet, go blind for at least twenty-four hours, observe the outcome, re-plan with what you
learned. Information arrives between bets, and the policy must be able to use it.

It also means re-planning has a cost. Because a new notification silently cancels the old one,
the system requires an explicit `CancelPending` action before re-committing, so abandoning a
commitment appears in the audit log as a decision rather than as a side effect.

## C9 · The mandate is the collateral

If the **first** presentation on a mandate fails, the mandate is revoked. The customer must
re-register, which requires an additional factor of authentication — a step most customers
never take.

**Why it changes the design.** The downside of a retry is not the loss of one cycle. It is the
loss of every future cycle. The objective function has to carry that term:

```
V  =  E[ rupees recovered this cycle ]
    −  cost of attempts and contacts
    +  L × P(mandate still LIVE at cycle end)          ← option value
```

`L` is the mandate's continuation value — expected discounted net revenue over its remaining
life. For a ₹499 monthly plan with fourteen months of expected life remaining, `L` is roughly
**fourteen times** the amount being chased.

<div class="stat-grid">
  <div class="stat"><span class="v">₹499</span><span class="k">amount at stake this cycle</span></div>
  <div class="stat"><span class="v">≈ ₹7,000</span><span class="k">continuation value at risk</span></div>
  <div class="stat"><span class="v">14×</span><span class="k">ratio of collateral to prize</span></div>
</div>

That ratio is the entire argument for stopping, and it is why an agent optimising recovery
alone is the wrong agent. It is also the mechanism behind twenty million monthly revocations:
an industry repeatedly staking a fourteen-times asset to win a one-times prize.

::: warning If C9 turns out to be narrower
C9 is the highest-risk claim in the build — it comes from a single PSP's documentation. It may
apply only to the initial presentation of a newly registered mandate rather than to the first
presentation of each cycle.

If so, the option-value term shrinks but does not vanish: customer-initiated revocation after
repeated failed debits keeps it alive, and that behaviour is independently evidenced by the
revocation statistics. The term is therefore parameterised, so the design degrades gracefully
rather than collapsing on one disputed fact.
:::

## Taken together

<div class="table-scroll">

| Constraint | Removes | Forces |
| --- | --- | --- |
| C5 | Free waiting | Waiting becomes a priced decision |
| C8 | Parallel allocation | A sequential commit-observe-replan loop |
| C9 | Cheap experimentation | Option value in the objective, and real stopping rules |

</div>

> One irrevocable, blind, twenty-four-hours-ahead bet at a time. At most four. The mandate is
> the collateral.

**Next:** [The allocator](/system/allocator) — how that gets turned into a decision.
