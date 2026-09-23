"""Task-queue worker presence for the control plane (#278).

A workflow started against a task queue that no worker is polling sits
pending forever. This reports the live poller count for the workflow's
resolved task queue so the console can warn before the operator is left
wondering whether the run is progressing.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict

from typeflux.project.environment import (
    async_project_environment_context,
    resolve_project_workflow,
)
from typeflux.project.spec import TypefluxProjectSpec


class WorkflowTaskQueueWorkers(BaseModel):
    """Live worker poller presence for a workflow's task queue."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_queue: str
    reachable: bool
    workers_polling: int = 0
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


async def workflow_task_queue_workers(
    project: TypefluxProjectSpec,
    *,
    workflow_id: str,
    environment_id: str,
    task_queue: str | None = None,
) -> WorkflowTaskQueueWorkers:
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
    # An explicit override (the start panel's task-queue field) is the queue
    # the run actually targets; otherwise the resolved default.
    queue = task_queue or resolved.spec.task_queue
    async with async_project_environment_context(resolved.application):
        try:
            client = await _connect_client(resolved.spec, plugin=None)
            count = await _poller_count(client, resolved.spec.runtime.temporal.namespace, queue)
        except Exception as exc:  # noqa: BLE001 - degrade, never 500 the panel.
            return WorkflowTaskQueueWorkers(
                task_queue=queue,
                reachable=False,
                detail=f"could not reach Temporal: {exc}",
            )
    return WorkflowTaskQueueWorkers(
        task_queue=queue,
        reachable=True,
        workers_polling=count,
    )


async def _poller_count(client: Any, namespace: str | None, task_queue: str) -> int:
    from temporalio.api.enums.v1 import TaskQueueType
    from temporalio.api.taskqueue.v1 import TaskQueue
    from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest

    response = await client.workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(
            namespace=namespace or "default",
            task_queue=TaskQueue(name=task_queue),
            task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
        )
    )
    return len(response.pollers)


__all__ = ["WorkflowTaskQueueWorkers", "workflow_task_queue_workers"]
