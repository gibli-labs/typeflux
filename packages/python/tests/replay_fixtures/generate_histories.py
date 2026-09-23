"""Regenerate the recorded replay histories against a local Temporal dev server.

Run from the repo root with the dev server up (see docs/yaml-worker-deployment.md):

    uv run python tests/replay_fixtures/generate_histories.py

Pass fixture names to regenerate selectively (so touching one fixture never
churns the others' recorded files):

    uv run python tests/replay_fixtures/generate_histories.py cached_map

Regeneration is required when GENERATOR_VERSION bumps or the fixture specs
change — the replay tests fail loudly in both cases, which is the signal this
harness exists to give.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

FIXTURES_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(FIXTURES_DIR))

# Deterministic parent ids for the sub-workflow fixtures — see _record_subworkflow.
# test_replay.py replays under these exact ids so the child-id derivation matches.
SUBWORKFLOW_PLAIN_ID = "replay-fixture-subworkflow-plain"
SUBWORKFLOW_MAP_ID = "replay-fixture-subworkflow-map"

from typeflux import ReviewCommand  # noqa: E402
from typeflux.yaml import build_runtime, load_yaml_spec  # noqa: E402


async def _record_plain() -> None:
    from replay_demo_project.schemas import InputModel, OutputModel

    runtime = await build_runtime(load_yaml_spec(FIXTURES_DIR / "plain.yaml"))
    workflow_id = f"replay-fixture-plain-{uuid4().hex[:8]}"
    async with runtime.worker.build_worker():
        result = await runtime.execute_workflow(
            InputModel(value="start"),
            id=workflow_id,
            result_type=OutputModel,
        )
    assert result == OutputModel(value="replay-fixture")
    await _dump_history(runtime, workflow_id, "plain.json")


async def _record_lifecycle() -> None:
    from replay_demo_project.schemas import InputModel, OutputModel

    runtime = await build_runtime(load_yaml_spec(FIXTURES_DIR / "lifecycle.yaml"))
    workflow_id = f"replay-fixture-lifecycle-{uuid4().hex[:8]}"
    async with runtime.worker.build_worker():
        # Start through the runtime so the execution carries the typeflux_project
        # identity memo that lifecycle ops verify-bind against (#320); a raw
        # client.start_workflow would skip it and the review submission below
        # would be refused with LifecycleBindingError.
        handle = await runtime.start_workflow(
            InputModel(value="start"),
            id=workflow_id,
            result_type=OutputModel,
        )
        while True:
            status = await handle.query("typeflux_lifecycle_status")
            if getattr(status, "state", None) == "waiting_for_review" or (
                isinstance(status, dict) and status.get("state") == "waiting_for_review"
            ):
                break
            await asyncio.sleep(0.2)
        await runtime.submit_lifecycle_review(
            workflow_id,
            ReviewCommand(user_decision="approve"),
        )
        result = await handle.result()
    assert result == OutputModel(value="replay-fixture")
    await _dump_history(runtime, workflow_id, "lifecycle.json")


async def _record_cached_map() -> None:
    from replay_demo_project.schemas import BatchInputModel, BatchModel, InputModel, MiddleModel

    runtime = await build_runtime(load_yaml_spec(FIXTURES_DIR / "cached_map.yaml"))
    workflow_id = f"replay-fixture-cached-map-{uuid4().hex[:8]}"
    async with runtime.worker.build_worker():
        # Three items over concurrency 2 so the recorded fan-out exercises the
        # slot-refill path, bracketed by the prep/release cache commands.
        result = await runtime.execute_workflow(
            BatchInputModel(items=[InputModel(value=v) for v in ("a", "b", "c")]),
            id=workflow_id,
            result_type=BatchModel,
        )
    assert result == BatchModel(results=[MiddleModel(value="replay-fixture")] * 3)
    await _dump_history(runtime, workflow_id, "cached_map.json")


def _status_field(status, name):
    if isinstance(status, dict):
        return status.get(name)
    return getattr(status, name, None)


def _skipped_events(status) -> list[tuple[str, str]]:
    events = _status_field(status, "events") or ()
    return [
        (_status_field(e, "step_id"), _status_field(e, "condition"))
        for e in events
        if _status_field(e, "event") == "step_skipped"
    ]


async def _record_composition() -> None:
    from replay_demo_project.schemas import InputModel, OutputModel

    runtime = await build_runtime(load_yaml_spec(FIXTURES_DIR / "composition.yaml"))
    async with runtime.worker.build_worker():
        # Full path: the gated branch runs (both branches concurrently) and the
        # gated tail executes.
        full_id = f"replay-fixture-composition-full-{uuid4().hex[:8]}"
        result = await runtime.execute_workflow(
            InputModel(value="deep"),
            id=full_id,
            result_type=OutputModel,
        )
        assert result == OutputModel(value="replay-fixture")
        full_status = await runtime.client.get_workflow_handle(full_id).query(
            "typeflux_lifecycle_status"
        )
        assert _skipped_events(full_status) == []

        # Gated path: the `screen` branch contributes None through the strict
        # activity boundary and the tail gate early-exits; both skips are recorded
        # as step_skipped events carrying the rendered condition (#55 §3.3).
        gated_id = f"replay-fixture-composition-gated-{uuid4().hex[:8]}"
        result = await runtime.execute_workflow(
            InputModel(value="start"),
            id=gated_id,
            result_type=OutputModel,
        )
        assert result == OutputModel(value="replay-fixture")
        gated_status = await runtime.client.get_workflow_handle(gated_id).query(
            "typeflux_lifecycle_status"
        )
        assert _skipped_events(gated_status) == [
            ("screen", 'input.value == "deep"'),
            ("escalate", 'input.value == "deep"'),
        ]
        assert _status_field(gated_status, "completed_units") == _status_field(
            gated_status, "total_units"
        )
    await _dump_history(runtime, full_id, "composition_full.json")
    await _dump_history(runtime, gated_id, "composition_gated.json")


async def _record_compensation() -> None:
    # Compensation (#299): a saga whose `charge` step fails, unwinding `book`'s compensation.
    # The recorded history carries the compensation activity schedule (cancel_book) + the
    # compensation lifecycle events, and ends in WorkflowExecutionFailed — replay verifies the
    # unwind reconstructs deterministically.
    from replay_demo_project.schemas import InputModel

    runtime = await build_runtime(load_yaml_spec(FIXTURES_DIR / "compensation.yaml"))
    workflow_id = f"replay-fixture-compensation-{uuid4().hex[:8]}"
    async with runtime.worker.build_worker():
        handle = await runtime.start_workflow(InputModel(value="start"), id=workflow_id)
        failed = False
        try:
            await handle.result()
        except Exception:
            failed = True
        assert failed, "compensation fixture workflow must fail (charge raises)"
        status = await handle.query("typeflux_lifecycle_status")
        events = [_status_field(e, "event") for e in (_status_field(status, "events") or ())]
        assert "compensation_started" in events, events
        assert "compensation_completed" in events, events
        assert _status_field(status, "compensation_status") == "complete", status
    await _dump_history(runtime, workflow_id, "compensation.json")


async def _record_subworkflow() -> None:
    # Sub-workflows (#55 slice 3): record the PARENT history for a plain
    # `workflow:` step and a `map.workflow` fan-out. The parent history carries
    # the StartChildWorkflowExecution + ChildWorkflowExecutionCompleted events, so
    # replaying the parent needs only the parent class (the child is external).
    # Built with a manual worker registering parent + child + the child's activity,
    # since children resolve through the project manifest (build_runtime is
    # single-spec).
    import os

    from replay_demo_project.schemas import (
        BatchInputModel,
        BatchModel,
        InputModel,
        MiddleModel,
    )
    from temporalio.client import Client
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.worker import Worker

    from typeflux.execution.worker import build_temporal_activity
    from typeflux.project import load_project_spec
    from typeflux.project.environment import (
        project_environment_context,
        resolve_project_workflow,
        resolve_subworkflows_for,
    )
    from typeflux.yaml import collect_activities, create_workflow
    from typeflux.yaml.runtime import _build_provider, _build_registry
    from typeflux.yaml.workflow import create_yaml_workflow_runner

    project = load_project_spec(FIXTURES_DIR / "typeflux.project.yaml")
    plain = resolve_project_workflow(project, workflow_id="plain", environment_id="local")
    fanout = resolve_project_workflow(project, workflow_id="fanout", environment_id="local")
    child = resolve_project_workflow(project, workflow_id="child", environment_id="local")
    plain_sub = resolve_subworkflows_for(project, plain)
    fanout_sub = resolve_subworkflows_for(project, fanout)
    with project_environment_context(plain.application):
        plain_cls = create_workflow(
            plain.spec, collect_activities(plain.spec), subworkflows=plain_sub.records
        )
        fanout_cls = create_workflow(
            fanout.spec, collect_activities(fanout.spec), subworkflows=fanout_sub.records
        )
        child_cls = plain_sub.workflow_classes[0]
        # The child's activities run with the CHILD spec's registry + provider.
        registry = _build_registry(child.spec)
        provider = _build_provider(child.spec, enable_langfuse=False)
        activity_fns = [
            build_temporal_activity(activity, registry=registry, provider=provider)
            for activity in collect_activities(child.spec).values()
        ]

    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        data_converter=pydantic_data_converter,
    )
    task_queue = plain.spec.task_queue
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[plain_cls, fanout_cls, child_cls],
        activities=activity_fns,
        workflow_runner=create_yaml_workflow_runner(),
    ):
        # DETERMINISTIC parent ids: the child ids derive from the parent id
        # (`{parent}.{step}` / `-{index}`), so the replay test must run under the SAME
        # parent id or the child-start command diverges from the scheduled event
        # (a nondeterminism error). Fresh dev server per recording => no id collision.
        plain_id = SUBWORKFLOW_PLAIN_ID
        result = await client.execute_workflow(
            plain_cls.run,
            InputModel(value="start"),
            id=plain_id,
            task_queue=task_queue,
            result_type=MiddleModel,
        )
        assert result == MiddleModel(value="replay-fixture")

        map_id = SUBWORKFLOW_MAP_ID
        map_result = await client.execute_workflow(
            fanout_cls.run,
            BatchInputModel(items=[InputModel(value=v) for v in ("a", "b", "c")]),
            id=map_id,
            task_queue=task_queue,
            result_type=BatchModel,
        )
        assert map_result == BatchModel(results=[MiddleModel(value="replay-fixture")] * 3)

    await _dump_client_history(client, plain_id, "subworkflow_plain.json")
    await _dump_client_history(client, map_id, "subworkflow_map.json")


async def _dump_client_history(client, workflow_id: str, filename: str) -> None:
    history = await client.get_workflow_handle(workflow_id).fetch_history()
    out = FIXTURES_DIR / "histories" / filename
    out.write_text(history.to_json(), encoding="utf-8")
    print(f"wrote {out}")


async def _dump_history(runtime, workflow_id: str, filename: str) -> None:
    handle = runtime.client.get_workflow_handle(workflow_id)
    history = await handle.fetch_history()
    out = FIXTURES_DIR / "histories" / filename
    out.write_text(history.to_json(), encoding="utf-8")
    print(f"wrote {out}")


_RECORDERS = {
    "plain": _record_plain,
    "lifecycle": _record_lifecycle,
    "cached_map": _record_cached_map,
    "composition": _record_composition,
    "subworkflow": _record_subworkflow,
    "compensation": _record_compensation,
}


async def _amain(names: list[str]) -> None:
    unknown = sorted(set(names) - set(_RECORDERS))
    if unknown:
        raise SystemExit(
            f"unknown fixture name(s): {', '.join(unknown)} (choose from: {', '.join(_RECORDERS)})"
        )
    for name in names or list(_RECORDERS):
        await _RECORDERS[name]()


if __name__ == "__main__":
    asyncio.run(_amain(sys.argv[1:]))
