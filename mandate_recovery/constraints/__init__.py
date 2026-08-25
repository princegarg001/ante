"""The regulatory spine, as code.

The policy proposes; the constraint layer disposes. No ML, no LLM, no I/O, no
wall-clock reads. Every veto names the rule that fired and the source it came
from, so an audit log entry is traceable to a circular.
"""

from .rules import (
    Allow,
    RULES,
    RuleKind,
    Verdict,
    Veto,
    all_vetoes,
    is_permitted,
)

__all__ = [
    "Allow",
    "RULES",
    "RuleKind",
    "Verdict",
    "Veto",
    "all_vetoes",
    "is_permitted",
]
