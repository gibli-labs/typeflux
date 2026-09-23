from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

EvidenceKind = Literal[
    "photo",
    "invoice",
    "email",
    "repair_estimate",
    "police_report",
    "adjuster_note",
    "other",
]
EvidenceDecision = Literal["support", "contradict", "needs_follow_up", "irrelevant"]
ClaimRecommendation = Literal["approve", "investigate", "deny", "escalate"]


class EvidenceItem(BaseModel):
    claim_id: str = Field(description="Claim identifier this evidence belongs to.")
    evidence_id: str = Field(description="Stable evidence identifier.")
    kind: EvidenceKind = Field(description="Evidence type.")
    source: str = Field(description="Source system, sender, or uploader.")
    received_at: datetime = Field(description="When the evidence entered the claim file.")
    content: str = Field(description="Text extracted from the evidence item.")


class ClaimInput(BaseModel):
    claim_id: str = Field(description="Claim identifier.")
    policy_id: str = Field(description="Policy identifier.")
    claimant_name: str = Field(description="Claimant display name.")
    loss_description: str = Field(description="Claimant's description of the loss.")
    loss_date: datetime = Field(description="Reported date of loss.")
    evidence: list[EvidenceItem] = Field(description="Evidence items to review.")

    @field_validator("evidence")
    @classmethod
    def _require_evidence(cls, value: list[EvidenceItem]) -> list[EvidenceItem]:
        if not value:
            raise ValueError("claim must include at least one evidence item")
        claim_ids = {item.claim_id for item in value}
        if len(claim_ids) != 1:
            raise ValueError("all evidence items must use the same claim_id")
        return value


class EvidenceReview(BaseModel):
    claim_id: str = Field(description="Claim identifier copied from the evidence item.")
    evidence_id: str = Field(description="Reviewed evidence identifier.")
    decision: EvidenceDecision = Field(description="How this evidence affects the claim.")
    relevance_score: float = Field(
        ge=0.0,
        le=1.0,
        description="0 means irrelevant, 1 means directly claim-dispositive.",
    )
    risk_signals: list[str] = Field(
        description="Potential fraud, compliance, or ambiguity signals."
    )
    missing_context: list[str] = Field(description="Information needed before final disposition.")
    follow_up_questions: list[str] = Field(description="Specific questions for adjuster follow-up.")
    summary: str = Field(description="Short evidence-level review summary.")


class EvidenceReviewBatch(BaseModel):
    reviews: list[EvidenceReview] = Field(description="Ordered evidence reviews.")

    @field_validator("reviews")
    @classmethod
    def _require_reviews(cls, value: list[EvidenceReview]) -> list[EvidenceReview]:
        if not value:
            raise ValueError("review batch must contain at least one review")
        return value


class ClaimReviewPacket(BaseModel):
    claim_id: str = Field(description="Claim identifier.")
    recommendation: ClaimRecommendation = Field(description="Suggested claim disposition.")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in the recommendation.")
    summary: str = Field(description="Claim-level review summary.")
    risk_signals: list[str] = Field(description="Claim-level risk signals.")
    required_follow_up: list[str] = Field(description="Follow-up needed before disposition.")
    evidence_count: int = Field(ge=1, description="Number of reviewed evidence items.")
    approval_required: bool = Field(description="True when a human adjuster must approve.")
