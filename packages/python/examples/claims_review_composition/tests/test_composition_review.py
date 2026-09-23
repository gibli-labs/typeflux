"""Admission + LIVE proof for the claims-review composition example (#55 slice 5).

The non-live test admits the whole project under the `base` policy — including the
transitive-closure admission of the parent's referenced sub-workflows
(`policy_subworkflow_closure`). The live test runs the full composition end to end on a
real Temporal dev server: the `full_review` parallel branch fans the `claim_triage`
sub-workflow over the claims (two child executions), the two review gates are driven by
gate id, and the terminal `ReviewPacket` comes back.

Run the live test:
    temporal server start-dev
    TYPEFLUX_LIVE_TEMPORAL=1 TYPEFLUX_LOCAL_OBSERVABILITY=none \
      uv run --all-extras pytest -m live -k composition_review
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest

MANIFEST = Path(__file__).resolve().parents[2] / "typeflux.project.yaml"
LIVE = os.environ.get("TYPEFLUX_LIVE_TEMPORAL") == "1"


def test_project_admits_composition_with_subworkflow_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parent admits under `base` AND its referenced children are transitively
    admitted under the same policy (#55 §9 governance closure)."""
    monkeypatch.setenv("TYPEFLUX_LOCAL_OBSERVABILITY", "none")
    from typeflux.project import load_project_spec
    from typeflux.project.validation import validate_project_bundle

    project = load_project_spec(MANIFEST)
    report = validate_project_bundle(
        project,
        environment_id="local",
        workflow_ids=["claims_review", "claims_review_pure", "claim_triage", "escalation_review"],
        policy_ids=["base"],
    )
    assert report.ok, [issue.model_dump() for issue in report.issues]
    # BOTH authoring-mode parents admit, each with the transitive closure of the same
    # two pure-YAML children (scope addition A: yaml+code AND pure-YAML stand alone).
    for parent_id in ("claims_review", "claims_review_pure"):
        parent = next(w for w in report.resolved_workflows if w.workflow_id == parent_id)
        closure = next(c for c in parent.checks if c.code == "policy_subworkflow_closure")
        assert closure.status == "passed", (parent_id, closure)
        assert set(closure.details["referenced_workflows"]) == {
            "claim_triage",
            "escalation_review",
        }, parent_id


def test_pure_yaml_parent_declares_no_code_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pure-YAML parent stands alone (#55 scope addition A): the FULL composition
    surface with every activity declared inline — zero code modules — while the
    yaml+code twin injects its activities from `activities.py`."""
    monkeypatch.setenv("TYPEFLUX_LOCAL_OBSERVABILITY", "none")
    from typeflux.project import load_project_spec
    from typeflux.project.environment import resolve_project_workflow

    project = load_project_spec(MANIFEST)
    pure = resolve_project_workflow(
        project, workflow_id="claims_review_pure", environment_id="local"
    )
    assert pure.spec.activities.modules == []
    assert {d.name for d in pure.spec.activities.definitions} == {
        "acknowledge",
        "consolidate",
        "finalize",
    }
    coded = resolve_project_workflow(project, workflow_id="claims_review", environment_id="local")
    assert [m.module for m in coded.spec.activities.modules] == ["activities"]
    assert coded.spec.activities.definitions == []
    # Same graph shape in both modes: identical step ids and gate ids.
    step_ids = lambda spec: [step.id for step in spec.workflow.steps]  # noqa: E731
    gate_ids = lambda spec: [g.id for g in spec.workflow.lifecycle.resolved_gates()]  # noqa: E731
    assert step_ids(pure.spec) == step_ids(coded.spec)
    assert gate_ids(pure.spec) == gate_ids(coded.spec) == ["intake_gate", "compliance_gate"]


def test_composed_runtime_resolves_every_merged_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The NORMAL composed runtime path (`prepare_runtime_build` with the resolved
    sub-workflows — the same prelude `build_runtime` runs) merges the children's
    activities under the parent's, and they resolve prompts against the ONE COMPOSED
    registry — the MERGE of the parent's and every child's registry (#748), so the
    parent spec no longer duplicates the children's prompts. Preflight proves every
    merged activity's prompt resolves — the platform guarantee that superseded the
    parent-duplication requirement (PromptNotFoundError at first execution otherwise)."""
    monkeypatch.setenv("TYPEFLUX_LOCAL_OBSERVABILITY", "none")
    # Provider construction (no call) requires a credential shape; never used.
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    from typeflux.execution.preflight import preflight_ai_activities
    from typeflux.project import load_project_spec
    from typeflux.project.environment import (
        project_environment_context,
        resolve_project_workflow,
        resolve_subworkflows_for,
    )
    from typeflux.yaml.runtime import prepare_runtime_build

    project = load_project_spec(MANIFEST)
    parent = resolve_project_workflow(project, workflow_id="claims_review", environment_id="local")
    sub = resolve_subworkflows_for(project, parent)
    # The parent registry no longer carries the children's prompts (#748) — they come from
    # the merge of the resolved child specs' own registries.
    assert "triage-claim" not in parent.spec.runtime.registry.prompts
    assert "escalate-review" not in parent.spec.runtime.registry.prompts
    with project_environment_context(parent.application):
        prepared = prepare_runtime_build(
            parent.spec,
            subworkflow_records=sub.records,
            child_workflow_classes=sub.workflow_classes,
            child_activities=sub.activities,
            child_registry_specs=tuple(sub.child_specs.items()),
        )
    # The composed worker registers the parent's activities AND the children's.
    assert set(prepared.activities) == {
        "acknowledge",
        "consolidate",
        "finalize",
        "triage_claim",
        "escalate_review",
    }
    report = preflight_ai_activities(
        activities=tuple(prepared.activities.values()),
        registry=prepared.registry,
        provider=prepared.provider,
        provider_name=parent.spec.runtime.provider.type,
        provider_default_params=parent.spec.runtime.provider.provider_params(),
    )
    assert report.ok, report.to_dict()


@pytest.mark.asyncio
async def test_intake_gate_timeout_routes_to_finalize(monkeypatch: pytest.MonkeyPatch) -> None:
    """The example's `intake_gate` timeout at RUNTIME: when no reviewer decision
    arrives within the window, the gate wait fires the durable-timer path and routes
    to `finalize` (the spec's `on_timeout: route`) — the expedite-by-default behavior
    the README describes, exercised through `_maybe_wait_for_review` on the example's
    own lifecycle shape (two named gates, timeout on the first)."""
    from types import SimpleNamespace

    monkeypatch.setenv("TYPEFLUX_LOCAL_OBSERVABILITY", "none")
    from typeflux.project import load_project_spec
    from typeflux.project.environment import resolve_project_workflow
    from typeflux.yaml import workflow as workflow_module

    project = load_project_spec(MANIFEST)
    parent = resolve_project_workflow(project, workflow_id="claims_review", environment_id="local")
    lifecycle = workflow_module._LifecycleRuntime(parent.spec.workflow.lifecycle)
    gate = lifecycle.pending_gate_after("consolidate")
    assert gate is not None and gate.id == "intake_gate"
    # Shrink the 1h window (bypasses the spec's >=1s floor); the polling fallback fires it.
    gate.timeout_seconds = 0.01  # type: ignore[assignment]

    fake_workflow = SimpleNamespace()  # no wait_condition -> polling fallback
    route = await workflow_module._maybe_wait_for_review(fake_workflow, lifecycle, "consolidate")

    assert route == "finalize"
    assert lifecycle.state == "running"
    events = [event.event for event in lifecycle.status().events]
    assert events[-2:] == ["review_timed_out", "review_routed"]
    assert lifecycle.status().review_route_target == "finalize"


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.parametrize("parent_id", ["claims_review", "claims_review_pure"])
async def test_live_composition_drives_both_gates(
    parent_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Parameterized over BOTH authoring-mode parents (yaml+code and pure-YAML): the
    # same full composition surface runs live in each (scope addition A).
    if not LIVE:
        pytest.skip("live Temporal dev-server test; set TYPEFLUX_LIVE_TEMPORAL=1 and run -m live")
    monkeypatch.setenv("TYPEFLUX_LOCAL_OBSERVABILITY", "none")

    from temporalio.client import Client
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.worker import Worker

    from examples.claims_review_composition.fakes import ClaimsReviewFakeProvider
    from examples.claims_review_composition.schemas import Claim, ClaimBatch, ReviewPacket
    from typeflux.execution.worker import build_temporal_activity
    from typeflux.project import load_project_spec
    from typeflux.project.environment import (
        ProjectResolvedWorkflow,
        resolve_project_workflow,
        resolve_subworkflows_for,
    )
    from typeflux.yaml import collect_activities, create_workflow
    from typeflux.yaml.runtime import _build_registry
    from typeflux.yaml.workflow import create_yaml_workflow_runner

    project = load_project_spec(MANIFEST)
    parent = resolve_project_workflow(project, workflow_id=parent_id, environment_id="local")
    sub = resolve_subworkflows_for(project, parent)
    parent_cls = create_workflow(
        parent.spec, collect_activities(parent.spec), subworkflows=sub.records
    )

    provider = ClaimsReviewFakeProvider()

    # ONE composed registry for every activity on the worker (#748): the merge of the
    # parent's registry and the resolved children's — exactly what the production
    # `prepare_runtime_build` path serves (the parent yaml no longer duplicates the
    # children's prompts, so per-spec parent-only registries would fail resolution).
    registry = _build_registry(parent.spec, tuple(sub.child_specs.items()))

    def activity_fns(resolved: ProjectResolvedWorkflow) -> list[object]:
        return [
            build_temporal_activity(activity, registry=registry, provider=provider)
            for activity in collect_activities(resolved.spec).values()
        ]

    triage = resolve_project_workflow(project, workflow_id="claim_triage", environment_id="local")
    escalation = resolve_project_workflow(
        project, workflow_id="escalation_review", environment_id="local"
    )
    activities = activity_fns(parent) + activity_fns(triage) + activity_fns(escalation)

    address = os.environ.get("TYPEFLUX_LIVE_TEMPORAL_ADDRESS", "localhost:7233")
    client = await Client.connect(address, data_converter=pydantic_data_converter)

    task_queue = f"claims-review-{uuid4().hex}"
    workflow_id = f"claims-review-{uuid4().hex}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[parent_cls, *sub.workflow_classes],
        activities=activities,
        workflow_runner=create_yaml_workflow_runner(),
    ):
        handle = await client.start_workflow(
            parent_cls.run,
            ClaimBatch(
                priority="high",
                claims=[Claim(claim_id="CLM-1", text="a"), Claim(claim_id="CLM-2", text="b")],
            ),
            id=workflow_id,
            task_queue=task_queue,
            result_type=ReviewPacket,
        )

        async def wait_for_gate(gate_id: str) -> dict:
            for _ in range(100):
                status = await handle.query("typeflux_lifecycle_status")
                waiting = {gate["gate_id"] for gate in status.get("waiting_gates", [])}
                if gate_id in waiting:
                    return status
                await asyncio.sleep(0.1)
            raise AssertionError(f"gate {gate_id!r} never opened")

        await wait_for_gate("intake_gate")
        await handle.signal(
            "typeflux_submit_review", {"user_decision": "escalate", "gate": "intake_gate"}
        )
        await wait_for_gate("compliance_gate")
        await handle.signal(
            "typeflux_submit_review", {"user_decision": "approve", "gate": "compliance_gate"}
        )

        result = await handle.result()
        assert result == ReviewPacket(decision="approved")

        status = await handle.query("typeflux_lifecycle_status")
        started = [e["step_id"] for e in status["events"] if e["event"] == "step_started"]
        # priority != low -> the full_review branch ran (fast_track was gated out).
        assert "triage_all" in started
        assert "fast_ack" not in started
        assert started.index("consolidate") < started.index("escalation")

        # The map.workflow fan-out started one child per claim; the workflow: step one more.
        triage_0 = await client.get_workflow_handle(f"{workflow_id}.triage_all-0").describe()
        triage_1 = await client.get_workflow_handle(f"{workflow_id}.triage_all-1").describe()
        escalation_child = await client.get_workflow_handle(f"{workflow_id}.escalation").describe()
        for child in (triage_0, triage_1, escalation_child):
            memo = await child.memo()
            assert memo["typeflux_parent_workflow_id"] == workflow_id
