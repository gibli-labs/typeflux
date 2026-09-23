from __future__ import annotations

import os
from typing import Literal

import pytest
from pydantic import BaseModel

from typeflux.core.contracts import AIActivity, ChatMessage, PromptRef, ResolvedPrompt
from typeflux.env import load_env
from typeflux.execution.executor import execute_ai_activity
from typeflux.manifests import AIInvocationContext
from typeflux.prompts import InlinePromptRegistry
from typeflux.providers import OpenAIProvider


class LiveTicket(BaseModel):
    subject: str
    body: str
    customer_tier: Literal["standard", "enterprise"]


class LiveClassification(BaseModel):
    category: Literal["billing", "technical", "account", "other"]
    urgency: Literal["low", "medium", "high"]
    summary: str


def _missing_live_env() -> list[str]:
    required = ["OPENAI_API_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"]
    return [name for name in required if not os.getenv(name)]


@pytest.mark.live
def test_live_openai_langfuse_invocation(request: pytest.FixtureRequest) -> None:
    load_env()
    if request.config.option.markexpr != "live":
        pytest.skip("live tests run only when explicitly selected with -m live")
    missing = _missing_live_env()
    if os.getenv("TYPEFLUX_RUN_LIVE") != "1" or missing:
        pytest.skip(
            "set TYPEFLUX_RUN_LIVE=1 plus OPENAI_API_KEY, "
            "LANGFUSE_PUBLIC_KEY, and LANGFUSE_SECRET_KEY to run"
        )

    prompt_ref = PromptRef("support/live-classify", version="live")
    model = os.getenv("TYPEFLUX_OPENAI_MODEL", "gpt-4o-mini")
    activity = AIActivity(
        name="live_classify_ticket",
        input_type=LiveTicket,
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
                        content=(
                            "Classify support tickets. Return only the requested structured output."
                        ),
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
        input_value=LiveTicket(
            subject="Unexpected invoice line item",
            body="Can someone explain a charge I do not recognize?",
            customer_tier="enterprise",
        ),
        registry=registry,
        provider=OpenAIProvider(default_model=model, enable_langfuse=True),
        invocation_context=AIInvocationContext(
            temporal_namespace="live-test",
            temporal_workflow_type="SupportTriageWorkflow",
            temporal_workflow_id="support-triage-live-test",
            temporal_run_id="manual-run",
            temporal_activity_type="live_classify_ticket",
            temporal_activity_id="activity-live-test",
            temporal_activity_attempt=1,
            typeflux_activity_name="live_classify_ticket",
            typeflux_manifest_hash="filled-by-executor-metadata",
        ),
    )

    assert result.category in {"billing", "technical", "account", "other"}
    assert result.urgency in {"low", "medium", "high"}
    assert result.summary

    from langfuse import get_client

    get_client().flush()
