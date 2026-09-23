"""Direct unit tests for typeflux.project.drain."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from conftest import make_minimal_project, patch_workflow_list_client
from typeflux.project import load_project_spec, workflow_drain_status
from typeflux.project.drain import WorkflowDrainStatus, _drain_query


def _patch_running_types(monkeypatch: pytest.MonkeyPatch, workflow_types: list[str]) -> None:
    patch_workflow_list_client(
        monkeypatch,
        [SimpleNamespace(workflow_type=workflow_type) for workflow_type in workflow_types],
    )


@pytest.mark.asyncio
async def test_drain_mixed_current_and_old_versions_is_not_drained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = make_minimal_project(tmp_path, monkeypatch, "drain_unit_project")
    project = load_project_spec(manifest)
    _patch_running_types(monkeypatch, [])
    current = (
        await workflow_drain_status(project, workflow_id="workflow", environment_id="local")
    ).current_workflow_type

    # Current-version executions running alongside an old version: counts are
    # per type, keys are sorted, and drained stays False until the old
    # versions finish.
    _patch_running_types(
        monkeypatch,
        [
            "DrainDemoWorkflow.zzold",
            current,
            "DrainDemoWorkflow.aaold",
            current,
            "DrainDemoWorkflow.aaold",
        ],
    )

    status = await workflow_drain_status(project, workflow_id="workflow", environment_id="local")

    assert status.running == {
        "DrainDemoWorkflow.aaold": 2,
        current: 2,
        "DrainDemoWorkflow.zzold": 1,
    }
    assert list(status.running) == sorted(status.running)
    assert status.total_running == 5
    assert status.drained is False
    assert status.logical_workflow == "DrainDemoWorkflow"


def test_drain_query_prefix_matches_type_and_filters_running() -> None:
    # Type-prefix matching (never the search attribute) so pre-attribute
    # executions are never invisible to decommission gating.
    assert _drain_query("DrainDemoWorkflow") == (
        "WorkflowType STARTS_WITH 'DrainDemoWorkflow.' AND ExecutionStatus = 'Running'"
    )


def test_drain_status_model_round_trips_and_is_strict() -> None:
    status = WorkflowDrainStatus(
        logical_workflow="Demo",
        current_workflow_type="Demo.v2abc",
        query=_drain_query("Demo"),
        running={"Demo.v1old": 3, "Demo.v2abc": 1},
        total_running=4,
        drained=False,
    )

    payload = status.to_dict()
    assert payload["running"] == {"Demo.v1old": 3, "Demo.v2abc": 1}
    assert WorkflowDrainStatus.model_validate(payload) == status

    with pytest.raises(ValidationError):
        WorkflowDrainStatus.model_validate({**payload, "surprise": True})
    with pytest.raises(ValidationError):
        status.drained = True  # type: ignore[misc] - frozen model
