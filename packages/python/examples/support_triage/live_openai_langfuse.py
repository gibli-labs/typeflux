from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel

from examples.support_triage.schemas import Ticket
from typeflux.core import AIActivity, ChatMessage, PromptRef, ResolvedPrompt
from typeflux.env import load_env
from typeflux.execution import execute_ai_activity
from typeflux.manifests import AIInvocationContext
from typeflux.prompts import InlinePromptRegistry
from typeflux.providers import OpenAIProvider


class LiveClassification(BaseModel):
    category: Literal["billing", "technical", "account", "other"]
    urgency: Literal["low", "medium", "high"]
    summary: str


def main() -> None:
    load_env()
    model = os.getenv("TYPEFLUX_OPENAI_MODEL", "gpt-4o-mini")
    prompt_ref = PromptRef("support/live-classify", label="live")
    activity = AIActivity(
        name="live_classify_ticket",
        input_type=Ticket,
        output_type=LiveClassification,
        prompt_ref=prompt_ref,
        validation_retries=1,
    )
    registry = InlinePromptRegistry(
        {
            prompt_ref: ResolvedPrompt(
                ref=prompt_ref,
                messages=(
                    ChatMessage(
                        role="system",
                        content="Classify support tickets into the requested schema.",
                    ),
                    ChatMessage(
                        role="user",
                        content=(
                            "Subject: {{ subject }}\n"
                            "Body: {{ body }}\n"
                            "Customer tier: {{ customer_tier }}"
                        ),
                    ),
                ),
                resolved_version="live-local-v1",
                model=model,
                temperature=0,
            )
        }
    )
    result = execute_ai_activity(
        activity=activity,
        input_value=Ticket(
            subject="Unexpected invoice line item",
            body="Can someone explain a charge I do not recognize?",
            customer_tier="enterprise",
        ),
        registry=registry,
        provider=OpenAIProvider(default_model=model, enable_langfuse=True),
        invocation_context=AIInvocationContext(
            temporal_namespace="manual-live",
            temporal_workflow_type="SupportTriageWorkflow",
            temporal_workflow_id="support-triage-live-manual",
            temporal_run_id="manual-run",
            temporal_activity_type="live_classify_ticket",
            temporal_activity_id="activity-live-manual",
            temporal_activity_attempt=1,
            typeflux_activity_name="live_classify_ticket",
            typeflux_manifest_hash="filled-by-executor-metadata",
        ),
    )
    print(result.model_dump_json(indent=2))

    from langfuse import get_client

    get_client().flush()


if __name__ == "__main__":
    main()
