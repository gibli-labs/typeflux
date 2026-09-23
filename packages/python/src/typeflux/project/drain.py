from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict

from typeflux.project.environment import (
    async_project_environment_context,
    create_workflow_with_subworkflows,
    resolve_project_workflow,
)
from typeflux.project.spec import TypefluxProjectSpec


class WorkflowDrainStatus(BaseModel):
    """Running-execution counts for a logical workflow across versioned types.

    ``drained`` is true when no workflow type other than the currently-loaded
    spec's type has running executions — old versions are safe to
    decommission.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    logical_workflow: str
    current_workflow_type: str
    query: str
    running: dict[str, int]
    total_running: int
    drained: bool

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


async def workflow_drain_status(
    project: TypefluxProjectSpec,
    *,
    workflow_id: str,
    environment_id: str,
) -> WorkflowDrainStatus:
    from typeflux.yaml.runtime import _connect_client

    # resolve_project_workflow takes the process-wide env lock through the
    # SYNC context manager — a blocking acquire that must never run on the
    # event loop (#590: one holder across a Temporal await + one loop-blocked
    # waiter = a permanent, API-wide deadlock). Resolve in a worker thread.
    resolved = await asyncio.to_thread(
        resolve_project_workflow,
        project,
        workflow_id=workflow_id,
        environment_id=environment_id,
    )
    # Build with sub-workflow references resolved, in a worker thread too — it
    # takes the same process-wide env lock (see above) per resolved child.
    workflow_class, _subworkflows, _activities = await asyncio.to_thread(
        create_workflow_with_subworkflows, project, resolved
    )
    async with async_project_environment_context(resolved.application):
        client = await _connect_client(resolved.spec, plugin=None)
    logical = resolved.spec.workflow.name
    current_type: str = getattr(workflow_class, "__typeflux_workflow_type__")
    query = _drain_query(logical)
    running: dict[str, int] = {}
    async for execution in client.list_workflows(query):
        workflow_type = execution.workflow_type
        running[workflow_type] = running.get(workflow_type, 0) + 1
    total = sum(running.values())
    return WorkflowDrainStatus(
        logical_workflow=logical,
        current_workflow_type=current_type,
        query=query,
        running=dict(sorted(running.items())),
        total_running=total,
        drained=all(workflow_type == current_type for workflow_type in running),
    )


def _drain_query(logical_workflow: str) -> str:
    # Drain gating deliberately ignores the logical-name search attribute:
    # executions started before the attribute was enabled, or via raw
    # client.start_workflow, do not carry it and would be invisible —
    # reporting drained while old versions still run. Type names exist on
    # every execution, so prefix matching can at worst over-match a
    # same-prefix sibling workflow (a fail-safe false not-drained), never
    # under-match. The search attribute remains the indexed choice for runs
    # lists, just not for decommission gating.
    return f"WorkflowType STARTS_WITH '{logical_workflow}.' AND ExecutionStatus = 'Running'"


__all__ = ["WorkflowDrainStatus", "workflow_drain_status"]
