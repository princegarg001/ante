"""The allocator.

Everything else in this repository exists to make this component's number
believable. It decides, for a batch of failed mandates competing for a scarce
and regulated supply of execution slots, which mandates get one, when, for how
much, and which get nothing.

Three things separate it from the greedy baseline it has to beat:

**Option value.** A failed presentation raises the chance the customer revokes,
and a revoked mandate costs every future cycle rather than this one. The
objective therefore carries a term for the mandate surviving, which is what
makes stopping worth money instead of merely being safe.

**A budget, not a schedule.** Four presentations exist per mandate per cycle and
they are not fungible with time. The value of spending one now is compared
against the value of holding it, by backward induction over the remaining cycle.

**A price.** Execution windows are shared and throttled, so mandates are not
independent. The capacity constraint is relaxed into a multiplier per window,
which turns into the rupee price of a slot — and every decision becomes a bid
against a clearing price rather than an opinion.
"""

from .allocator import (
    AllocatorConfig,
    ClearingPrice,
    SlotAllocator,
)

__all__ = ["AllocatorConfig", "ClearingPrice", "SlotAllocator"]
