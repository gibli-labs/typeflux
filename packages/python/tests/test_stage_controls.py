from __future__ import annotations

import asyncio

import pytest

from typeflux.execution import (
    WorkflowStageController,
    WorkflowStageEvent,
    WorkflowStageLimits,
)


def test_workflow_stage_limits_validate_max_concurrent() -> None:
    with pytest.raises(ValueError, match="max_concurrent must be >= 1"):
        WorkflowStageLimits(max_concurrent=0)


@pytest.mark.asyncio
async def test_workflow_stage_map_bounds_concurrency_and_preserves_order() -> None:
    events: list[WorkflowStageEvent] = []
    controller = WorkflowStageController(
        stage="review-pages",
        limits=WorkflowStageLimits(max_concurrent=2),
        on_event=events.append,
    )
    active = 0
    max_active = 0

    async def work(item: str, index: int) -> str:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.01)
            return f"{index}:{item}"
        finally:
            active -= 1

    result = await controller.map_ordered(["a", "b", "c", "d"], work)

    assert result == ["0:a", "1:b", "2:c", "3:d"]
    assert max_active == 2
    assert [event.status for event in events].count("queued") == 4
    assert [event.status for event in events].count("started") == 4
    assert [event.status for event in events].count("completed") == 4
    queued_metadata = events[0].to_metadata()
    assert set(queued_metadata) == {
        "stage",
        "unit_id",
        "status",
        "queued",
        "active",
        "cancelled",
        "completed",
        "failed",
        "queued_seconds",
        "active_seconds",
        "max_concurrent",
    }
    assert queued_metadata["stage"] == "review-pages"
    assert queued_metadata["unit_id"] == "0"
    assert queued_metadata["status"] == "queued"
    assert queued_metadata["max_concurrent"] == 2


@pytest.mark.asyncio
async def test_workflow_stage_controller_cancels_queued_work_before_start() -> None:
    events: list[WorkflowStageEvent] = []
    controller = WorkflowStageController(
        stage="review-pages",
        limits=WorkflowStageLimits(max_concurrent=1),
        on_event=events.append,
    )
    started_work: list[str] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def first_work() -> str:
        started_work.append("first")
        first_started.set()
        await release_first.wait()
        return "first"

    async def second_work() -> str:
        started_work.append("second")
        return "second"

    first = asyncio.create_task(controller.run("first", first_work))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    second = asyncio.create_task(controller.run("second", second_work))
    await asyncio.sleep(0)

    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    release_first.set()

    assert await first == "first"
    assert started_work == ["first"]
    assert any(
        event.unit_id == "second" and event.status == "cancelled" and event.cancel_phase == "queued"
        for event in events
    )
    assert not any(event.unit_id == "second" and event.status == "started" for event in events)


@pytest.mark.asyncio
async def test_workflow_stage_controller_uses_injected_clock() -> None:
    events: list[WorkflowStageEvent] = []
    now = 10.0

    def clock() -> float:
        return now

    controller = WorkflowStageController(stage="review-pages", on_event=events.append, clock=clock)

    async def work() -> str:
        nonlocal now
        now = 12.5
        return "done"

    assert await controller.run("first", work) == "done"

    completed = [event for event in events if event.status == "completed"][0]
    assert completed.active_seconds == 2.5


@pytest.mark.asyncio
async def test_workflow_stage_controller_releases_slot_when_started_event_fails() -> None:
    failed_started_event = True
    started_work: list[str] = []

    def on_event(event: WorkflowStageEvent) -> None:
        nonlocal failed_started_event
        if event.unit_id == "first" and event.status == "started" and failed_started_event:
            failed_started_event = False
            raise RuntimeError("event sink failed")

    controller = WorkflowStageController(
        stage="review-pages",
        limits=WorkflowStageLimits(max_concurrent=1),
        on_event=on_event,
    )

    async def first_work() -> str:
        started_work.append("first")
        return "first"

    async def second_work() -> str:
        started_work.append("second")
        return "second"

    with pytest.raises(RuntimeError, match="event sink failed"):
        await controller.run("first", first_work)

    assert await asyncio.wait_for(controller.run("second", second_work), timeout=1) == "second"
    assert started_work == ["second"]


@pytest.mark.asyncio
async def test_workflow_stage_map_cancels_remaining_work_on_failure() -> None:
    events: list[WorkflowStageEvent] = []
    controller = WorkflowStageController(
        stage="review-pages",
        limits=WorkflowStageLimits(max_concurrent=2),
        on_event=events.append,
    )
    started_work: list[str] = []
    second_started = asyncio.Event()
    second_cancelled = asyncio.Event()

    async def work(item: str, index: int) -> str:
        del index
        started_work.append(item)
        if item == "first":
            await second_started.wait()
            raise RuntimeError("first failed")
        if item == "second":
            second_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                second_cancelled.set()
                raise
        return item

    with pytest.raises(RuntimeError, match="first failed"):
        await controller.map_ordered(
            ["first", "second", "third"],
            work,
            unit_id=lambda item, index: f"{index}:{item}",
        )
    await asyncio.wait_for(second_cancelled.wait(), timeout=1)

    assert started_work == ["first", "second"]
    assert any(
        event.unit_id == "0:first"
        and event.status == "failed"
        and event.error_type == "RuntimeError"
        for event in events
    )
    assert any(
        event.unit_id == "1:second"
        and event.status == "cancelled"
        and event.cancel_phase == "active"
        for event in events
    )
    assert not any(event.unit_id == "2:third" and event.status == "started" for event in events)
