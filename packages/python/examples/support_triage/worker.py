from __future__ import annotations

import asyncio

from temporalio.client import Client

from examples.support_triage.activities import ALL_ACTIVITIES
from examples.support_triage.schemas import (
    Classification,
    DraftReply,
    PackagedReply,
    RoutingDecision,
)
from examples.support_triage.workflow import SupportTriageWorkflow
from typeflux.core import ChatMessage, PromptRef, ResolvedPrompt
from typeflux.execution import TypefluxWorker
from typeflux.prompts import InlinePromptRegistry
from typeflux.testing import FakeProvider


async def main() -> None:
    client = await Client.connect("localhost:7233")
    registry = InlinePromptRegistry(
        {
            "support/classify": ResolvedPrompt(
                ref=PromptRef("support/classify"),
                messages=(ChatMessage("user", "Classify {{ subject }}: {{ body }}"),),
                resolved_version="demo-classify-v1",
                model="fake-model",
            ),
            "support/route": ResolvedPrompt(
                ref=PromptRef("support/route"),
                messages=(ChatMessage("user", "Route {{ subject }} for {{ customer_tier }}"),),
                resolved_version="demo-route-v1",
                model="fake-model",
            ),
            "support/draft": ResolvedPrompt(
                ref=PromptRef("support/draft"),
                messages=(ChatMessage("user", "Draft reply for {{ subject }}: {{ body }}"),),
                resolved_version="demo-draft-v1",
                model="fake-model",
            ),
            "support/package": ResolvedPrompt(
                ref=PromptRef("support/package"),
                messages=(ChatMessage("user", "Package final reply for {{ subject }}"),),
                resolved_version="demo-package-v1",
                model="fake-model",
            ),
        }
    )
    provider = FakeProvider(
        [
            Classification(category="billing", urgency="medium", summary="Billing question"),
            RoutingDecision(team="billing", priority=3, reason="Billing ownership"),
            DraftReply(
                subject="Re: Billing", body="Thanks for reaching out. We are checking this."
            ),
            PackagedReply(
                subject="Re: Billing",
                body="Thanks for reaching out. We are checking this.",
                team="billing",
                priority=3,
                internal_note="Prepared by Typeflux demo.",
            ),
        ]
    )
    await TypefluxWorker(
        client=client,
        task_queue="support-ai",
        activities=ALL_ACTIVITIES,
        registry=registry,
        provider=provider,
        workflows=[SupportTriageWorkflow],
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
