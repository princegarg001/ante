"""The effect boundary — the only place the system touches the outside world.

One requirement dominates the design of this interface: **every effect must be
addressable by its idempotency key after the fact.** A gateway that can only be
told to do things, and never asked what it has already done, cannot be recovered
from safely. After a crash in the in-doubt window the caller has exactly two
options — ask, or guess — and guessing here means either a double debit or a
cancelled pre-debit notification.

That is not a hypothetical on these rails. Under COMPLIANCE.md C8, raising a
second notification for a mandate *cancels the first*. So the naive recovery
strategy — "retry anything that did not complete" — silently pushes the
execution out by a day and burns the aperture. `lookup()` exists so that never
has to happen.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..core.money import Paise


class CrashInjected(RuntimeError):
    """Raised by `FakeGateway` to simulate a process dying at a chosen instant.

    Always raised *after* the effect has been recorded internally, because the
    dangerous window is precisely the one where the effect landed and the caller
    never found out.
    """


@dataclass(frozen=True, slots=True)
class GatewayResult:
    ok: bool
    idem_key: str
    external_ref: str | None = None
    #: NPCI presentation sequence id, needed to present against this notification.
    sequence_id: str | None = None
    error_code: str | None = None
    error_description: str | None = None
    #: True when replayed from the gateway's own record rather than freshly performed.
    replayed: bool = False


@runtime_checkable
class PaymentGateway(Protocol):
    """What the executor needs from a payments provider."""

    def raise_pdn(
        self,
        idem_key: str,
        mandate_id: str,
        execute_at: datetime,
        amount_paise: Paise,
    ) -> GatewayResult:
        """Raise a pre-debit notification. Irrevocable, and cancels any pending
        notification for the same mandate (C8)."""
        ...

    def present(
        self,
        idem_key: str,
        mandate_id: str,
        sequence_id: str,
        amount_paise: Paise,
    ) -> GatewayResult:
        """Present the debit. Requires an accepted notification (C6)."""
        ...

    def lookup(self, idem_key: str) -> GatewayResult | None:
        """Return a previously performed effect, or None if it never happened.

        The method that makes crash recovery possible.
        """
        ...


# --------------------------------------------------------------------------- #
# Dry run — the default
# --------------------------------------------------------------------------- #


class DryRunGateway:
    """Performs nothing. Records what it was asked to do.

    The default mode, so that running the system by accident cannot move money.
    Its results are tagged in the journal, so a replay can never be confused
    about whether a run was real.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def raise_pdn(
        self, idem_key: str, mandate_id: str, execute_at: datetime, amount_paise: Paise
    ) -> GatewayResult:
        self.calls.append(("raise_pdn", idem_key, mandate_id))
        return GatewayResult(
            ok=True,
            idem_key=idem_key,
            external_ref=f"dryrun-pdn-{idem_key[:12]}",
            sequence_id=f"DRY-{idem_key[:8]}-0001",
        )

    def present(
        self, idem_key: str, mandate_id: str, sequence_id: str, amount_paise: Paise
    ) -> GatewayResult:
        self.calls.append(("present", idem_key, mandate_id))
        return GatewayResult(
            ok=True, idem_key=idem_key, external_ref=f"dryrun-pay-{idem_key[:12]}"
        )

    def lookup(self, idem_key: str) -> GatewayResult | None:
        return None


# --------------------------------------------------------------------------- #
# Test double
# --------------------------------------------------------------------------- #


@dataclass
class FakeGateway:
    """An in-memory gateway that models the parts of the rails that bite.

    Specifically it models C8: raising a notification for a mandate that already
    has one pending cancels the previous one, and the cancellation is recorded.
    Tests assert that `cancelled_sequence_ids` stays empty through a crash and
    restart — which is the property naive recovery logic breaks.
    """

    #: Idempotency keys at which `raise_pdn` should die after recording the effect.
    crash_on: set[str] = field(default_factory=set)
    #: Idempotency keys whose presentation should decline.
    decline_on: set[str] = field(default_factory=set)

    _effects: dict[str, GatewayResult] = field(default_factory=dict, init=False)
    _pending_by_mandate: dict[str, str] = field(default_factory=dict, init=False)
    cancelled_sequence_ids: list[str] = field(default_factory=list, init=False)
    raise_calls: int = field(default=0, init=False)
    present_calls: int = field(default=0, init=False)

    def raise_pdn(
        self, idem_key: str, mandate_id: str, execute_at: datetime, amount_paise: Paise
    ) -> GatewayResult:
        if idem_key in self._effects:
            return _as_replayed(self._effects[idem_key])

        self.raise_calls += 1

        # C8: a new notification cancels whatever was pending for this mandate.
        previous = self._pending_by_mandate.get(mandate_id)
        if previous is not None:
            self.cancelled_sequence_ids.append(previous)

        sequence_id = f"SEQ-{mandate_id}-{self.raise_calls:04d}"
        result = GatewayResult(
            ok=True,
            idem_key=idem_key,
            external_ref=f"pdn-{self.raise_calls:06d}",
            sequence_id=sequence_id,
        )
        # Recorded before the crash, because the whole point of the in-doubt
        # window is that the effect happened and the caller never learned it.
        self._effects[idem_key] = result
        self._pending_by_mandate[mandate_id] = sequence_id

        if idem_key in self.crash_on:
            raise CrashInjected(f"gateway died after raising PDN for {mandate_id}")
        return result

    def present(
        self, idem_key: str, mandate_id: str, sequence_id: str, amount_paise: Paise
    ) -> GatewayResult:
        if idem_key in self._effects:
            return _as_replayed(self._effects[idem_key])

        self.present_calls += 1
        if self._pending_by_mandate.get(mandate_id) != sequence_id:
            # C6: presenting without a live notification is rejected outright.
            result = GatewayResult(
                ok=False,
                idem_key=idem_key,
                error_code="PRE_DEBIT_NOTIFICATION_NOT_FOUND",
                error_description="no accepted notification for this presentation",
            )
        elif idem_key in self.decline_on:
            result = GatewayResult(
                ok=False,
                idem_key=idem_key,
                error_code="insufficient_funds",
                error_description="customer account did not have enough funds",
            )
        else:
            result = GatewayResult(
                ok=True, idem_key=idem_key, external_ref=f"pay-{self.present_calls:06d}"
            )

        self._effects[idem_key] = result
        self._pending_by_mandate.pop(mandate_id, None)

        if idem_key in self.crash_on:
            raise CrashInjected(f"gateway died after presenting for {mandate_id}")
        return result

    def lookup(self, idem_key: str) -> GatewayResult | None:
        found = self._effects.get(idem_key)
        return _as_replayed(found) if found else None

    # -- assertions for tests ---------------------------------------------

    @property
    def effect_count(self) -> int:
        return len(self._effects)

    def pending_for(self, mandate_id: str) -> str | None:
        return self._pending_by_mandate.get(mandate_id)


# --------------------------------------------------------------------------- #
# Durable stand-in, for demonstrating recovery across a real process death
# --------------------------------------------------------------------------- #


class FileGateway:
    """`FakeGateway` semantics, persisted to disk.

    An in-memory double cannot demonstrate crash recovery, because it dies with
    the process that was supposed to crash. This one keeps its records in a file
    the way a real provider keeps them on their side of the network, so a
    restarted process can genuinely ask "did you already do this?" and get an
    answer that predates its own lifetime.

    `pause_after_key` stops the process *inside the in-doubt window* — the effect
    is durably recorded here, and the caller has not yet written its outcome.
    That is the precise instant the crash demo needs to interrupt.
    """

    def __init__(self, path: str | Path, pause_after_key: str | None = None) -> None:
        self.path = Path(path)
        self.pause_after_key = pause_after_key
        self._state = self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> dict:
        if not self.path.exists():
            return {
                "effects": {},
                "pending": {},
                "cancelled": [],
                "raise_calls": 0,
                "present_calls": 0,
            }
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._state, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    # -- operations --------------------------------------------------------

    def raise_pdn(
        self, idem_key: str, mandate_id: str, execute_at: datetime, amount_paise: Paise
    ) -> GatewayResult:
        if idem_key in self._state["effects"]:
            return _as_replayed(_from_obj(self._state["effects"][idem_key]))

        self._state["raise_calls"] += 1
        previous = self._state["pending"].get(mandate_id)
        if previous is not None:
            self._state["cancelled"].append(previous)      # C8

        sequence_id = f"SEQ-{mandate_id}-{self._state['raise_calls']:04d}"
        obj = {
            "ok": True,
            "idem_key": idem_key,
            "external_ref": f"pdn-{self._state['raise_calls']:06d}",
            "sequence_id": sequence_id,
            "error_code": None,
            "error_description": None,
        }
        self._state["effects"][idem_key] = obj
        self._state["pending"][mandate_id] = sequence_id
        self._save()

        if self.pause_after_key is not None and idem_key == self.pause_after_key:
            self._signal_ready_to_be_killed()

        return _from_obj(obj)

    def present(
        self, idem_key: str, mandate_id: str, sequence_id: str, amount_paise: Paise
    ) -> GatewayResult:
        if idem_key in self._state["effects"]:
            return _as_replayed(_from_obj(self._state["effects"][idem_key]))
        self._state["present_calls"] += 1
        ok = self._state["pending"].get(mandate_id) == sequence_id
        obj = {
            "ok": ok,
            "idem_key": idem_key,
            "external_ref": f"pay-{self._state['present_calls']:06d}" if ok else None,
            "sequence_id": None,
            "error_code": None if ok else "PRE_DEBIT_NOTIFICATION_NOT_FOUND",
            "error_description": None if ok else "no accepted notification",
        }
        self._state["effects"][idem_key] = obj
        self._state["pending"].pop(mandate_id, None)
        self._save()
        return _from_obj(obj)

    def lookup(self, idem_key: str) -> GatewayResult | None:
        self._state = self._load()          # another process may have written
        found = self._state["effects"].get(idem_key)
        return _as_replayed(_from_obj(found)) if found else None

    # -- inspection --------------------------------------------------------

    @property
    def raise_calls(self) -> int:
        return self._load()["raise_calls"]

    @property
    def cancelled_sequence_ids(self) -> list[str]:
        return self._load()["cancelled"]

    def pending_for(self, mandate_id: str) -> str | None:
        return self._load()["pending"].get(mandate_id)

    def _signal_ready_to_be_killed(self) -> None:
        """Park inside the in-doubt window until something kills this process."""
        marker = self.path.parent / "READY_TO_KILL"
        marker.write_text("in the in-doubt window\n", encoding="utf-8")
        while True:                          # the parent sends SIGKILL here
            time.sleep(0.05)


def _from_obj(obj: dict) -> GatewayResult:
    return GatewayResult(
        ok=bool(obj["ok"]),
        idem_key=obj["idem_key"],
        external_ref=obj.get("external_ref"),
        sequence_id=obj.get("sequence_id"),
        error_code=obj.get("error_code"),
        error_description=obj.get("error_description"),
    )


def _as_replayed(result: GatewayResult) -> GatewayResult:
    return GatewayResult(
        ok=result.ok,
        idem_key=result.idem_key,
        external_ref=result.external_ref,
        sequence_id=result.sequence_id,
        error_code=result.error_code,
        error_description=result.error_description,
        replayed=True,
    )
