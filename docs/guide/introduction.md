# Introduction

Ante is a recovery agent for failed UPI Autopay and e-mandate debits, built for Track 03 of
the Razorpay AI Buildathon. It decides which failed mandates deserve one of a strictly limited
number of retry attempts, when to spend them, and — the part that matters most — when to stop.

## The bar for this track

Razorpay states it plainly:

> "Don't just identify the problem. Show measured money recovered across a batch, with
> compliant escalation, stopping rules, and an audit trail."

Four requirements, and three of them are about restraint rather than recovery. That shapes
everything in this design: compliance is a structural property of the system rather than a
report generated at the end, stopping is a first-class decision with its own accounting, and
every action leaves a replayable record.

## What makes this problem different

Recovery products built for card rails in the US and Europe solve a timing problem. They ask:
*given that this payment failed, when is it most likely to succeed if I try again?* Retries are
cheap, so you take many of them and let a model pick good moments.

Indian regulation removes every assumption that argument rests on.

<div class="table-scroll">

| Assumption elsewhere | Reality on Indian rails |
| --- | --- |
| Retries are effectively unlimited | One execution plus **three** retries per mandate per cycle |
| Retry any time of day | Execution barred 10:00–13:00 and 17:00–21:30 IST |
| Decide and execute together | Debit must be notified **24–48 hours in advance**, then executed exactly as notified |
| Schedule several attempts ahead | Only **one** notification may be pending per mandate at a time |
| A failed retry costs one retry | A failed **first** presentation revokes the mandate entirely |

</div>

The consequence is that the question "when should I retry?" is not available. By the time you
are allowed to act, you have already had to commit, and you cannot revise. What remains is an
allocation problem under scarcity, and a decision about whether a given mandate is worth a
bet at all.

## The name

An *ante* is the forced bet you must place before you are permitted to see anything. It is also
the Latin prefix for *before* — which is precisely what a pre-debit notification is.

Both meanings are load-bearing. Every retry in this system is a stake committed 24 hours
blind, non-refundable, capped at four per cycle, with the mandate itself as collateral.

## How to read these docs

<div class="table-scroll">

| If you want | Read |
| --- | --- |
| Why Western retry playbooks fail here | [The problem](/guide/problem) and [Prior art](/guide/prior-art) |
| Exactly which rules bind, and their sources | [Constraint register](/constraints/) |
| The three constraints that change the algorithm | [The three that matter](/constraints/critical) |
| How allocation actually works | [The allocator](/system/allocator) |
| Why the compliance claim is credible | [Verification](/engineering/verification) |
| What is built versus designed | [Status & roadmap](/project/roadmap) |

</div>

::: warning Verification status
Every regulatory constraint documented here is currently sourced from secondary material —
law-firm notes, PSP developer documentation and press reporting — and is marked `SECONDARY`
in the [constraint register](/constraints/). None has yet been confirmed against the NPCI or
RBI circular itself. Three of them may turn out to be PSP convention rather than regulation.
[Verification status](/constraints/sources) tracks the work.
:::
