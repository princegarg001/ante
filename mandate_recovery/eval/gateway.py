"""The simulator, behind the payment-gateway interface.

Until now there were two separate paths to the world. The crash demo drove the
write-ahead log, the idempotency keys and the hash-chained receipts; the
evaluation harness called the simulator directly. Both were individually sound
— the constraint layer gated each — but it meant the reported rupees had never
travelled through the audited path, and *"is your measured recovery actually
going through the money path you verified?"* had the answer "no".

This adapter closes that. It presents the `World` as a `PaymentGateway`, so the
evaluation can drive the real `Executor`: intent written and fsynced before any
effect, outcome written after, every decision addressable by its idempotency
key, and the whole run reconstructible with `--replay`.

The `lookup` method is what makes it a real gateway rather than a shim. A
provider that can only be told to do things, and never asked what it already
did, cannot be recovered from — so the adapter keeps its own record of every
effect, exactly as a provider would.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..act.gateway import GatewayResult
from ..core.money import Paise
from ..sim.world import Presentation, World


@dataclass
class WorldGateway:
    """A `PaymentGateway` backed by the simulator."""

    world: World
    #: idem_key -> the effect that was performed, so recovery can interrogate.
    _effects: dict[str, GatewayResult] = field(default_factory=dict)
    #: idem_key -> the simulator outcome, so the harness can score what happened
    #: without going behind the executor's back.
    outcomes: dict[str, Presentation] = field(default_factory=dict)
    _sequence: int = 0

    # -- PaymentGateway ----------------------------------------------------

    def raise_pdn(
        self, idem_key: str, mandate_id: str, execute_at: datetime, amount_paise: Paise
    ) -> GatewayResult:
        if idem_key in self._effects:
            return _replayed(self._effects[idem_key])

        # Deliberately does NOT call `world.notify`. The harness already models
        # the notification implicitly, and calling it here would add a customer
        # contact per commit, raise the revocation hazard, and make an audited
        # run score differently from an unaudited one. The audit layer records
        # the run; it must not change it.
        self._sequence += 1
        accepted = True
        result = GatewayResult(
            ok=accepted,
            idem_key=idem_key,
            external_ref=f"pdn-{self._sequence:07d}",
            sequence_id=f"SEQ-{mandate_id}-{self._sequence:05d}",
            error_code=None if accepted else "mandate_not_live",
            error_description=None if accepted else "mandate is not LIVE",
        )
        self._effects[idem_key] = result
        return result

    def present(
        self, idem_key: str, mandate_id: str, sequence_id: str, amount_paise: Paise
    ) -> GatewayResult:
        if idem_key in self._effects:
            return _replayed(self._effects[idem_key])

        outcome = self.world.present(
            mandate_id, self.world.time_of(self.world.slot_of(_now(self.world))), amount_paise
        )
        result = GatewayResult(
            ok=outcome.ok,
            idem_key=idem_key,
            external_ref=f"pay-{self._sequence:07d}",
            error_code=outcome.error_code,
            error_description=outcome.error_description,
        )
        self._effects[idem_key] = result
        self.outcomes[idem_key] = outcome
        return result

    def lookup(self, idem_key: str) -> GatewayResult | None:
        found = self._effects.get(idem_key)
        return _replayed(found) if found else None

    # -- for the harness ---------------------------------------------------

    def present_at(
        self, idem_key: str, mandate_id: str, when: datetime, amount_paise: Paise
    ) -> GatewayResult:
        """Present at an explicit instant.

        The simulator is time-addressed, so the executor's notion of "now" is not
        enough — the presentation has to land in the slot it was committed to.
        """
        if idem_key in self._effects:
            return _replayed(self._effects[idem_key])

        outcome = self.world.present(mandate_id, when, amount_paise)
        result = GatewayResult(
            ok=outcome.ok,
            idem_key=idem_key,
            external_ref=f"pay-{len(self._effects):07d}",
            error_code=outcome.error_code,
            error_description=outcome.error_description,
        )
        self._effects[idem_key] = result
        self.outcomes[idem_key] = outcome
        return result

    @property
    def effect_count(self) -> int:
        return len(self._effects)


def _replayed(result: GatewayResult) -> GatewayResult:
    return GatewayResult(
        ok=result.ok,
        idem_key=result.idem_key,
        external_ref=result.external_ref,
        sequence_id=result.sequence_id,
        error_code=result.error_code,
        error_description=result.error_description,
        replayed=True,
    )


def _now(world: World) -> datetime:
    return world.origin
