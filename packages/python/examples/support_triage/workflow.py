from __future__ import annotations

from datetime import timedelta

try:
    from temporalio import workflow
except ModuleNotFoundError:  # pragma: no cover - import convenience without temporalio.
    workflow = None

from examples.support_triage.schemas import PackagedReply, Ticket

if workflow is not None:

    @workflow.defn
    class SupportTriageWorkflow:
        @workflow.run
        async def run(self, ticket: Ticket) -> PackagedReply:
            await workflow.execute_activity(
                "classify_ticket",
                ticket,
                start_to_close_timeout=timedelta(minutes=2),
            )
            await workflow.execute_activity(
                "route_ticket",
                ticket,
                start_to_close_timeout=timedelta(minutes=2),
            )
            await workflow.execute_activity(
                "draft_reply",
                ticket,
                start_to_close_timeout=timedelta(minutes=2),
            )
            return await workflow.execute_activity(
                "package_reply",
                ticket,
                start_to_close_timeout=timedelta(minutes=2),
            )

else:

    class SupportTriageWorkflow:  # pragma: no cover
        pass
