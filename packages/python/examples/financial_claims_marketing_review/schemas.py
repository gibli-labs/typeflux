from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

MarketingChannel = Literal["web", "email", "social", "advisor_script", "brochure"]
ProductCategory = Literal["retirement", "investment", "cash_management", "insurance"]
ReviewDecision = Literal["approved", "revise", "legal_review", "rejected"]
RiskLevel = Literal["low", "medium", "high", "critical"]
PacketDecision = Literal["approved", "revise_before_publish", "legal_review_required", "rejected"]


class MarketingClaim(BaseModel):
    submission_id: str = Field(description="Review submission identifier.")
    campaign_id: str = Field(description="Campaign identifier.")
    claim_id: str = Field(description="Stable claim identifier within the campaign.")
    product_category: ProductCategory = Field(description="Financial product category.")
    channel: MarketingChannel = Field(description="Marketing channel where the claim appears.")
    jurisdiction: str = Field(description="Jurisdiction or market where the claim will run.")
    audience: str = Field(description="Target customer segment.")
    claim_text: str = Field(description="Exact promotional claim under review.")
    evidence: str = Field(description="Available substantiation or supporting context.")
    contact: str = Field(description="Marketing owner contact for redaction demonstration.")


class MarketingSubmission(BaseModel):
    submission_id: str = Field(description="Review submission identifier.")
    brand: str = Field(description="Brand or business unit.")
    reviewer_email: str = Field(
        description="Compliance reviewer email for redaction demonstration."
    )
    claims: list[MarketingClaim] = Field(description="Promotional claims to review.")

    @field_validator("claims")
    @classmethod
    def _require_claims(cls, value: list[MarketingClaim]) -> list[MarketingClaim]:
        if not value:
            raise ValueError("marketing submission must include at least one claim")
        return value


class ClaimComplianceReview(BaseModel):
    submission_id: str = Field(description="Review submission identifier copied from the claim.")
    campaign_id: str = Field(description="Campaign identifier copied from the claim.")
    claim_id: str = Field(description="Reviewed claim identifier.")
    decision: ReviewDecision = Field(description="Claim-level compliance decision.")
    risk_level: RiskLevel = Field(description="Claim-level compliance risk.")
    risk_signals: list[str] = Field(description="Specific compliance risks or prohibited patterns.")
    missing_evidence: list[str] = Field(description="Evidence needed before approval.")
    required_disclosures: list[str] = Field(
        description="Disclosures needed for the claim or channel."
    )
    suggested_revision: str = Field(description="Compliant rewrite or revision guidance.")
    rationale: str = Field(description="Short rationale grounded in the provided evidence.")


class ClaimComplianceReviewBatch(BaseModel):
    reviews: list[ClaimComplianceReview] = Field(description="Ordered claim compliance reviews.")

    @field_validator("reviews")
    @classmethod
    def _require_reviews(cls, value: list[ClaimComplianceReview]) -> list[ClaimComplianceReview]:
        if not value:
            raise ValueError("review batch must contain at least one review")
        return value


class MarketingReviewPacket(BaseModel):
    submission_id: str = Field(description="Review submission identifier.")
    final_decision: PacketDecision = Field(description="Overall marketing approval decision.")
    summary: str = Field(description="Compliance summary for the submission.")
    approved_claim_ids: list[str] = Field(description="Claims approved as written.")
    revision_claim_ids: list[str] = Field(
        description="Claims requiring revision before publication."
    )
    legal_review_claim_ids: list[str] = Field(description="Claims requiring legal review.")
    rejected_claim_ids: list[str] = Field(description="Claims that should not be used.")
    missing_evidence: list[str] = Field(description="Submission-level missing evidence.")
    required_disclosures: list[str] = Field(description="Submission-level required disclosures.")
    suggested_next_steps: list[str] = Field(
        description="Operational next steps for marketing review."
    )
