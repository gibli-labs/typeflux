from __future__ import annotations

from examples.insurance_claim_review.schemas import (
    ClaimReviewPacket,
    EvidenceItem,
    EvidenceReview,
    EvidenceReviewBatch,
)
from typeflux.core import PromptRef, ai_activity


@ai_activity.defn(
    name="review_evidence_item",
    prompt=PromptRef("insurance-claim-review-evidence"),
    output=EvidenceReview,
    validation_retries=2,
)
def review_evidence_item(input: EvidenceItem, output: EvidenceReview) -> EvidenceReview:
    risk_signals = sorted({signal.strip().lower() for signal in output.risk_signals if signal})
    missing_context = sorted({item.strip().lower() for item in output.missing_context if item})
    follow_up_questions = [question.strip() for question in output.follow_up_questions if question]
    return output.model_copy(
        update={
            "claim_id": input.claim_id,
            "evidence_id": input.evidence_id,
            "risk_signals": risk_signals,
            "missing_context": missing_context,
            "follow_up_questions": follow_up_questions,
        }
    )


@ai_activity.defn(
    name="consolidate_claim_review",
    prompt=PromptRef("insurance-claim-consolidate"),
    output=ClaimReviewPacket,
)
def consolidate_claim_review(
    input: EvidenceReviewBatch,
    output: ClaimReviewPacket,
) -> ClaimReviewPacket:
    claim_id = input.reviews[0].claim_id
    risk_signals = sorted(
        {signal for review in input.reviews for signal in review.risk_signals if signal}
    )
    follow_up = sorted(
        {
            question
            for review in input.reviews
            for question in (*review.follow_up_questions, *review.missing_context)
            if question
        }
    )
    approval_required = bool(risk_signals or follow_up or output.recommendation != "approve")
    return output.model_copy(
        update={
            "claim_id": claim_id,
            "risk_signals": risk_signals,
            "required_follow_up": follow_up,
            "evidence_count": len(input.reviews),
            "approval_required": approval_required,
        }
    )


ALL_ACTIVITIES = (review_evidence_item, consolidate_claim_review)
