"""Direct unit tests for typeflux.project.workers.

The end-to-end poller reporting (override queue, unreachable degrade) is
covered in tests/test_topology.py; this exercises the request construction in
``_poller_count`` and the response model contract directly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from typeflux.project.workers import WorkflowTaskQueueWorkers, _poller_count


class _CapturingClient:
    def __init__(self, pollers: int) -> None:
        self._pollers = pollers
        self.requests: list = []

    @property
    def workflow_service(self):
        client = self

        class _Service:
            async def describe_task_queue(self, request):
                client.requests.append(request)
                return SimpleNamespace(pollers=[object()] * client._pollers)

        return _Service()


@pytest.mark.asyncio
async def test_poller_count_targets_the_workflow_queue_and_defaults_namespace() -> None:
    from temporalio.api.enums.v1 import TaskQueueType

    client = _CapturingClient(pollers=3)

    count = await _poller_count(client, None, "demo-queue")

    assert count == 3
    request = client.requests[0]
    # No configured namespace degrades to Temporal's default, never empty.
    assert request.namespace == "default"
    assert request.task_queue.name == "demo-queue"
    assert request.task_queue_type == TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW


@pytest.mark.asyncio
async def test_poller_count_uses_the_configured_namespace_and_counts_zero() -> None:
    client = _CapturingClient(pollers=0)

    count = await _poller_count(client, "team-namespace", "demo-queue")

    assert count == 0
    assert client.requests[0].namespace == "team-namespace"


def test_workers_model_defaults_round_trip_and_strictness() -> None:
    unreachable = WorkflowTaskQueueWorkers(
        task_queue="demo-queue",
        reachable=False,
        detail="could not reach Temporal: boom",
    )
    assert unreachable.workers_polling == 0

    payload = unreachable.to_dict()
    assert payload == {
        "task_queue": "demo-queue",
        "reachable": False,
        "workers_polling": 0,
        "detail": "could not reach Temporal: boom",
    }
    assert WorkflowTaskQueueWorkers.model_validate(payload) == unreachable

    with pytest.raises(ValidationError):
        WorkflowTaskQueueWorkers.model_validate({**payload, "surprise": 1})
    with pytest.raises(ValidationError):
        unreachable.reachable = True  # type: ignore[misc] - frozen model
