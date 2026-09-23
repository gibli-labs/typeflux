"""Runnable entry point for the claims-review composition example (#55).

Resolves the parent workflow THROUGH the project manifest (so its sub-workflow
references resolve), builds the composed runtime — the children's workflow classes and
activities register on the parent's worker — starts a high-priority batch against a
local Temporal dev server, drives the two review gates by gate id, and prints the
terminal packet plus the child execution ids.

Runs out of the box with the YAML-declared env-keyed provider (OPENAI_API_KEY) and
Langfuse observability (set TYPEFLUX_LOCAL_OBSERVABILITY=none for pure-offline traces):

    temporal server start-dev
    cd packages/python
    uv run python -m examples.claims_review_composition.main            # yaml+code parent
    uv run python -m examples.claims_review_composition.main --workflow claims_review_pure

Pick the first gate's decision with CLAIMS_REVIEW_DECISION=expedite (skips escalation).
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from uuid import uuid4

from examples.claims_review_composition.schemas import Claim, ClaimBatch, ReviewPacket
from typeflux import ReviewCommand
from typeflux.project import load_project_spec
from typeflux.project.environment import (
    project_environment_context,
    resolve_project_workflow,
    resolve_subworkflows_for,
)
from typeflux.yaml.runtime import build_runtime, prepare_runtime_build

HERE = Path(__file__).resolve().parent
MANIFEST = HERE.parent / "typeflux.project.yaml"


def sample_batch() -> ClaimBatch:
    return ClaimBatch(
        priority="high",
        claims=[
            Claim(claim_id="CLM-1", text="Cuts onboarding time by 30%."),
            Claim(claim_id="CLM-2", text="Approved for expedited replacement."),
        ],
    )


async def run_demo(workflow_ref: str) -> int:
    project = load_project_spec(MANIFEST)
    parent = resolve_project_workflow(project, workflow_id=workflow_ref, environment_id="local")
    sub = resolve_subworkflows_for(project, parent)
    with project_environment_context(parent.application):
        prepared = prepare_runtime_build(
            parent.spec,
            subworkflow_records=sub.records,
            child_workflow_classes=sub.workflow_classes,
            child_activities=sub.activities,
            # Registry composition (#748): the children's prompts are MERGED into the ONE
            # registry this worker serves, so the parent spec no longer duplicates them.
            child_registry_specs=tuple(sub.child_specs.items()),
        )
    runtime = await build_runtime(parent.spec, prepared=prepared)

    decision = os.environ.get("CLAIMS_REVIEW_DECISION", "escalate")
    workflow_id = f"claims-review-{uuid4().hex[:8]}"
    try:
        async with runtime.worker.build_worker():
            # runtime.start_workflow stamps the identity memo (spec digest + logical
            # name), which the lifecycle helpers' execution-binding check verifies.
            handle = await runtime.start_workflow(
                sample_batch(),
                id=workflow_id,
                result_type=ReviewPacket,
            )
            print(f"workflow_id={workflow_id}")

            await runtime.wait_for_lifecycle_state(workflow_id, "waiting_for_review")
            print(f"intake_gate open — deciding {decision!r}")
            await runtime.submit_lifecycle_review(
                workflow_id,
                ReviewCommand(user_decision=decision, gate="intake_gate", reviewer="demo"),
            )
            if decision == "escalate":
                await runtime.wait_for_lifecycle_state(workflow_id, "waiting_for_review")
                print('compliance_gate open — deciding "approve"')
                await runtime.submit_lifecycle_review(
                    workflow_id,
                    ReviewCommand(user_decision="approve", gate="compliance_gate", reviewer="demo"),
                )

            result = await handle.result()
            print(result.model_dump_json(indent=2))
            for child_id in (
                f"{workflow_id}.triage_all-0",
                f"{workflow_id}.triage_all-1",
                *((f"{workflow_id}.escalation",) if decision == "escalate" else ()),
            ):
                description = await runtime.client.get_workflow_handle(child_id).describe()
                print(f"child {child_id}: {description.status.name}")
    finally:
        runtime.observability.writer.shutdown()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workflow",
        default="claims_review",
        choices=["claims_review", "claims_review_pure"],
        help="Which authoring-mode parent to run (yaml+code or pure-YAML).",
    )
    args = parser.parse_args()
    return asyncio.run(run_demo(args.workflow))


if __name__ == "__main__":
    raise SystemExit(main())
