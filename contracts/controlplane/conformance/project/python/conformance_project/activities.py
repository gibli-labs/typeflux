"""Activities for the canonical conformance fixture project (#617)."""

from temporalio import activity

from conformance_project.schemas import AssessmentBatch, Decision


@activity.defn(name="decide")
async def decide(value: AssessmentBatch) -> Decision:
    return Decision(value=str(len(value.reviews)))
