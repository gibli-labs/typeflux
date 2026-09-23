from __future__ import annotations

from pydantic import BaseModel

from examples.support_triage_langfuse.activities import (
    classify_ticket,
    draft_response,
    package_for_review,
    route_ticket,
)
from examples.support_triage_langfuse.schemas import ReviewPacket, TicketInput
from typeflux.core import AIActivity
from typeflux.execution import execute_ai_activity
from typeflux.manifests import AIInvocationContext
from typeflux.prompts import PromptRegistry
from typeflux.providers import ModelProvider

PIPELINE_NAME = "SupportTriageLangfusePipeline"
PIPELINE_ACTIVITIES = (classify_ticket, route_ticket, draft_response, package_for_review)


def run(
    ticket: TicketInput,
    *,
    registry: PromptRegistry,
    provider: ModelProvider,
) -> ReviewPacket:
    current: BaseModel = ticket
    for index, activity in enumerate(PIPELINE_ACTIVITIES, start=1):
        current = execute_ai_activity(
            activity=activity,
            input_value=current,
            registry=registry,
            provider=provider,
            invocation_context=_invocation_context(activity, index),
        )
    if not isinstance(current, ReviewPacket):
        raise TypeError("support triage pipeline must return ReviewPacket")
    return current


def _invocation_context(activity: AIActivity, index: int) -> AIInvocationContext:
    return AIInvocationContext(
        temporal_namespace="manual-live",
        temporal_workflow_type=PIPELINE_NAME,
        temporal_workflow_id="support-triage-langfuse-manual",
        temporal_run_id="manual-run",
        temporal_activity_type=activity.name,
        temporal_activity_id=f"{activity.name}-{index}",
        temporal_activity_attempt=1,
        typeflux_activity_name=activity.name,
        typeflux_manifest_hash="filled-by-executor-metadata",
    )
