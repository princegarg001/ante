"""The world: a mandate book, and what happens when you present a debit to it.

The agent interacts with this through three calls — `notify`, `present`, and
reading the `MandateState` it is allowed to see. Everything else on this object
is ground truth, used by the evaluation harness to compute the oracle bound and
the realised regret of stopping, and never reachable from the policy.

Design commitments worth arguing with:

**The failure taxonomy is emergent.** Nothing decides "make this one fail with
insufficient funds". A presentation is checked against the mandate's lifecycle,
then the issuer's health at that instant, then the customer's balance. Whichever
gate closes first determines the error code, and the resulting mix of causes is
an output of the world rather than a parameter of it.

**Successful debits change the balance.** The exogenous income and spend path is
fixed by the seed, and collected debits are subtracted on top. That keeps the
underlying randomness invariant to the policy — common random numbers survive —
while still making a successful collection reduce what is available for the next
one.

**A share of the book is unrecoverable by construction.** Closed accounts,
already-revoked mandates, lapsed validity, and customers who intend to leave. If
the stop list is empty at the end of a run, the world was too kind and the
result means nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Final

import numpy as np

from ..core.clock import SLOTS_PER_DAY, SLOT_MINUTES, to_ist
from ..core.money import Paise, rupees
from ..core.types import (
    CauseClass,
    Category,
    MandateState,
    MandateStatus,
)
from ..diagnose.rules import diagnose
from .customer import Population, build_population
from .issuer import ISSUERS, IssuerModel, build_issuers
from .rng import RandomTape


class Doom(Enum):
    """Why a mandate is unrecoverable, for the ones that are born that way."""

    NONE = "NONE"
    ACCOUNT_CLOSED = "ACCOUNT_CLOSED"
    ALREADY_REVOKED = "ALREADY_REVOKED"
    VALIDITY_LAPSED = "VALIDITY_LAPSED"
    INTENDS_TO_CHURN = "INTENDS_TO_CHURN"


#: Error codes as the rails actually emit them, so the diagnosis layer has
#: something real to classify rather than an internal enum wearing a costume.
ERROR_CODES: Final[dict[CauseClass, tuple[str, str]]] = {
    CauseClass.TRANSIENT_ISSUER: ("bank_technical_error", "Beneficiary bank offline"),
    CauseClass.INSUFFICIENT_FUNDS: ("insufficient_funds", "Insufficient balance in account"),
    CauseClass.LIMIT_BREACH: ("limit_exceeded", "Per-transaction limit exceeded"),
    CauseClass.MANDATE_REVOKED: ("mandate_revoked", "Mandate cancelled by customer"),
    CauseClass.MANDATE_EXPIRED: ("mandate_expired", "Mandate validity has lapsed"),
    CauseClass.PDN_MISSING: ("PRE_DEBIT_NOTIFICATION_NOT_FOUND", "No accepted pre-debit notification"),
    CauseClass.TERMINAL: ("account_closed", "Customer account closed or frozen"),
}


@dataclass(frozen=True, slots=True)
class WorldConfig:
    n_mandates: int = 3_000
    days: int = 35
    #: Fraction of the book that cannot be recovered by any policy.
    unrecoverable_share: float = 0.15
    #: COMPLIANCE.md C9. Parameterised because the claim is single-sourced.
    #:
    #: Calibration settled the ambiguity empirically. Under the broad reading —
    #: the first presentation of *every* cycle — a book with a 66% first-attempt
    #: failure rate revokes 58% of its mandates a month, against a market that
    #: reports roughly 20M revocations on 808M executions. The broad reading is
    #: not consistent with the data, so C9 is applied to newly registered
    #: mandates only, and the share of those is a parameter.
    first_failure_revokes: bool = True
    #: Fraction of the book presenting for the first time since registration.
    new_registration_share: float = 0.12
    #: Share of failures that come back with a code carrying no information —
    #: a bank saying "technical decline" and nothing more. Real, and the
    #: reason the diagnosis layer has something to do: without it every code
    #: maps one-to-one onto a cause and classification is free.
    ambiguous_code_share: float = 0.08
    #: Hazard multipliers for customer-initiated revocation.
    revoke_per_failed_debit: float = 0.85
    revoke_per_contact: float = 0.55


@dataclass(frozen=True, slots=True)
class Presentation:
    """The outcome of presenting one debit."""

    mandate_id: str
    at: datetime
    amount_paise: Paise
    ok: bool
    collected_paise: Paise
    cause: CauseClass | None
    error_code: str | None
    error_description: str | None
    #: True when the mandate died as a result of this presentation (C9).
    revoked_mandate: bool = False


@dataclass(slots=True)
class MandateTruth:
    mandate_id: str
    customer: int
    issuer: int
    amount_due: Paise
    max_amount: Paise
    category: Category
    variable_amount_allowed: bool
    due_slot: int
    cycle_end_slot: int
    validity_end_slot: int
    doom: Doom
    #: True while the mandate has never had a successful presentation since
    #: registration. Only these are exposed to C9.
    is_new_registration: bool = False
    status: MandateStatus = MandateStatus.LIVE
    attempts_used: int = 0
    is_first_presentation: bool = True
    contacts_used: int = 0
    collected: Paise = 0
    last_cause: CauseClass = CauseClass.UNKNOWN
    last_error_code: str | None = None
    revoked_slot: int | None = None


@dataclass(slots=True)
class GroundTruth:
    """Everything the evaluation harness may look at and the policy may not."""

    population: Population
    issuers: IssuerModel
    mandates: list[MandateTruth]


class World:
    """A seeded mandate book that can be presented against."""

    def __init__(
        self,
        seed: int,
        config: WorldConfig,
        origin: datetime,
    ) -> None:
        self.seed = seed
        self.config = config
        self.origin = to_ist(origin)
        self.tape = RandomTape(seed)

        self.population = build_population(self.tape, config.n_mandates, config.days)
        self.issuers = build_issuers(self.tape, config.days)
        self.mandates = self._build_mandates()

        self._collected: dict[int, list[tuple[int, Paise]]] = {}
        self._tech_u: dict[int, np.ndarray] = {}
        self._ambiguous_u: dict[int, np.ndarray] = {}
        self._revoke_u: dict[int, np.ndarray] = {}
        self._by_id = {m.mandate_id: m for m in self.mandates}
        self._resolved_days: dict[int, int] = {}

    # -- construction ------------------------------------------------------

    @classmethod
    def generate(
        cls, seed: int, origin: datetime, config: WorldConfig | None = None
    ) -> "World":
        return cls(seed, config or WorldConfig(), origin)

    def _build_mandates(self) -> list[MandateTruth]:
        cfg = self.config
        n = cfg.n_mandates
        gen = self.tape.generator("mandates.book")

        shares = np.array([i.share for i in ISSUERS])
        shares = shares / shares.sum()
        issuer = gen.choice(len(ISSUERS), size=n, p=shares)

        # Subscription prices cluster at familiar price points rather than
        # spreading smoothly, which matters because the amount lever works on
        # the ratio of debit to balance.
        price_points = np.array([149, 199, 249, 299, 399, 499, 599, 799, 999, 1499, 2499])
        weights = np.array([9, 12, 10, 13, 11, 14, 8, 8, 6, 5, 4], dtype=float)
        weights /= weights.sum()
        amount = gen.choice(price_points, size=n, p=weights) * 100

        cat_roll = gen.random(n)
        categories: list[Category] = []
        for r in cat_roll:
            if r < 0.05:
                categories.append(Category.INSURANCE)
            elif r < 0.09:
                categories.append(Category.MF_SIP)
            elif r < 0.11:
                categories.append(Category.CC_BILL)
            else:
                categories.append(Category.STANDARD)

        # Variable-amount mandates permit partial collection (C19). A minority,
        # so the amount lever is reported on the segment where it is legal.
        variable = gen.random(n) < 0.35
        max_amount = (amount * gen.uniform(1.5, 3.0, size=n)).astype(np.int64)

        # Due dates cluster at the start of the month, as subscriptions do.
        due_day = np.clip(gen.choice(np.arange(1, 29), size=n,
                                     p=_due_day_weights()), 1, cfg.days - 8)
        due_slot = (due_day - 1) * SLOTS_PER_DAY + gen.integers(0, SLOTS_PER_DAY, size=n)

        doom = self._assign_doom(gen, n)
        newly_registered = gen.random(n) < cfg.new_registration_share

        mandates: list[MandateTruth] = []
        for i in range(n):
            d = Doom(doom[i])
            validity_slot = int(due_slot[i]) + SLOTS_PER_DAY * int(gen.integers(60, 400))
            if d is Doom.VALIDITY_LAPSED:
                validity_slot = max(0, int(due_slot[i]) - SLOTS_PER_DAY)
            status = (
                MandateStatus.REVOKED if d is Doom.ALREADY_REVOKED
                else MandateStatus.EXPIRED if d is Doom.VALIDITY_LAPSED
                else MandateStatus.LIVE
            )
            mandates.append(
                MandateTruth(
                    mandate_id=f"MND_{i:05d}",
                    customer=i,
                    issuer=int(issuer[i]),
                    amount_due=int(amount[i]),
                    max_amount=int(max(max_amount[i], amount[i])),
                    category=categories[i],
                    variable_amount_allowed=bool(variable[i]),
                    due_slot=int(due_slot[i]),
                    cycle_end_slot=min(
                        self.population.slots - 1, int(due_slot[i]) + 20 * SLOTS_PER_DAY
                    ),
                    validity_end_slot=validity_slot,
                    doom=d,
                    is_new_registration=bool(newly_registered[i]),
                    status=status,
                )
            )
        return mandates

    def _assign_doom(self, gen: np.random.Generator, n: int) -> np.ndarray:
        """Carve out the segment no policy can recover.

        Deliberately not a single bucket: each reason produces a different error
        code and a different correct response, so a system that lumps them
        together will show up in the confusion matrix.
        """
        doom = np.array([Doom.NONE.value] * n, dtype=object)
        roll = gen.random(n)
        share = self.config.unrecoverable_share
        cuts = np.cumsum([0.34, 0.26, 0.18, 0.22]) * share
        doom[roll < cuts[3]] = Doom.INTENDS_TO_CHURN.value
        doom[roll < cuts[2]] = Doom.VALIDITY_LAPSED.value
        doom[roll < cuts[1]] = Doom.ALREADY_REVOKED.value
        doom[roll < cuts[0]] = Doom.ACCOUNT_CLOSED.value
        return doom

    # -- time --------------------------------------------------------------

    def slot_of(self, when: datetime) -> int:
        delta = to_ist(when) - self.origin
        return int(delta.total_seconds() // (SLOT_MINUTES * 60))

    def time_of(self, slot: int) -> datetime:
        return self.origin + timedelta(minutes=SLOT_MINUTES * slot)

    @property
    def horizon_slots(self) -> int:
        return self.population.slots

    # -- what the agent may see -------------------------------------------

    def observable(self, m: MandateTruth) -> MandateState:
        """The agent's view. Contains no latent state, by construction.

        The cause is **inferred** from the error code the rails returned, not
        copied from the simulator's ground truth. Reading `last_cause`
        directly would have been defensible — the modelled codes map cleanly
        onto causes — but it left classification error out of every reported
        number, which is not the same thing as it being zero.
        """
        inferred = diagnose(m.last_error_code)
        return MandateState(
            mandate_id=m.mandate_id,
            status=m.status,
            cause=inferred.cause,
            attempts_used=m.attempts_used,
            is_first_presentation=m.is_first_presentation,
            amount_due_paise=m.amount_due - m.collected,
            max_amount_paise=m.max_amount,
            category=m.category,
            cycle_end=self.time_of(m.cycle_end_slot),
            validity_end=self.time_of(m.validity_end_slot),
            pending_pdn=None,
            contacts_used=m.contacts_used,
            issuer_id=ISSUERS[m.issuer].code,
            variable_amount_allowed=m.variable_amount_allowed,
            last_error_code=m.last_error_code,
        )

    def failed_book(self, at_slot: int) -> list[MandateTruth]:
        """Mandates whose original execution has failed and whose cycle is open —
        the batch the recovery agent is handed."""
        return [
            m
            for m in self.mandates
            if m.due_slot <= at_slot <= m.cycle_end_slot
            and m.collected < m.amount_due
            and m.attempts_used > 0
        ]

    # -- effects -----------------------------------------------------------

    def notify(self, mandate_id: str, at: datetime) -> bool:
        """Record a customer contact. Returns whether the notification was accepted."""
        m = self._by_id[mandate_id]
        m.contacts_used += 1
        if m.status is not MandateStatus.LIVE:
            return False
        return True

    def present(self, mandate_id: str, at: datetime, amount_paise: Paise) -> Presentation:
        """Present a debit and resolve it against the world.

        Gates are checked in the order the rails check them, so whichever closes
        first is the one the customer's bank would actually report.
        """
        m = self._by_id[mandate_id]
        slot = self.slot_of(at)
        self._advance_revocation(m, slot)

        def fail(cause: CauseClass, revoked: bool = False) -> Presentation:
            code, desc = ERROR_CODES[cause]
            # Some failures come back uninformative. Drawn from the addressed
            # tape so the blur is part of the world rather than of the run.
            if self._ambiguous_uniform(m.customer)[slot % self.config.days] < (
                self.config.ambiguous_code_share
            ):
                code, desc = "technical_decline", "Transaction declined by bank"
            m.last_cause = cause
            m.last_error_code = code
            return Presentation(
                mandate_id=mandate_id, at=at, amount_paise=amount_paise, ok=False,
                collected_paise=0, cause=cause, error_code=code,
                error_description=desc, revoked_mandate=revoked,
            )

        # 1. Lifecycle
        if m.doom is Doom.ACCOUNT_CLOSED:
            m.attempts_used += 1
            return fail(CauseClass.TERMINAL)
        if m.status is MandateStatus.REVOKED:
            return fail(CauseClass.MANDATE_REVOKED)
        if m.status is MandateStatus.EXPIRED or slot > m.validity_end_slot:
            m.status = MandateStatus.EXPIRED
            return fail(CauseClass.MANDATE_EXPIRED)

        first = m.is_first_presentation
        m.attempts_used += 1
        m.is_first_presentation = False

        # 2. Issuer health at this instant
        u = self._tech_uniform(m.customer)[slot]
        if u < self.issuers.technical_failure_probability(slot, m.issuer):
            revoked = self._maybe_revoke_on_first_failure(m, first, slot)
            return fail(CauseClass.TRANSIENT_ISSUER, revoked)

        # 3. Per-transaction limit — rare, and different from a balance failure
        if amount_paise > m.max_amount:
            revoked = self._maybe_revoke_on_first_failure(m, first, slot)
            return fail(CauseClass.LIMIT_BREACH, revoked)

        # 4. Balance
        if self.balance_at(m.customer, slot) < amount_paise:
            revoked = self._maybe_revoke_on_first_failure(m, first, slot)
            return fail(CauseClass.INSUFFICIENT_FUNDS, revoked)

        # 5. Collected
        self._collected.setdefault(m.customer, []).append((slot, amount_paise))
        m.collected += amount_paise
        m.last_cause = CauseClass.UNKNOWN
        m.last_error_code = None
        if m.collected >= m.amount_due:
            m.status = MandateStatus.COMPLETED
        return Presentation(
            mandate_id=mandate_id, at=at, amount_paise=amount_paise, ok=True,
            collected_paise=amount_paise, cause=None, error_code=None,
            error_description=None,
        )

    # -- ground truth (evaluation only) ------------------------------------

    def balance_at(self, customer: int, slot: int) -> Paise:
        """Exogenous balance less anything already collected from this customer."""
        slot = int(np.clip(slot, 0, self.population.slots - 1))
        base = float(self.population.exogenous_balance[slot, customer])
        taken = sum(a for s, a in self._collected.get(customer, ()) if s <= slot)
        return int(max(0.0, base - taken))

    def truth(self) -> GroundTruth:
        return GroundTruth(self.population, self.issuers, self.mandates)

    # -- internals ---------------------------------------------------------

    def _ambiguous_uniform(self, entity: int) -> np.ndarray:
        arr = self._ambiguous_u.get(entity)
        if arr is None:
            arr = self.tape.uniform(
                "diagnose.ambiguous", entity, self.config.days + 1
            )
            self._ambiguous_u[entity] = arr
        return arr

    def _tech_uniform(self, entity: int) -> np.ndarray:
        arr = self._tech_u.get(entity)
        if arr is None:
            arr = self.tape.uniform("present.technical", entity, self.population.slots)
            self._tech_u[entity] = arr
        return arr

    def _revoke_uniform(self, entity: int) -> np.ndarray:
        arr = self._revoke_u.get(entity)
        if arr is None:
            arr = self.tape.uniform("revocation.daily", entity, self.config.days + 1)
            self._revoke_u[entity] = arr
        return arr

    def _maybe_revoke_on_first_failure(
        self, m: MandateTruth, was_first: bool, slot: int
    ) -> bool:
        """COMPLIANCE.md C9 — a failed first presentation kills the mandate.

        Scoped to newly registered mandates. See `WorldConfig` for why the broad
        reading was rejected on evidence rather than on preference.
        """
        if was_first and m.is_new_registration and self.config.first_failure_revokes:
            m.status = MandateStatus.REVOKED
            m.revoked_slot = slot
            return True
        return False

    def _advance_revocation(self, m: MandateTruth, slot: int) -> None:
        """Customer-initiated revocation, as a daily hazard.

        The uniform for a given customer-day is fixed; what the agent's behaviour
        moves is the threshold. That keeps the reaction genuine while leaving the
        randomness common across policies.
        """
        if m.status is not MandateStatus.LIVE:
            return
        day = min(slot // SLOTS_PER_DAY, self.config.days)
        start = self._resolved_days.get(m.customer, 0)
        if day < start:
            return
        u = self._revoke_uniform(m.customer)
        cfg = self.config
        base = float(self.population.churn_intent[m.customer]) * 0.004 + 0.0008
        for d in range(start, day + 1):
            hazard = base * (
                1.0
                + cfg.revoke_per_failed_debit * m.attempts_used
                + cfg.revoke_per_contact * m.contacts_used
            )
            if m.doom is Doom.INTENDS_TO_CHURN:
                hazard *= 9.0
            if u[d] < min(hazard, 0.9):
                m.status = MandateStatus.REVOKED
                m.revoked_slot = d * SLOTS_PER_DAY
                break
        self._resolved_days[m.customer] = day + 1


def _due_day_weights() -> np.ndarray:
    """Subscription billing clusters at the start of the month."""
    days = np.arange(1, 29)
    w = np.exp(-0.09 * (days - 1)) + 0.25
    w[np.isin(days, [1, 2, 3, 5, 10, 15, 20, 25])] *= 1.6
    return w / w.sum()
