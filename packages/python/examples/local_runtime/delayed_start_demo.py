"""Observe native Temporal backoff; the portable YAML computation is unchanged."""

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from typeflux.project import load_project_spec
from typeflux.project.environment import resolve_project_workflow
from typeflux.yaml import build_runtime

DELAY_SECONDS = 2


def delayed_schedule(history):
    started = history.events[0]
    backoff = started.workflow_execution_started_event_attributes.first_workflow_task_backoff
    assert backoff.ToTimedelta() == timedelta(seconds=DELAY_SECONDS)
    scheduled = next(
        (
            event
            for event in history.events
            if event.HasField("workflow_task_scheduled_event_attributes")
        ),
        None,
    )
    if scheduled is not None:
        assert (
            scheduled.event_time.ToNanoseconds() - started.event_time.ToNanoseconds()
            >= DELAY_SECONDS * 1_000_000_000
        )
    return scheduled


async def pending_without_worker(handle):
    deadline = asyncio.get_running_loop().time() + 10
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        assert remaining > 0, "delayed workflow task was not recorded within the observation window"
        history = await asyncio.wait_for(handle.fetch_history(), timeout=min(3, remaining))
        assert not any(
            event.HasField("workflow_task_started_event_attributes")
            or event.HasField("activity_task_scheduled_event_attributes")
            for event in history.events
        ), "the unique demonstration queue must have no worker yet"
        if delayed_schedule(history) is not None:
            return
        await asyncio.sleep(0.05)


async def demonstrate():
    root = Path(__file__).resolve().parent
    resolved = resolve_project_workflow(
        load_project_spec(root / "typeflux.project.yaml"),
        workflow_id="echo",
        environment_id="temporal",
    )
    raw = resolved.spec.model_dump(by_alias=True, exclude_unset=True)
    raw["task_queue"] = f"delayed-adoption-{uuid4().hex}"
    runtime = await asyncio.wait_for(
        build_runtime(type(resolved.spec).model_validate(raw)), timeout=10
    )
    handle = None
    primary_failed = False
    try:
        # Existing starter preparation/memo and native SDK options; no YAML timer.
        handle = await runtime.start_workflow(
            json.loads((root / "input.json").read_text()),
            id=f"delayed-adoption-{uuid4().hex}",
            start_delay=timedelta(seconds=DELAY_SECONDS),
            run_timeout=timedelta(seconds=45),
            rpc_timeout=timedelta(seconds=5),
        )
        before = await asyncio.wait_for(handle.describe(), timeout=3)
        assert before.status.name == "RUNNING"
        await pending_without_worker(handle)
        async with runtime.worker.build_worker():
            result = await asyncio.wait_for(handle.result(), timeout=20)
        result = result.model_dump()
        assert result == {"text": "local result"}
        after = await asyncio.wait_for(handle.describe(), timeout=3)
        assert after.run_id == before.run_id and after.status.name == "COMPLETED"
        history = await asyncio.wait_for(handle.fetch_history(), timeout=3)
        assert delayed_schedule(history) is not None
        first_task = next(
            event
            for event in history.events
            if event.HasField("workflow_task_started_event_attributes")
        )
        assert (
            first_task.event_time.ToNanoseconds() - history.events[0].event_time.ToNanoseconds()
            >= DELAY_SECONDS * 1_000_000_000
        )
        assert (
            sum(
                event.HasField("activity_task_scheduled_event_attributes")
                for event in history.events
            )
            == 1
        )
        return {
            "scenario": "delayed-start",
            "backoffSeconds": DELAY_SECONDS,
            "pendingWithoutWorker": True,
            "sameRun": True,
            "scheduledAfterDelay": True,
            "firstTaskStartedAfterDelay": True,
            "status": after.status.name,
            "result": result,
        }
    except BaseException:
        primary_failed = True
        raise
    finally:
        try:
            if (
                handle is not None
                and (await asyncio.wait_for(handle.describe(), timeout=3)).status.name == "RUNNING"
            ):
                await asyncio.wait_for(
                    handle.terminate("owned delayed-start demo cleanup"), timeout=3
                )
        except Exception:
            if not primary_failed:
                raise
            print("Additional delayed-start demo cleanup failed", file=sys.stderr)
        finally:
            runtime.observability.writer.shutdown()


async def main():
    print(json.dumps(await asyncio.wait_for(demonstrate(), timeout=60)))


if __name__ == "__main__":
    asyncio.run(main())
