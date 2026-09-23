"""Direct unit tests for typeflux.project.runs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from conftest import make_minimal_project, patch_workflow_list_client
from typeflux.project import (
    load_project_spec,
    workflow_executions,
    workflow_run_correlation,
)
from typeflux.project import runs as runs_module
from typeflux.project.runs import WorkflowExecutionList, WorkflowExecutionRecord, _iso


@pytest.mark.asyncio
async def test_executions_stop_consuming_at_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = make_minimal_project(tmp_path, monkeypatch, "runs_unit_project")
    project = load_project_spec(manifest)
    items = [
        SimpleNamespace(
            id=f"case-{index}",
            run_id=f"r{index}",
            workflow_type="RunsDemoWorkflow.vX",
            status=SimpleNamespace(name="RUNNING"),
            start_time=None,
            close_time=None,
        )
        for index in range(5)
    ]
    client = patch_workflow_list_client(monkeypatch, items)

    listing = await workflow_executions(
        project, workflow_id="workflow", environment_id="local", limit=2
    )

    assert [record.execution_id for record in listing.executions] == ["case-0", "case-1"]
    # The iterator is abandoned at the limit, not drained.
    assert len(client.consumed) == 2


@pytest.mark.asyncio
async def test_execution_records_degrade_missing_fields_and_render_iso_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = make_minimal_project(tmp_path, monkeypatch, "runs_unit_project")
    project = load_project_spec(manifest)
    bare = SimpleNamespace(id="bare", workflow_type="RunsDemoWorkflow.old1")
    timed = SimpleNamespace(
        id="timed",
        run_id="r1",
        workflow_type="RunsDemoWorkflow.old2",
        status=SimpleNamespace(name="COMPLETED"),
        start_time=datetime(2026, 6, 1, 12, 30, tzinfo=UTC),
        close_time=datetime(2026, 6, 1, 12, 45, tzinfo=UTC),
    )
    patch_workflow_list_client(monkeypatch, [bare, timed])

    listing = await workflow_executions(project, workflow_id="workflow", environment_id="local")

    records = {record.execution_id: record for record in listing.executions}
    assert records["bare"].run_id is None
    assert records["bare"].status == "UNKNOWN"
    assert records["bare"].start_time is None
    assert records["bare"].current_version is False
    assert records["timed"].status == "COMPLETED"
    assert records["timed"].start_time == "2026-06-01T12:30:00+00:00"
    assert records["timed"].close_time == "2026-06-01T12:45:00+00:00"


def test_iso_renders_datetimes_and_none_otherwise() -> None:
    assert _iso(datetime(2026, 1, 2, tzinfo=UTC)) == "2026-01-02T00:00:00+00:00"
    assert _iso(None) is None
    assert _iso("2026-01-02") is None


@pytest.mark.asyncio
async def test_correlation_reports_langfuse_trace_and_degrades_when_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = make_minimal_project(
        tmp_path,
        monkeypatch,
        "runs_unit_project",
        observability_block="            type: langfuse",
    )
    project = load_project_spec(manifest)

    monkeypatch.setattr(
        runs_module, "_langfuse_trace_summary", lambda execution_id: {"id": f"trace-{execution_id}"}
    )
    client = patch_workflow_list_client(monkeypatch, [])
    found = await workflow_run_correlation(
        project, workflow_id="workflow", environment_id="local", execution_id="case-1"
    )
    assert found.observer == "langfuse"
    assert found.reachable is True
    assert found.trace == {"id": "trace-case-1"}
    assert found.warning is None
    # The amended no-refs contract (#204 review): children stays [] with no
    # children SCAN — but one describe runs for the migration provenance.
    assert found.children == ()
    assert client.queries == []
    assert client.described == ["case-1"]

    def _boom(execution_id: str) -> dict:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(runs_module, "_langfuse_trace_summary", _boom)
    degraded = await workflow_run_correlation(
        project, workflow_id="workflow", environment_id="local", execution_id="case-1"
    )
    assert degraded.reachable is False
    assert degraded.trace is None
    assert "observability backend unreachable: connection refused" in (degraded.warning or "")


def test_read_migration_provenance_parses_the_memo_and_ignores_absent() -> None:
    from typeflux.project.runs import (
        WorkflowMigrationProvenance,
        _read_migration_provenance,
    )

    # A migrated run's memo → the provenance record (run id + version key).
    got = _read_migration_provenance(
        {"typeflux_migrated_from": "old-run", "typeflux_migrated_from_version": "W.v1"}
    )
    assert got == WorkflowMigrationProvenance(run_id="old-run", version_key="W.v1")
    # The version key is optional; a bare run id still records provenance.
    assert _read_migration_provenance({"typeflux_migrated_from": "old-run"}) == (
        WorkflowMigrationProvenance(run_id="old-run", version_key=None)
    )
    # A non-migrated run (no key, or an empty/typed-wrong value) → None (field omitted).
    assert _read_migration_provenance({}) is None
    assert _read_migration_provenance({"typeflux_migrated_from": ""}) is None
    assert _read_migration_provenance({"typeflux_migrated_from": 123}) is None


@pytest.mark.asyncio
async def test_correlation_surfaces_migrated_from_for_a_non_composed_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The common case (#204 review): a NON-composed workflow whose memo carries
    # typeflux_migrated_from — one describe surfaces the provenance; children
    # stays () with no scan.
    from typeflux.project.runs import WorkflowMigrationProvenance

    manifest = make_minimal_project(tmp_path, monkeypatch, "runs_unit_project")
    project = load_project_spec(manifest)
    client = patch_workflow_list_client(
        monkeypatch,
        [],
        describe_memos={
            "migrated-case": {
                "typeflux_migrated_from": "old-run-9",
                "typeflux_migrated_from_version": "RunsDemoWorkflow.v1",
            }
        },
    )

    result = await workflow_run_correlation(
        project, workflow_id="workflow", environment_id="local", execution_id="migrated-case"
    )

    assert result.migrated_from == WorkflowMigrationProvenance(
        run_id="old-run-9", version_key="RunsDemoWorkflow.v1"
    )
    assert result.children == ()
    assert result.warning is None
    assert client.queries == []  # no children scan
    assert client.described == ["migrated-case"]  # exactly one provenance describe


@pytest.mark.asyncio
async def test_correlation_provenance_degrades_with_a_warning_when_describe_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Degraded path (#204 review): the provenance describe fails → the field is
    # OMITTED and a warning names the provenance read — never an error.
    manifest = make_minimal_project(tmp_path, monkeypatch, "runs_unit_project")
    project = load_project_spec(manifest)

    async def refusing_connect(spec, *, plugin):
        raise RuntimeError("Failed client connect: connection refused")

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", refusing_connect)

    result = await workflow_run_correlation(
        project, workflow_id="workflow", environment_id="local", execution_id="case-1"
    )

    assert result.migrated_from is None
    # Children never needed the tier for a no-refs workflow: the honest ().
    assert result.children == ()
    assert "temporal tier unreachable for the migration provenance" in (result.warning or "")


def test_execution_list_model_round_trips_and_is_strict() -> None:
    listing = WorkflowExecutionList(
        logical_workflow="RunsDemoWorkflow",
        current_workflow_type="RunsDemoWorkflow.vX",
        executions=(
            WorkflowExecutionRecord(
                execution_id="case-1",
                run_id=None,
                workflow_type="RunsDemoWorkflow.old",
                current_version=False,
                status="TERMINATED",
            ),
        ),
    )

    payload = listing.to_dict()
    assert payload["executions"][0]["run_id"] is None
    assert WorkflowExecutionList.model_validate(payload) == listing

    with pytest.raises(ValidationError):
        WorkflowExecutionList.model_validate({**payload, "extra": 1})
