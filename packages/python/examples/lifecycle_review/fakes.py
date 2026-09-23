from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from examples.lifecycle_review.schemas import FinalDecision, ReviewPacket, RiskAssessment
from typeflux import ChatMessage


class LifecycleDemoProvider:
    provider_name = "fake"
    default_model = "fake-lifecycle-review"

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BaseModel:
        if output_schema is RiskAssessment:
            return RiskAssessment(
                case_id="CASE-2026-0101",
                risk_level="high",
                summary="Request is eligible but needs approval because risk notes are present.",
                flags=["manual review"],
            )
        if output_schema is ReviewPacket:
            return ReviewPacket(
                case_id="CASE-2026-0101",
                recommendation="approve_after_review",
                summary="Prepared for human approval before finalization.",
                approval_required=True,
                flags=["manual review"],
            )
        if output_schema is FinalDecision:
            return FinalDecision(
                case_id="CASE-2026-0101",
                decision="approved",
                summary="Human approval was received and the case can proceed.",
                approved=True,
            )
        raise AssertionError(f"unexpected schema: {output_schema}")
