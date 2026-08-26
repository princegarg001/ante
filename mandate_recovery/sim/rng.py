"""Addressable randomness — the foundation of common random numbers.

The naive way to seed a simulation is one generator consumed in call order. It
reproduces, and it is useless for comparing policies, because the moment one
policy takes a different action every subsequent draw shifts. Two runs on "the
same seed" then meet different customers, different salary dates and different
outages, and the difference between them is mostly noise.

Here every variate is addressed by `(stream, entity, index)` and derived from the
seed by hashing. Nothing is consumed; nothing shifts. Two policies on seed 42
meet the identical world, so their difference is attributable to the policy
alone — which is what lets the evaluation harness detect an uplift of a few
percent instead of drowning it in between-seed variance.

The world still *reacts* to actions. A customer contacted three times is likelier
to revoke. That is modelled by keeping the uniform variate fixed and moving the
threshold it is compared against, so the reaction is real while the randomness
stays common.
"""

from __future__ import annotations

import hashlib
from typing import Final

import numpy as np

#: Draws are addressed by name. Python's built-in `hash()` is salted per process
#: and would make runs irreproducible across invocations — a bug that hides for
#: exactly as long as you only ever look at one run.
_DIGEST_BYTES: Final[int] = 8


def _stable_hash(name: str) -> int:
    return int.from_bytes(
        hashlib.blake2b(name.encode("utf-8"), digest_size=_DIGEST_BYTES).digest(),
        "big",
    )


class RandomTape:
    """Deterministic, addressable variates derived from one root seed.

    Generators are memoised per `(stream, entity)`, and a stream is normally
    drawn once as a whole array which the caller then indexes. Indexing rather
    than consuming is the point: the draw for day 12 is the same variate whether
    or not the policy did anything on day 3.
    """

    def __init__(self, seed: int) -> None:
        self.seed = int(seed)
        self._cache: dict[tuple[int, int], np.random.Generator] = {}

    def generator(self, stream: str, entity: int = 0) -> np.random.Generator:
        key = (_stable_hash(stream), int(entity))
        gen = self._cache.get(key)
        if gen is None:
            gen = np.random.default_rng(
                np.random.SeedSequence([self.seed, key[0], key[1]])
            )
            self._cache[key] = gen
        return gen

    # -- convenience draws -------------------------------------------------

    def uniform(self, stream: str, entity: int, size: int) -> np.ndarray:
        """`size` uniforms on [0, 1), stable for this (stream, entity)."""
        return self.generator(stream, entity).random(size)

    def choice(self, stream: str, entity: int, n_options: int, p: np.ndarray | None = None) -> int:
        return int(self.generator(stream, entity).choice(n_options, p=p))

    def gamma(self, stream: str, entity: int, shape: float, scale: float, size: int) -> np.ndarray:
        return self.generator(stream, entity).gamma(shape, scale, size)

    def poisson(self, stream: str, entity: int, lam: float, size: int) -> np.ndarray:
        return self.generator(stream, entity).poisson(lam, size)

    def integers(self, stream: str, entity: int, low: int, high: int, size: int = 1) -> np.ndarray:
        return self.generator(stream, entity).integers(low, high, size)

    def spawn(self, suffix: str) -> "RandomTape":
        """A derived tape for an independent concern.

        Used to keep the *evaluation* seed split away from the *world* seed, so
        a held-out seed cannot leak into training through a shared stream.
        """
        return RandomTape(_stable_hash(f"{self.seed}:{suffix}") % (2**63))
