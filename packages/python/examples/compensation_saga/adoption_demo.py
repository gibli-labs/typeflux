"""Observe configured Temporal compensation; no Local undo system is added."""

import asyncio
import json
import sys
from datetime import timedelta
from uuid import uuid4

from temporalio.client import WorkflowFailureError

from examples.compensation_saga.main import YAML_PATH, sample_order
from typeflux.yaml import build_runtime, load_yaml_spec

RETRY_SECONDS = 5
FORWARD_FAILURE = "demonstration forward failure; fake effects only"


def failure_messages(error):
    messages = []
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        messages.append(str(error))
        error = getattr(error, "cause", None) or error.__cause__
    return " | ".join(messages)


async def recorded_compensation_retry(handle):
    deadline = asyncio.get_running_loop().time() + 4
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        assert remaining > 0, "compensation failure was not acknowledged before worker shutdown"
        description = await asyncio.wait_for(handle.describe(), timeout=min(3, remaining))
        assert description.status.name == "RUNNING"
        pending = description.raw_description.pending_activities
        if len(pending) == 1 and pending[0].HasField("last_failure"):
            return
        await asyncio.sleep(0.05)


async def demonstrate(scenario):
    raw = load_yaml_spec(YAML_PATH).model_dump(by_alias=True, exclude_unset=True)
    raw["task_queue"] = f"compensation-adoption-{uuid4().hex}"
    raw["runtime"]["provider"] = {
        "type": "custom",
        "class": "examples.compensation_saga.adoption_provider:AdoptionProvider",
        "config": {"scenario": scenario},
    }
    raw["runtime"]["provider_retry"] = {"max_attempts": 1}
    raw["runtime"]["activity_retry"] = {"maximum_attempts": 1}
    for definition in raw["activities"]["definitions"]:
        definition["start_to_close_timeout_seconds"] = 10
    raw["workflow"]["steps"][1]["compensate"]["retry"] = {
        "maximum_attempts": 2 if scenario == "replacement" else 1,
        "initial_interval_seconds": RETRY_SECONDS,
        "maximum_interval_seconds": RETRY_SECONDS,
        "backoff_coefficient": 1,
    }
    spec = type(load_yaml_spec(YAML_PATH)).model_validate(raw)
    runtime = await asyncio.wait_for(build_runtime(spec), timeout=10)
    replacement = None
    workflow_id = f"compensation-adoption-{uuid4().hex}"
    handle = runtime.client.get_workflow_handle(workflow_id)
    primary_failed = False
    try:
        handle = await runtime.start_workflow(
            sample_order(),
            id=workflow_id,
            run_timeout=timedelta(seconds=45),
            rpc_timeout=timedelta(seconds=5),
        )
        before = await asyncio.wait_for(handle.describe(), timeout=3)

        async def observe_failure():
            try:
                await asyncio.wait_for(handle.result(), timeout=20)
            except WorkflowFailureError as failure:
                assert FORWARD_FAILURE in failure_messages(failure)
            else:
                raise AssertionError("the intentional forward failure must remain the result")
            # A lifecycle query needs a compatible polling worker even after
            # the run closes. Capture it before this worker is shut down.
            return await asyncio.wait_for(
                runtime.query_lifecycle_status(workflow_id, run_id=before.run_id), timeout=5
            )

        if scenario == "replacement":
            async with runtime.worker.build_worker():
                await recorded_compensation_retry(handle)
            assert runtime.provider.calls == [
                ("book_room", 1),
                ("charge_card", 1),
                ("fulfill_order", 1),
                ("refund_card", 1),
            ]
            await asyncio.sleep(RETRY_SECONDS + 0.2)
            pending = await asyncio.wait_for(handle.describe(), timeout=3)
            assert pending.run_id == before.run_id and pending.status.name == "RUNNING"
            replacement = await asyncio.wait_for(build_runtime(spec), timeout=10)
            async with replacement.worker.build_worker():
                status = await observe_failure()
        else:
            async with runtime.worker.build_worker():
                status = await observe_failure()
        calls = runtime.provider.calls + (replacement.provider.calls if replacement else [])
        expected = [
            ("book_room", 1),
            ("charge_card", 1),
            ("fulfill_order", 1),
            ("refund_card", 1),
            *([("refund_card", 2)] if scenario == "replacement" else []),
            ("cancel_room", 1),
        ]
        assert calls == expected
        outcome = "partial" if scenario == "partial" else "complete"
        assert status.state == "failed" and status.compensation_status == outcome
        events = [
            (event.event, event.step_id)
            for event in status.events
            if event.event.startswith("compensation_")
        ]
        assert events == [
            ("compensation_started", "charge"),
            (
                "compensation_failed" if scenario == "partial" else "compensation_completed",
                "charge",
            ),
            ("compensation_started", "book"),
            ("compensation_completed", "book"),
        ]
        after = await asyncio.wait_for(handle.describe(), timeout=3)
        assert after.run_id == before.run_id and after.status.name == "FAILED"
        writes = {
            name: runtime.provider.external_writes[name]
            + (replacement.provider.external_writes[name] if replacement else 0)
            for name in ("book_room", "charge_card")
        }
        assert writes == {"book_room": 1, "charge_card": 1}
        return {
            "scenario": scenario,
            "sameRun": True,
            "originalFailure": True,
            "compensationStatus": outcome,
            "actions": calls,
            "forwardFakeWrites": writes,
            "retryRecordedBeforeStop": scenario == "replacement",
        }
    except BaseException:
        primary_failed = True
        raise
    finally:
        try:
            if (await asyncio.wait_for(handle.describe(), timeout=3)).status.name == "RUNNING":
                await asyncio.wait_for(
                    handle.terminate("owned compensation demo cleanup"), timeout=3
                )
        except Exception:
            if not primary_failed:
                raise
            print("Additional compensation demo cleanup failed", file=sys.stderr)
        finally:
            runtime.observability.writer.shutdown()
            if replacement is not None:
                replacement.observability.writer.shutdown()


async def main():
    observations = []
    for scenario in ("complete", "partial", "replacement"):
        observations.append(await demonstrate(scenario))
    print(json.dumps(observations))


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=90))
