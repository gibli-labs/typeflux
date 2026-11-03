"""Observe existing Temporal retry/recovery and timeout behavior; never runs locally."""

import asyncio
import json
import logging
import sys
from pathlib import Path
from uuid import uuid4

from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError, TimeoutError, TimeoutType
from temporalio.runtime import LogForwardingConfig, LoggingConfig, Runtime, TelemetryConfig

from typeflux.project import load_project_spec
from typeflux.project.environment import resolve_project_workflow
from typeflux.yaml import build_runtime

RETRY_SECONDS = 5


async def recorded_retry(handle):
    while True:
        description = await handle.describe()
        assert description.status.name == "RUNNING"
        if any(
            pending.HasField("last_failure")
            for pending in description.raw_description.pending_activities
        ):
            return description
        await asyncio.sleep(0.05)


async def demonstrate(mode):
    root = Path(__file__).resolve().parent
    resolved = resolve_project_workflow(
        load_project_spec(root / "typeflux.project.yaml"),
        workflow_id="echo",
        environment_id="temporal",
    )
    # The same computational YAML, contracts and prompt. Only operational policy
    # and the fake fault provider change; Local rejects these Temporal policies.
    raw = resolved.spec.model_dump(by_alias=True, exclude_unset=True)
    raw["task_queue"] = f"adoption-{uuid4().hex}"
    raw["runtime"]["provider"] = {
        "type": "custom",
        "class": "local_example.recovery_provider:RecoveryProvider",
        "config": {"mode": mode},
    }
    raw["runtime"]["provider_retry"] = {"max_attempts": 1}
    raw["runtime"]["activity_retry"] = {
        "maximum_attempts": 2 if mode == "retry" else 1,
        "initial_interval_seconds": RETRY_SECONDS,
        "maximum_interval_seconds": RETRY_SECONDS,
        "backoff_coefficient": 1,
    }
    timeout = 10 if mode == "retry" else 0.5
    raw["activities"]["definitions"][0]["start_to_close_timeout_seconds"] = timeout
    spec = type(resolved.spec).model_validate(raw)
    runtime = await build_runtime(spec)
    replacement = None
    handle = None
    retry_recorded_before_stop = False
    try:
        handle = await runtime.start_workflow(
            json.loads((root / "input.json").read_text()), id=f"adoption-{uuid4().hex}"
        )
        before = await handle.describe()
        if mode == "retry":
            async with runtime.worker.build_worker():
                await asyncio.wait_for(runtime.provider.first_failure.wait(), timeout=15)
                # A provider-local event precedes SDK failure encoding/reporting.
                # Keep the worker alive until Temporal acknowledges the failure.
                await asyncio.wait_for(recorded_retry(handle), timeout=4)
                retry_recorded_before_stop = True
            assert runtime.provider.calls == [1]
            # No worker polls this unique queue while the server retains the run
            # and its retry. This simulates worker unavailability, not server loss.
            await asyncio.sleep(RETRY_SECONDS + 0.2)
            paused = await handle.describe()
            assert paused.run_id == before.run_id and paused.status.name == "RUNNING"
            replacement = await build_runtime(spec)
            async with replacement.worker.build_worker():
                result = await asyncio.wait_for(handle.result(), timeout=20)
            result = result.model_dump()
            assert result == {"text": "local result"}
            attempts = runtime.provider.calls + replacement.provider.calls
            assert attempts == [1, 2]
        else:
            async with runtime.worker.build_worker():
                try:
                    await asyncio.wait_for(handle.result(), timeout=15)
                except WorkflowFailureError as failure:
                    assert isinstance(failure.cause, ActivityError)
                    assert isinstance(failure.cause.cause, TimeoutError)
                    assert failure.cause.cause.type == TimeoutType.START_TO_CLOSE
                else:
                    raise AssertionError("configured activity timeout must fail")
            attempts = runtime.provider.calls
            assert attempts == [1]
        after = await handle.describe()
        assert after.run_id == before.run_id
        history = await handle.fetch_history()
        scheduled = [
            event.activity_task_scheduled_event_attributes
            for event in history.events
            if event.HasField("activity_task_scheduled_event_attributes")
        ]
        assert len(scheduled) == 1
        assert scheduled[0].retry_policy.maximum_attempts == (2 if mode == "retry" else 1)
        assert (
            scheduled[0].retry_policy.initial_interval.ToTimedelta().total_seconds()
            == RETRY_SECONDS
        )
        assert (
            scheduled[0].retry_policy.maximum_interval.ToTimedelta().total_seconds()
            == RETRY_SECONDS
        )
        assert scheduled[0].retry_policy.backoff_coefficient == 1
        assert scheduled[0].start_to_close_timeout.ToTimedelta().total_seconds() == timeout
        return {
            "scenario": mode,
            "attempts": attempts,
            "sameRun": True,
            "retryRecordedBeforeStop": retry_recorded_before_stop,
            "status": after.status.name,
            "maximumAttempts": scheduled[0].retry_policy.maximum_attempts,
            "startToCloseSeconds": timeout,
            "result": result if mode == "retry" else None,
        }
    finally:
        primary_error = sys.exception()
        cleanup_errors = []
        try:
            if handle is not None:
                description = await handle.describe()
                if description.status.name == "RUNNING":
                    await handle.terminate(reason="adoption demo cleanup")
        except Exception as error:
            cleanup_errors.append(error)
        for owned in (runtime, replacement):
            if owned is not None:
                try:
                    owned.observability.writer.shutdown()
                except Exception as error:
                    cleanup_errors.append(error)
        if cleanup_errors:
            if primary_error is None:
                raise ExceptionGroup("adoption demo cleanup failed", cleanup_errors)
            primary_error.add_note(
                "Additional cleanup failures: "
                + ", ".join(type(error).__name__ for error in cleanup_errors)
            )


async def main():
    # Preserve Core warnings on stderr so stdout remains the JSON evidence record.
    logging.basicConfig()
    Runtime.set_default(
        Runtime(
            telemetry=TelemetryConfig(
                logging=LoggingConfig(
                    filter="warn",
                    forwarding=LogForwardingConfig(logging.getLogger("temporal.core")),
                )
            )
        )
    )
    async with asyncio.timeout(75):
        print(json.dumps([await demonstrate("retry"), await demonstrate("timeout")]))


if __name__ == "__main__":
    asyncio.run(main())
