"""Issuer health — correlated outages, not independent noise.

If technical failures were IID, a retry a few hours later would be an almost
free draw from an independent coin, and any policy that spreads attempts would
look good for a reason that does not exist. Real outages arrive as incidents:
they start, they persist for a while, and everything presented to that bank
during the window fails together.

Modelled as a marked Poisson process of incidents per issuer, plus a rarer
**system-wide** incident that degrades every issuer at once. The second one
matters because it is the case a diversify-across-issuers strategy cannot help
with, and its absence would flatter any policy that tried.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Final

import numpy as np

from ..core.clock import SLOTS_PER_DAY
from .rng import RandomTape


class IssuerHealth(IntEnum):
    UP = 0
    DEGRADED = 1
    DOWN = 2


@dataclass(frozen=True, slots=True)
class Issuer:
    code: str
    share: float
    #: Probability a presentation fails technically while the issuer is UP.
    base_tech_fail: float
    incidents_per_day: float
    incident_mean_minutes: float
    degradations_per_day: float
    degradation_mean_minutes: float


#: Shares and reliability differ by bank. The largest remitter is not the most
#: reliable, which is part of why headline approval rates look the way they do.
ISSUERS: Final[tuple[Issuer, ...]] = (
    Issuer("SBIN", 0.26, 0.055, 0.11, 105, 0.55, 55),
    Issuer("HDFC", 0.17, 0.022, 0.06, 70, 0.30, 40),
    Issuer("ICIC", 0.14, 0.026, 0.07, 75, 0.34, 42),
    Issuer("UTIB", 0.11, 0.030, 0.08, 80, 0.38, 45),
    Issuer("KKBK", 0.09, 0.028, 0.07, 72, 0.33, 40),
    Issuer("PUNB", 0.09, 0.048, 0.10, 95, 0.50, 52),
    Issuer("BARB", 0.08, 0.044, 0.09, 90, 0.46, 50),
    Issuer("YESB", 0.06, 0.038, 0.09, 85, 0.42, 48),
)

SYSTEM_INCIDENTS_PER_DAY: Final[float] = 0.035
SYSTEM_INCIDENT_MEAN_MINUTES: Final[float] = 65.0


@dataclass(slots=True)
class IssuerModel:
    """Per-slot health for every issuer, plus the system-wide overlay."""

    health: np.ndarray              # (slots, n_issuers) of IssuerHealth
    system_down: np.ndarray         # (slots,) bool
    codes: tuple[str, ...]

    def code_index(self, code: str) -> int:
        return self.codes.index(code)

    def state_at(self, slot: int, issuer: int) -> IssuerHealth:
        if self.system_down[slot]:
            return IssuerHealth.DOWN
        return IssuerHealth(int(self.health[slot, issuer]))

    def technical_failure_probability(self, slot: int, issuer: int) -> float:
        """Probability a presentation fails for technical reasons right now."""
        state = self.state_at(slot, issuer)
        if state is IssuerHealth.DOWN:
            return 1.0
        base = ISSUERS[issuer].base_tech_fail
        return min(1.0, base * (4.0 if state is IssuerHealth.DEGRADED else 1.0))

    def uptime_fraction(self) -> float:
        combined = self.health.copy()
        combined[self.system_down] = IssuerHealth.DOWN
        return float((combined == IssuerHealth.UP).mean())


def build_issuers(tape: RandomTape, days: int) -> IssuerModel:
    slots = days * SLOTS_PER_DAY
    health = np.zeros((slots, len(ISSUERS)), dtype=np.int8)

    for i, issuer in enumerate(ISSUERS):
        gen = tape.generator("issuer.incidents", i)
        _paint(gen, health[:, i], slots, days,
               issuer.degradations_per_day, issuer.degradation_mean_minutes,
               IssuerHealth.DEGRADED)
        _paint(gen, health[:, i], slots, days,
               issuer.incidents_per_day, issuer.incident_mean_minutes,
               IssuerHealth.DOWN)

    system_down = np.zeros(slots, dtype=bool)
    sys_gen = tape.generator("issuer.system")
    marker = np.zeros(slots, dtype=np.int8)
    _paint(sys_gen, marker, slots, days,
           SYSTEM_INCIDENTS_PER_DAY, SYSTEM_INCIDENT_MEAN_MINUTES, IssuerHealth.DOWN)
    system_down[marker == IssuerHealth.DOWN] = True

    return IssuerModel(
        health=health,
        system_down=system_down,
        codes=tuple(x.code for x in ISSUERS),
    )


def _paint(
    gen: np.random.Generator,
    lane: np.ndarray,
    slots: int,
    days: int,
    rate_per_day: float,
    mean_minutes: float,
    state: IssuerHealth,
) -> None:
    """Lay incidents down as intervals so failures inside one are correlated."""
    n_incidents = gen.poisson(rate_per_day * days)
    if n_incidents == 0:
        return
    starts = gen.integers(0, slots, size=n_incidents)
    # Gamma durations: mostly short, with a tail that occasionally spans hours.
    minutes = gen.gamma(1.8, mean_minutes / 1.8, size=n_incidents)
    lengths = np.maximum(1, np.round(minutes / 30.0)).astype(int)
    for start, length in zip(starts, lengths):
        end = min(slots, start + length)
        lane[start:end] = np.maximum(lane[start:end], int(state))
