"""The world simulator.

Every number this project reports is graded against this package, which makes it
the component most worth being suspicious of. Two properties are load-bearing:

**It is harsh.** Calibration targets are drawn from published market statistics
and fixed *before* any policy exists — see `calibrate.py`. If total recovery
comes out above the band, the world is wrong, not the policy, and CI says so.

**Its randomness does not depend on the policy.** Variates are addressed by
`(stream, entity, index)` rather than consumed in call order, so two policies run
on the same seed meet an identical world: the same customers, the same salary
dates, the same issuer outages. That is what makes the paired comparison in the
evaluation harness able to detect a 3% difference at all.
"""

from .customer import LATENT_TYPES, LatentCustomer, LiquidityType
from .issuer import IssuerHealth, IssuerModel
from .rng import RandomTape
from .world import (
    GroundTruth,
    Presentation,
    World,
    WorldConfig,
)

__all__ = [
    "GroundTruth",
    "IssuerHealth",
    "IssuerModel",
    "LATENT_TYPES",
    "LatentCustomer",
    "LiquidityType",
    "Presentation",
    "RandomTape",
    "World",
    "WorldConfig",
]
