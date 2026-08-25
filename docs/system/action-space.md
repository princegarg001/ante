# Action space

Eight actions. Defined in `mandate_recovery/core/types.py`, all frozen, all gated by the
constraint layer before they can reach the action layer.

The breadth matters: a system whose only choices are *retry* and *don't retry* is a scheduler.
Being able to lower the amount, spend a free notification instead of a slot, escalate, or
deliberately wait is what makes this an agent.

## The actions

<div class="table-scroll">

| Action | Spends | Meaning |
| --- | --- | --- |
| `Commit(execute_at, amount)` | a retry slot | Raise a pre-debit notification now for an execution at a fixed time and amount |
| `CancelPending()` | nothing | Withdraw the in-flight commitment, freeing the mandate to be re-planned |
| `NotifyOnly(at, template)` | a contact | A dunning message that is not a debit |
| `RequestAFA()` | a contact | Escalate to customer authentication |
| `RequestRemandate()` | a contact | Mandate is dead; ask the customer to re-register |
| `EscalateHuman(summary)` | a handoff | Pass to a collections agent with generated context |
| `Stop(reason)` | nothing | Refuse to spend further this cycle |
| `Wait()` | nothing | Hold the aperture open and buy information |

</div>

## `Commit` — the only action that can move money

```python
@dataclass(frozen=True, slots=True)
class Commit:
    execute_at: datetime
    amount_paise: Paise
```

Both fields are decision variables. Most competing designs optimise only the first; see
[the amount lever](/system/allocator#the-amount-lever) for why the second is worth as much.

A `Commit` must satisfy, simultaneously:

<div class="table-scroll">

| Requirement | Rule |
| --- | --- |
| Execution outside peak hours | <span class="rule reg">C2</span> |
| Lead time within `[24h, 48h]` | <span class="rule reg">C5</span> |
| Not raised at/after 23:50 for a T+1 execution | <span class="rule reg">C7</span> |
| No commitment already pending | <span class="rule reg">C8</span> |
| Mandate is `LIVE` | <span class="rule reg">C12</span> |
| Amount at or under the AFA-free ceiling | <span class="rule reg">C15</span> |
| Amount at or under the authorised cap | <span class="rule reg">C19</span> |
| Execution inside the validity period | <span class="rule reg">C21</span> |
| Cause is not terminal | <span class="rule reg">RATCHET</span> |
| On the slot grid, sane amount, before cycle end | <span class="rule ops">OPS-*</span> |

</div>

## Three design decisions worth arguing with

### `Wait` is an action, not an absence of one

Under a one-sided notification rule, waiting would be free — the option to execute later
survives. Under the two-sided aperture ([C5](/constraints/critical#c5-the-commit-aperture-is-two-sided))
the set of reachable execution times slides forward continuously, so declining to commit today
destroys options and is therefore a real decision.

Making it explicit means it appears in the audit log as something the agent chose, with its
justification, rather than as a gap in the record.

### `CancelPending` must be explicit

[C8](/constraints/critical#c8-commitments-are-serialized) means that raising a new
notification silently cancels the previous one. The system could exploit that implicitly —
just issue a better commitment and let the rails clean up.

It does not. `Commit` is vetoed while anything is pending, so re-planning requires an explicit
cancel first. The cost of abandoning a bet therefore appears in the audit log as a decision
that was taken, instead of disappearing into a silent overwrite.

```python
def test_c8_requires_an_explicit_cancel_first(pending_pdn):
    s = make_state(pending_pdn=pending_pdn)
    assert is_permitted(CancelPending(), s, ORIGIN).allowed
    cleared = s.with_(pending_pdn=None)
    assert is_permitted(commit(24), cleared, ORIGIN).allowed
```

### `Stop` and `Wait` are always available

The agent must never be cornered into spending. Refusing is legal from every state, and this
is asserted directly:

```python
def test_stop_and_wait_are_always_available(state):
    assert is_permitted(Stop(reason="terminal cause"), state, ORIGIN).allowed
    assert is_permitted(Wait(), state, ORIGIN).allowed
```

## The notification is an instrument, not overhead

The pre-debit notification is mandatory ([C6](/constraints/)) and cannot be charged for
([C20](/constraints/)). It is therefore a **free contact with the customer that the regulation
compels you to make anyway**.

A well-timed, well-worded notice raises the probability that the customer tops up before the
debit — which means notification copy and timing belong in the action space rather than in a
side effect. `NotifyOnly` exists as a separate action precisely so the agent can spend a
contact without spending a retry slot.

::: warning And the guard that goes with it
An agent rewarded on recovery alone will discover that contacting people more often raises
recovery. `OPS-CONTACT` caps contacts per customer per cycle, independently of the retry cap,
and it is one of the attacks in the planned red-team suite.
:::

## Verdicts

Every action is evaluated by a pure function:

```python
def is_permitted(action: Action, state: MandateState, clock: datetime) -> Allow | Veto
```

`Veto` carries the rule that fired, a human-readable reason, and whether the rule is regulatory
or operational:

```
[C5] lead 12h00m is under the 24h pre-debit notification minimum
[C8] a PDN for 2026-09-02 06:30 IST is already pending; cancel it explicitly first
[C2] execution at 2026-09-02 10:00 IST falls in an NPCI peak window
```

There is also `all_vetoes()`, which returns every rule that fired rather than the first.
`is_permitted` is the gate; `all_vetoes` is what the audit log records, so a reviewer can see
that an action was illegal for four reasons rather than one.

**Next:** [The money path](/system/action-layer).
