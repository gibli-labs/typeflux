from __future__ import annotations

from examples.insurance_claim_review.activities import (
    consolidate_claim_review,
    review_evidence_item,
)
from examples.insurance_claim_review.schemas import (
    ClaimInput,
    ClaimReviewPacket,
    EvidenceReviewBatch,
)
from typeflux.core import AIActivity, ChatMessage, PromptRef, ResolvedPrompt
from typeflux.execution import execute_ai_activity
from typeflux.manifests import AIInvocationContext
from typeflux.prompts import InlinePromptRegistry
from typeflux.providers import ModelProvider

PIPELINE_NAME = "InsuranceClaimEvidenceReview"


def run_offline_pipeline(
    claim: ClaimInput,
    *,
    provider: ModelProvider,
    registry: InlinePromptRegistry,
) -> ClaimReviewPacket:
    reviews = [
        execute_ai_activity(
            activity=review_evidence_item,
            input_value=item,
            registry=registry,
            provider=provider,
            invocation_context=_invocation_context(review_evidence_item, index),
        )
        for index, item in enumerate(claim.evidence, start=1)
    ]
    batch = EvidenceReviewBatch(reviews=reviews)
    result = execute_ai_activity(
        activity=consolidate_claim_review,
        input_value=batch,
        registry=registry,
        provider=provider,
        invocation_context=_invocation_context(consolidate_claim_review, len(reviews) + 1),
    )
    if not isinstance(result, ClaimReviewPacket):
        raise TypeError("insurance claim review pipeline must return ClaimReviewPacket")
    return result


def inline_registry() -> InlinePromptRegistry:
    from examples.insurance_claim_review.main import PROMPT_DIR, PROMPTS

    prompts: dict[PromptRef, ResolvedPrompt] = {}
    for filename, activity in PROMPTS.items():
        ref = activity.prompt_ref
        prompts[ref] = ResolvedPrompt(
            ref=ref,
            messages=(ChatMessage(role="user", content=(PROMPT_DIR / filename).read_text()),),
            resolved_version="local",
            model="fake-insurance-model",
            temperature=0,
        )
    return InlinePromptRegistry(prompts)


def _invocation_context(activity: AIActivity, index: int) -> AIInvocationContext:
    return AIInvocationContext(
        temporal_namespace="offline",
        temporal_workflow_type=PIPELINE_NAME,
        temporal_workflow_id="insurance-claim-review-offline",
        temporal_run_id="offline-run",
        temporal_activity_type=activity.name,
        temporal_activity_id=f"{activity.name}-{index}",
        temporal_activity_attempt=1,
        typeflux_activity_name=activity.name,
        typeflux_manifest_hash="filled-by-executor-metadata",
    )
