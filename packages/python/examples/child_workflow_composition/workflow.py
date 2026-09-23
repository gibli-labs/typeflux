"""Parent/child workflow definitions (#396) - the durable code-defined orchestration.

IMPORTANT - import-light by design: the Temporal determinism sandbox re-imports this
module when validating a workflow, so it imports ONLY ``temporalio`` and the Pydantic
schemas, and calls the activity by string NAME. Keep ``typeflux`` / provider
imports OUT of this module (they belong in ``activity.py``, registered by the worker).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

try:
    from temporalio import workflow
except ModuleNotFoundError:  # pragma: no cover - import convenience without temporalio.
    workflow = None

from examples.child_workflow_composition.schemas import Claim, Verdict

#: Must match ``activity.ASSESS_ACTIVITY_NAME`` (duplicated here to keep this module
#: free of the ``typeflux`` import the sandbox would re-run).
ASSESS_ACTIVITY_NAME = "assess_claim"

if workflow is not None:

    @workflow.defn
    class AssessClaimWorkflow:
        """Child workflow: one claim -> one AI activity, as its own durable unit
        (an adopter's reference-mapping sub-agent modeled as a child workflow)."""

        @workflow.run
        async def run(self, claim: Claim) -> Verdict:
            return await workflow.execute_activity(
                ASSESS_ACTIVITY_NAME,
                claim,
                start_to_close_timeout=timedelta(minutes=2),
            )

    @workflow.defn
    class ReviewWorkflow:
        """Parent workflow: composes a child workflow per claim via
        ``execute_child_workflow``, started in parallel then awaited - the durable
        ``@workflow.defn`` counterpart to the async ``fan_out`` runner."""

        @workflow.run
        async def run(self, claims: list[Claim]) -> list[Verdict]:
            handles = [
                await workflow.start_child_workflow(
                    AssessClaimWorkflow.run,
                    claim,
                    id=f"{workflow.info().workflow_id}-assess-{claim.claim_id}",
                )
                for claim in claims
            ]
            return list(await asyncio.gather(*handles))

else:  # pragma: no cover - placeholders when temporalio is unavailable.

    class AssessClaimWorkflow:
        pass

    class ReviewWorkflow:
        pass
