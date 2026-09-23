from __future__ import annotations

from examples.financial_claims_marketing_review.schemas import (
    ClaimComplianceReview,
    ClaimComplianceReviewBatch,
    MarketingClaim,
    MarketingReviewPacket,
)
from typeflux.core import PromptRef, ai_activity


@ai_activity.defn(
    name="review_marketing_claim",
    prompt=PromptRef("financial-claims-review-claim", prompt_type="chat"),
    output=ClaimComplianceReview,
    validation_retries=2,
)
def review_marketing_claim(
    input: MarketingClaim,
    output: ClaimComplianceReview,
) -> ClaimComplianceReview:
    return output.model_copy(
        update={
            "submission_id": input.submission_id,
            "campaign_id": input.campaign_id,
            "claim_id": input.claim_id,
            "risk_signals": _clean(output.risk_signals),
            "missing_evidence": _clean(output.missing_evidence),
            "required_disclosures": _clean(output.required_disclosures),
            "suggested_revision": output.suggested_revision.strip(),
            "rationale": output.rationale.strip(),
        }
    )


@ai_activity.defn(
    name="consolidate_marketing_review",
    prompt=PromptRef("financial-claims-consolidate", prompt_type="chat"),
    output=MarketingReviewPacket,
)
def consolidate_marketing_review(
    input: ClaimComplianceReviewBatch,
    output: MarketingReviewPacket,
) -> MarketingReviewPacket:
    submission_id = input.reviews[0].submission_id
    approved = [review.claim_id for review in input.reviews if review.decision == "approved"]
    revise = [review.claim_id for review in input.reviews if review.decision == "revise"]
    legal = [review.claim_id for review in input.reviews if review.decision == "legal_review"]
    rejected = [review.claim_id for review in input.reviews if review.decision == "rejected"]
    missing_evidence = _clean(item for review in input.reviews for item in review.missing_evidence)
    required_disclosures = _clean(
        item for review in input.reviews for item in review.required_disclosures
    )
    if rejected:
        final_decision = "rejected"
    elif legal:
        final_decision = "legal_review_required"
    elif revise or missing_evidence:
        final_decision = "revise_before_publish"
    else:
        final_decision = "approved"
    return output.model_copy(
        update={
            "submission_id": submission_id,
            "approved_claim_ids": approved,
            "revision_claim_ids": revise,
            "legal_review_claim_ids": legal,
            "rejected_claim_ids": rejected,
            "missing_evidence": missing_evidence,
            "required_disclosures": required_disclosures,
            "final_decision": final_decision,
        }
    )


def _clean(values) -> list[str]:
    return sorted({str(value).strip() for value in values if str(value).strip()})


ALL_ACTIVITIES = (review_marketing_claim, consolidate_marketing_review)
