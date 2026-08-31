"""Turning what the rails said into what it means.

The policy needs a *cause*: is this mandate worth another attempt, does it need
re-registration, or is it dead. What the rails actually return is an error code,
and sometimes a line of free text from a bank that has its own opinions about
formatting.

Until this package existed the agent read the simulator's true cause directly.
That was defensible — the modelled codes map one-to-one onto causes, so a lookup
would have produced the same answer — but it meant classification error was
absent from every reported number, and a reviewer would be right to ask.

Now the agent sees an error code and infers. The inference is a deterministic
rule table with a **one-way ratchet**: a rule may move a cause *into* the
terminal set, and nothing may move one out. That constraint is what makes it
safe to put a language model on top later — it can only ever make the system
more cautious, never less.
"""

from .rules import (
    AMBIGUOUS_CODES,
    Diagnosis,
    diagnose,
    confusion_matrix,
)

__all__ = ["AMBIGUOUS_CODES", "Diagnosis", "confusion_matrix", "diagnose"]
