from __future__ import annotations

from pydantic import BaseModel

from examples.support_triage_langfuse.schemas import (
    Classification,
    DraftReply,
    ReviewPacket,
    RoutingDecision,
    Urgency,
)
from typeflux.core.contracts import ChatMessage


class FakeProvider:
    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict | None = None,
    ) -> BaseModel:
        if output_schema is Classification:
            return Classification(
                category="billing",
                urgency=Urgency.CRITICAL,
                sentiment=0.1,
                topics=["billing"],
            )
        if output_schema is RoutingDecision:
            return RoutingDecision(team="billing", sla_hours=4)
        if output_schema is DraftReply:
            return DraftReply(
                subject="Re: Billing issue",
                body="Thanks for writing in. We will investigate jane.doe@example.com.",
                tone="apologetic",
            )
        if output_schema is ReviewPacket:
            return ReviewPacket(
                verdict="flag",
                summary="Duplicate charge ticket with a drafted billing response.",
            )
        raise AssertionError(f"unexpected schema: {output_schema}")
