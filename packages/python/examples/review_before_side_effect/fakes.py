from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from examples.review_before_side_effect.schemas import (
    Assessment,
    Disbursement,
    Reversal,
)
from typeflux import ChatMessage


def _idempotency_key(messages: list[ChatMessage]) -> str:
    """A stable key for one external request (the rendered prompt is claim-scoped and
    deterministic), so a retry of the same disbursement hashes to the same key."""
    return repr(tuple(messages))


class ReviewBeforeSideEffectProvider:
    """A scripted provider (no API key). The disbursement is a GENUINELY idempotent
    external write: a dedup ledger keyed on the request means a retry returns the original
    transfer and performs NO second payment. ``transfers_performed`` counts the real
    effects, so a test can prove a repeated disburse is a no-op."""

    provider_name = "fake"
    default_model = "fake-review-before-side-effect"

    def __init__(self) -> None:
        self._ledger: dict[str, Disbursement] = {}
        #: Count of REAL disbursements performed — a duplicate request does not increment it.
        self.transfers_performed = 0

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BaseModel:
        if output_schema is Assessment:
            return Assessment(
                claim_id="CLAIM-1",
                recommended=True,
                rationale="Documentation complete; eligible for disbursement after approval.",
            )
        if output_schema is Disbursement:
            key = _idempotency_key(messages)
            if key not in self._ledger:
                self.transfers_performed += 1
                self._ledger[key] = Disbursement(
                    claim_id="CLAIM-1",
                    transfer_id="XFER-1",
                    idempotency_key="idem-CLAIM-1",
                    amount=500.0,
                )
            return self._ledger[key]
        if output_schema is Reversal:
            return Reversal(claim_id="CLAIM-1", reversed=True)
        raise AssertionError(f"unexpected schema: {output_schema}")
