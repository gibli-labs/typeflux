from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from examples.insurance_claim_review.schemas import (
    ClaimReviewPacket,
    EvidenceReview,
)
from typeflux.core.contracts import ChatMessage


class FakeProvider:
    provider_name = "fake"
    default_model = "fake-insurance-model"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._review_index = 0

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BaseModel:
        self.calls.append(
            {
                "messages": messages,
                "output_schema": output_schema,
                "model": model,
                "temperature": temperature,
                "metadata": metadata,
            }
        )
        if output_schema is EvidenceReview:
            self._review_index += 1
            return EvidenceReview(
                claim_id="CLM-2026-0042",
                evidence_id=f"pending-{self._review_index}",
                decision="needs_follow_up" if self._review_index == 2 else "support",
                relevance_score=0.9 if self._review_index != 3 else 0.62,
                risk_signals=["duplicate invoice"] if self._review_index == 2 else [],
                missing_context=["repair shop estimate"] if self._review_index == 2 else [],
                follow_up_questions=(
                    ["Confirm whether invoice INV-8842 was paid twice."]
                    if self._review_index == 2
                    else []
                ),
                summary=f"Evidence item {self._review_index} supports the claimed loss.",
            )
        if output_schema is ClaimReviewPacket:
            return ClaimReviewPacket(
                claim_id="CLM-2026-0042",
                recommendation="investigate",
                confidence=0.78,
                summary="Claim has supporting evidence, but duplicate billing needs adjuster review.",
                risk_signals=["duplicate invoice"],
                required_follow_up=["Confirm whether invoice INV-8842 was paid twice."],
                evidence_count=4,
                approval_required=True,
            )
        raise AssertionError(f"unexpected schema: {output_schema}")
