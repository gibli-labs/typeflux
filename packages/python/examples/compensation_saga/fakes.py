from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from examples.compensation_saga.schemas import (
    Booking,
    CancelResult,
    ChargeResult,
    Fulfillment,
    RefundResult,
)
from typeflux import ChatMessage


def _idempotency_key(messages: list[ChatMessage]) -> str:
    """A stable key for one external request. The rendered prompt threads the caller's
    idempotency_key (e.g. ``Charge order ORDER-1 with key idem-ORDER-1.``), so identical
    requests — a retry, or a best-effort compensation retry — hash to the same key."""
    return repr(tuple(messages))


class CompensationSagaProvider:
    """A scripted provider (no API key) that models a GENUINELY idempotent external
    system: ``book_room`` and ``charge_card`` dedup on the caller's idempotency key
    (carried in the rendered prompt), so a repeated request returns the original result
    and performs NO second side effect. ``external_writes`` counts the real effects that
    actually happened, so a test can prove a retry is a no-op."""

    provider_name = "fake"
    default_model = "fake-compensation-saga"

    def __init__(self) -> None:
        self._booking_ledger: dict[str, Booking] = {}
        self._charge_ledger: dict[str, ChargeResult] = {}
        #: Count of REAL external effects performed, per activity — a duplicate request
        #: does not increment it (that is the point of the idempotency key).
        self.external_writes: dict[str, int] = {"book_room": 0, "charge_card": 0}

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BaseModel:
        if output_schema is Booking:
            key = _idempotency_key(messages)
            if key not in self._booking_ledger:
                self.external_writes["book_room"] += 1
                self._booking_ledger[key] = Booking(
                    order_id="ORDER-1", confirmation_id="CONF-1", idempotency_key="idem-ORDER-1"
                )
            return self._booking_ledger[key]
        if output_schema is ChargeResult:
            key = _idempotency_key(messages)
            if key not in self._charge_ledger:
                self.external_writes["charge_card"] += 1
                self._charge_ledger[key] = ChargeResult(
                    order_id="ORDER-1",
                    charge_id="CHG-1",
                    idempotency_key="idem-ORDER-1",
                    amount=100.0,
                )
            return self._charge_ledger[key]
        if output_schema is CancelResult:
            return CancelResult(order_id="ORDER-1", cancelled=True)
        if output_schema is RefundResult:
            return RefundResult(order_id="ORDER-1", refunded=True)
        if output_schema is Fulfillment:
            return Fulfillment(order_id="ORDER-1", fulfilled=True)
        raise AssertionError(f"unexpected schema: {output_schema}")
