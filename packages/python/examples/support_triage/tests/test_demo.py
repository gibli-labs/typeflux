from __future__ import annotations

from examples.support_triage.activities import ALL_ACTIVITIES
from examples.support_triage.schemas import (
    Classification,
    DraftReply,
    PackagedReply,
    RoutingDecision,
    Ticket,
)
from typeflux.core import ChatMessage, PromptRef, ResolvedPrompt
from typeflux.execution.executor import execute_ai_activity
from typeflux.prompts import InlinePromptRegistry
from typeflux.testing import FakeProvider


def test_support_triage_demo_runs_without_temporal_runtime() -> None:
    registry = InlinePromptRegistry(
        {
            "support/classify": ResolvedPrompt(
                ref=PromptRef("support/classify"),
                messages=(ChatMessage("user", "Classify {{ subject }}"),),
                resolved_version="classify-v1",
            ),
            "support/route": ResolvedPrompt(
                ref=PromptRef("support/route"),
                messages=(ChatMessage("user", "Route {{ subject }}"),),
                resolved_version="route-v1",
            ),
            "support/draft": ResolvedPrompt(
                ref=PromptRef("support/draft"),
                messages=(ChatMessage("user", "Draft {{ body }}"),),
                resolved_version="draft-v1",
            ),
            "support/package": ResolvedPrompt(
                ref=PromptRef("support/package"),
                messages=(ChatMessage("user", "Package {{ subject }}"),),
                resolved_version="package-v1",
            ),
        }
    )
    provider = FakeProvider(
        [
            Classification(category="billing", urgency="medium", summary="Invoice question"),
            RoutingDecision(team="billing", priority=3, reason="Billing owns invoices"),
            DraftReply(subject="Re: Invoice", body="Thanks, we are checking this."),
            PackagedReply(
                subject="Re: Invoice",
                body="Thanks, we are checking this.",
                team="billing",
                priority=3,
                internal_note="Ready to send.",
            ),
        ]
    )
    ticket = Ticket(subject="Invoice", body="Unexpected line item", customer_tier="enterprise")

    results = [
        execute_ai_activity(
            activity=activity,
            input_value=ticket,
            registry=registry,
            provider=provider,
        )
        for activity in ALL_ACTIVITIES
    ]

    assert isinstance(results[-1], PackagedReply)
    assert results[-1].priority == 3
    assert len(provider.calls) == 4
    assert provider.calls[0]["metadata"]["typeflux"]["join"]["activity_manifest_hash"]
