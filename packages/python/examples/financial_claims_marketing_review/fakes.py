from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from examples.financial_claims_marketing_review.schemas import (
    ClaimComplianceReview,
    MarketingReviewPacket,
)
from typeflux.core import ChatMessage


class FakeProvider:
    provider_name = "fake"
    default_model = "fake-financial-compliance-model"

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
        if output_schema is ClaimComplianceReview:
            self._review_index += 1
            return _fake_claim_review(self._review_index)
        if output_schema is MarketingReviewPacket:
            return MarketingReviewPacket(
                submission_id="MKT-2026-RET-0042",
                final_decision="legal_review_required",
                summary=(
                    "The submission includes high-risk performance and regulator endorsement "
                    "claims that cannot be approved as written."
                ),
                approved_claim_ids=["CLM-003"],
                revision_claim_ids=["CLM-002"],
                legal_review_claim_ids=["CLM-001", "CLM-004"],
                rejected_claim_ids=[],
                missing_evidence=[
                    "substantiation for guaranteed 12% annual return",
                    "evidence for regulator approval claim",
                ],
                required_disclosures=["performance risk disclosure", "fee disclosure"],
                suggested_next_steps=[
                    "Remove guarantee language or provide approved substantiation.",
                    "Route regulator endorsement language to legal review.",
                ],
            )
        raise AssertionError(f"unexpected schema: {output_schema}")


def _fake_claim_review(index: int) -> ClaimComplianceReview:
    if index == 1:
        return ClaimComplianceReview(
            submission_id="MKT-2026-RET-0042",
            campaign_id="CMP-RET-2026-Q3",
            claim_id="CLM-001",
            decision="legal_review",
            risk_level="critical",
            risk_signals=["guaranteed return", "missing performance substantiation"],
            missing_evidence=["substantiation for guaranteed 12% annual return"],
            required_disclosures=["performance risk disclosure"],
            suggested_revision="Replace guaranteed return language with qualified historical context.",
            rationale="The evidence does not substantiate a guaranteed annual return.",
        )
    if index == 2:
        return ClaimComplianceReview(
            submission_id="MKT-2026-RET-0042",
            campaign_id="CMP-RET-2026-Q3",
            claim_id="CLM-002",
            decision="revise",
            risk_level="high",
            risk_signals=["risk-free language"],
            missing_evidence=[],
            required_disclosures=["investment risk disclosure"],
            suggested_revision="Explain the income feature without describing it as risk-free.",
            rationale="The claim overstates certainty for an investment product.",
        )
    if index == 3:
        return ClaimComplianceReview(
            submission_id="MKT-2026-RET-0042",
            campaign_id="CMP-RET-2026-Q3",
            claim_id="CLM-003",
            decision="approved",
            risk_level="low",
            risk_signals=[],
            missing_evidence=[],
            required_disclosures=["fee disclosure"],
            suggested_revision="No revision required.",
            rationale="The claim is factual and supported by the provided fee schedule.",
        )
    return ClaimComplianceReview(
        submission_id="MKT-2026-RET-0042",
        campaign_id="CMP-RET-2026-Q3",
        claim_id="CLM-004",
        decision="legal_review",
        risk_level="critical",
        risk_signals=["regulator endorsement claim"],
        missing_evidence=["evidence for regulator approval claim"],
        required_disclosures=[],
        suggested_revision="Remove regulator endorsement language unless legal approves it.",
        rationale="The evidence does not show regulator endorsement of the product.",
    )
