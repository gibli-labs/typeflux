from __future__ import annotations

from examples.financial_claims_marketing_review.activities import (
    consolidate_marketing_review,
    review_marketing_claim,
)
from examples.financial_claims_marketing_review.schemas import (
    ClaimComplianceReviewBatch,
    MarketingReviewPacket,
    MarketingSubmission,
)
from typeflux.core import AIActivity, ChatMessage, PromptRef, ResolvedPrompt
from typeflux.execution import execute_ai_activity
from typeflux.manifests import AIInvocationContext
from typeflux.prompts import InlinePromptRegistry
from typeflux.providers import ModelProvider

PIPELINE_NAME = "FinancialClaimsMarketingReview"


def run_offline_pipeline(
    submission: MarketingSubmission,
    *,
    provider: ModelProvider,
    registry: InlinePromptRegistry,
) -> MarketingReviewPacket:
    reviews = [
        execute_ai_activity(
            activity=review_marketing_claim,
            input_value=claim,
            registry=registry,
            provider=provider,
            invocation_context=_invocation_context(review_marketing_claim, index),
        )
        for index, claim in enumerate(submission.claims, start=1)
    ]
    batch = ClaimComplianceReviewBatch(reviews=reviews)
    result = execute_ai_activity(
        activity=consolidate_marketing_review,
        input_value=batch,
        registry=registry,
        provider=provider,
        invocation_context=_invocation_context(consolidate_marketing_review, len(reviews) + 1),
    )
    if not isinstance(result, MarketingReviewPacket):
        raise TypeError("financial claims marketing pipeline must return MarketingReviewPacket")
    return result.model_copy(update={"submission_id": submission.submission_id})


def inline_registry() -> InlinePromptRegistry:
    from examples.financial_claims_marketing_review.main import CHAT_PROMPTS

    prompts: dict[PromptRef, ResolvedPrompt] = {}
    for definition in CHAT_PROMPTS.values():
        activity = definition.activity
        ref = activity.prompt_ref
        prompts[ref] = ResolvedPrompt(
            ref=ref,
            messages=tuple(
                ChatMessage(role=message.role, content=message.content)
                for message in definition.messages
            ),
            resolved_version="local",
            model="fake-financial-compliance-model",
            temperature=0,
            metadata={"langfuse.prompt_type": "chat"},
        )
    return InlinePromptRegistry(prompts)


def _invocation_context(activity: AIActivity, index: int) -> AIInvocationContext:
    return AIInvocationContext(
        temporal_namespace="offline",
        temporal_workflow_type=PIPELINE_NAME,
        temporal_workflow_id="financial-claims-marketing-review-offline",
        temporal_run_id="offline-run",
        temporal_activity_type=activity.name,
        temporal_activity_id=f"{activity.name}-{index}",
        temporal_activity_attempt=1,
        typeflux_activity_name=activity.name,
        typeflux_manifest_hash="filled-by-executor-metadata",
    )
