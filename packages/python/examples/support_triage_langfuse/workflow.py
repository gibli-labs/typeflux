from __future__ import annotations

from datetime import timedelta

from temporalio import workflow

from examples.support_triage_langfuse.schemas import (
    Classification,
    DraftReply,
    ReviewPacket,
    RoutingDecision,
    TicketInput,
)


@workflow.defn
class SupportTriageLangfuseWorkflow:
    @workflow.run
    async def run(self, ticket: TicketInput) -> ReviewPacket:
        classification = await workflow.execute_activity(
            "classify_ticket",
            ticket,
            result_type=Classification,
            start_to_close_timeout=timedelta(minutes=2),
        )
        routing = await workflow.execute_activity(
            "route_ticket",
            classification,
            result_type=RoutingDecision,
            start_to_close_timeout=timedelta(minutes=2),
        )
        draft = await workflow.execute_activity(
            "draft_response",
            routing,
            result_type=DraftReply,
            start_to_close_timeout=timedelta(minutes=2),
        )
        return await workflow.execute_activity(
            "package_for_review",
            draft,
            result_type=ReviewPacket,
            start_to_close_timeout=timedelta(minutes=2),
        )
