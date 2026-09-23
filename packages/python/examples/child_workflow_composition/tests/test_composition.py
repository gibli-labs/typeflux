"""Proves child-workflow composition (#396).

The end-to-end tests run a real parent->child->AIActivity through a time-skipping
``WorkflowEnvironment`` (skipping when the Temporal test server is unavailable, as
``tests/test_yaml.py`` does). The structural test always runs.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from examples.child_workflow_composition.activity import (
    ASSESS_ACTIVITY_NAME,
    assess_claim_activity,
    inline_registry,
)
from examples.child_workflow_composition.schemas import Claim, Verdict
from examples.child_workflow_composition.workflow import (
    AssessClaimWorkflow,
    ReviewWorkflow,
)
from typeflux.testing import FakeProvider


def _verdict(claim_id: str, *, substantiated: bool = True) -> Verdict:
    return Verdict(claim_id=claim_id, substantiated=substantiated, rationale="assessed")


@asynccontextmanager
async def _running_worker(responses: list[Verdict]):
    """A time-skipping WorkflowEnvironment + Worker running the parent/child
    workflows and the FakeProvider-backed assess activity. Skips if the Temporal
    test server cannot start."""
    try:
        from temporalio.contrib.pydantic import pydantic_data_converter
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker
    except ModuleNotFoundError as exc:  # pragma: no cover - temporalio not installed.
        pytest.skip(f"temporalio not installed: {exc}")

    from typeflux.execution.worker import build_temporal_activity

    try:
        env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    except Exception as exc:  # pragma: no cover - server binary unavailable (e.g. CI).
        pytest.skip(f"Temporal test server unavailable: {exc}")

    assess_fn = build_temporal_activity(
        assess_claim_activity,
        registry=inline_registry(),
        provider=FakeProvider(list(responses)),
    )
    task_queue = f"cwc-{uuid4().hex}"
    async with env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ReviewWorkflow, AssessClaimWorkflow],
            activities=[assess_fn],
        ):
            yield env.client, task_queue


@pytest.mark.asyncio
async def test_parent_composes_child_via_execute_child_workflow() -> None:
    claim = Claim(claim_id="CLM-1", text="cuts risk by 30%")
    async with _running_worker([_verdict("CLM-1")]) as (client, task_queue):
        result = await client.execute_workflow(
            ReviewWorkflow.run,
            [claim],
            id=f"review-{uuid4().hex}",
            task_queue=task_queue,
        )
    # The parent returns the child workflow's typed Verdict, produced by the
    # AIActivity the child ran.
    assert result == [_verdict("CLM-1")]


@pytest.mark.asyncio
async def test_parent_fans_out_a_child_per_claim() -> None:
    claims = [Claim(claim_id="A", text="claim a"), Claim(claim_id="B", text="claim b")]
    # Distinct verdicts; the two children run concurrently so the verdict<->child
    # assignment is race-ordered - assert the returned SET, not positions.
    async with _running_worker([_verdict("A"), _verdict("B")]) as (client, task_queue):
        result = await client.execute_workflow(
            ReviewWorkflow.run,
            claims,
            id=f"review-{uuid4().hex}",
            task_queue=task_queue,
        )
    assert len(result) == 2
    assert sorted(v.claim_id for v in result) == ["A", "B"]
    assert all(v.substantiated for v in result)


def test_activity_name_matches_what_the_child_workflow_invokes() -> None:
    # The workflow module names the activity by string (sandbox-light); it must
    # match the AIActivity the worker registers.
    from examples.child_workflow_composition import workflow as workflow_module

    assert workflow_module.ASSESS_ACTIVITY_NAME == ASSESS_ACTIVITY_NAME
    assert assess_claim_activity.name == ASSESS_ACTIVITY_NAME
