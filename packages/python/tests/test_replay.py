"""Recorded-history replay tests for YAML-generated workflows.

The histories under tests/replay_fixtures/histories were captured from a real
Temporal dev server (see tests/replay_fixtures/generate_histories.py). Replay
itself is fully local. A clean replay of the unchanged fixture spec is the
regression net for engine control-flow changes; a deliberate GENERATOR_VERSION
bump requires regenerating the fixtures, which is the signal this harness
exists to give.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "replay_fixtures"


@pytest.fixture()
def fixture_path(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.syspath_prepend(str(FIXTURES_DIR))
    for name in tuple(sys.modules):
        if name == "replay_demo_project" or name.startswith("replay_demo_project."):
            del sys.modules[name]
    return FIXTURES_DIR


def _workflow_class(fixture_path: Path, spec_name: str):
    from typeflux.yaml import collect_activities, create_workflow, load_yaml_spec

    spec = load_yaml_spec(fixture_path / spec_name, load_dotenv=False)
    return create_workflow(spec, collect_activities(spec))


def _subworkflow_parent_class(fixture_path: Path, workflow_id: str):
    # A sub-workflow parent resolves its child through the project manifest (#55 §3.4),
    # so its class is built via project resolution (create_workflow with the resolved
    # sibling records) rather than the standalone loader. Replaying the PARENT history
    # needs only the parent class — the child is an external execution whose result is
    # recorded in the parent's ChildWorkflowExecutionCompleted event.
    from typeflux.project import load_project_spec
    from typeflux.project.environment import (
        project_environment_context,
        resolve_project_workflow,
        resolve_subworkflows_for,
    )
    from typeflux.yaml import collect_activities, create_workflow

    project = load_project_spec(fixture_path / "typeflux.project.yaml")
    resolved = resolve_project_workflow(project, workflow_id=workflow_id, environment_id="local")
    subworkflows = resolve_subworkflows_for(project, resolved)
    with project_environment_context(resolved.application):
        return create_workflow(
            resolved.spec, collect_activities(resolved.spec), subworkflows=subworkflows.records
        )


def _history(fixture_path: Path, name: str, workflow_id: str | None = None):
    from temporalio.client import WorkflowHistory

    raw = (fixture_path / "histories" / name).read_text(encoding="utf-8")
    # A sub-workflow parent derives its child ids from `workflow.info().workflow_id`
    # (`{parent}.{step}`), so its history must replay under the ORIGINAL parent id or
    # the child-start command diverges from the scheduled event; other fixtures don't
    # start children, so an arbitrary replay id is fine.
    return WorkflowHistory.from_json(workflow_id or f"replay-{name}", raw)


def _replayer(workflow_class: type):
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.worker import Replayer

    from typeflux.yaml.workflow import create_yaml_workflow_runner

    return Replayer(
        workflows=[workflow_class],
        workflow_runner=create_yaml_workflow_runner(),
        data_converter=pydantic_data_converter,
    )


@pytest.mark.asyncio
async def test_unchanged_spec_replays_recorded_plain_history(fixture_path: Path) -> None:
    workflow_class = _workflow_class(fixture_path, "plain.yaml")

    await _replayer(workflow_class).replay_workflow(_history(fixture_path, "plain.json"))


@pytest.mark.asyncio
async def test_unchanged_spec_replays_recorded_lifecycle_history(fixture_path: Path) -> None:
    # The review wait/routing path is the most replay-sensitive engine code.
    workflow_class = _workflow_class(fixture_path, "lifecycle.yaml")

    await _replayer(workflow_class).replay_workflow(_history(fixture_path, "lifecycle.json"))


@pytest.mark.asyncio
async def test_unchanged_spec_replays_recorded_cached_map_history(fixture_path: Path) -> None:
    # The cache-enabled map bracket — the {activity}.__prepare_cache__ command,
    # the fan-out threading the recorded handle, the {activity}.__release_cache__
    # command (#363/#368) — is workflow-scheduled control flow, so it must
    # replay deterministically against the recorded history.
    workflow_class = _workflow_class(fixture_path, "cached_map.yaml")

    await _replayer(workflow_class).replay_workflow(_history(fixture_path, "cached_map.json"))


@pytest.mark.asyncio
async def test_unchanged_spec_replays_recorded_compensation_history(fixture_path: Path) -> None:
    # Compensation (#299) is replay-sensitive engine code: the failure unwind schedules the
    # compensation activity (cancel_book) from the outer handler, and that command must
    # reconstruct deterministically against the recorded history (which ends in a
    # WorkflowExecutionFailed after the compensation ran).
    workflow_class = _workflow_class(fixture_path, "compensation.yaml")

    await _replayer(workflow_class).replay_workflow(_history(fixture_path, "compensation.json"))


@pytest.mark.asyncio
async def test_unchanged_spec_replays_recorded_composition_histories(fixture_path: Path) -> None:
    # Composition control flow (#55 slice 2) is the newest replay-sensitive
    # engine code: the concurrent branch tasks' command order, the gated-branch
    # None contribution, and the early-exit tail must all replay deterministically
    # against the recorded full and gated runs of ONE spec.
    workflow_class = _workflow_class(fixture_path, "composition.yaml")

    replayer = _replayer(workflow_class)
    await replayer.replay_workflow(_history(fixture_path, "composition_full.json"))
    await replayer.replay_workflow(_history(fixture_path, "composition_gated.json"))


@pytest.mark.asyncio
async def test_unchanged_spec_replays_recorded_subworkflow_histories(fixture_path: Path) -> None:
    # Sub-workflow control flow (#55 slice 3) is the newest replay-sensitive engine
    # code: the plain `workflow:` step's child start/await and the `map.workflow`
    # fan-out's bounded-concurrency child starts (each StartChildWorkflowExecution +
    # its recorded completion) must replay deterministically against the parent
    # histories. Replaying the PARENT needs only the parent class.
    plain_class = _subworkflow_parent_class(fixture_path, "plain")
    await _replayer(plain_class).replay_workflow(
        _history(fixture_path, "subworkflow_plain.json", "replay-fixture-subworkflow-plain")
    )
    map_class = _subworkflow_parent_class(fixture_path, "fanout")
    await _replayer(map_class).replay_workflow(
        _history(fixture_path, "subworkflow_map.json", "replay-fixture-subworkflow-map")
    )


@pytest.mark.asyncio
async def test_child_graph_edit_makes_recorded_subworkflow_history_unreplayable(
    fixture_path: Path,
    tmp_path: Path,
) -> None:
    # The child's registered TYPE + digest are baked into the parent's
    # SubworkflowCallSpec, and the child digest folds into the parent digest (#55 §6):
    # a child-graph edit moves the PARENT's registered workflow type, so the recorded
    # parent history — which pins the original type — cannot reach the edited parent.
    original_class = _subworkflow_parent_class(fixture_path, "plain")
    original_type = original_class.__typeflux_workflow_type__

    from typeflux.project import load_project_spec
    from typeflux.project.environment import (
        project_environment_context,
        resolve_project_workflow,
        resolve_subworkflows_for,
    )
    from typeflux.yaml import collect_activities, create_workflow

    # Edit ONLY the child graph (append a second step). Point the manifest's child at
    # the edited copy so the parent folds the moved child digest.
    child_src = (fixture_path / "subworkflow_child.yaml").read_text(encoding="utf-8")
    edited_child = child_src.replace(
        "    - id: first\n      activity: first\n",
        "    - id: first\n      activity: first\n    - id: second\n      activity: second\n",
    ).replace("schemas:MiddleModel", "schemas:OutputModel")
    (tmp_path / "subworkflow_child.yaml").write_text(edited_child, encoding="utf-8")
    (tmp_path / "subworkflow_plain.yaml").write_text(
        (fixture_path / "subworkflow_plain.yaml")
        .read_text(encoding="utf-8")
        .replace("schemas:MiddleModel", "schemas:OutputModel"),
        encoding="utf-8",
    )
    (tmp_path / "typeflux.project.yaml").write_text(
        (fixture_path / "typeflux.project.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "environments").mkdir(exist_ok=True)
    (tmp_path / "environments" / "local.yaml").write_text(
        (fixture_path / "environments" / "local.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    project = load_project_spec(tmp_path / "typeflux.project.yaml")
    resolved = resolve_project_workflow(project, workflow_id="plain", environment_id="local")
    subworkflows = resolve_subworkflows_for(project, resolved)
    with project_environment_context(resolved.application):
        edited_class = create_workflow(
            resolved.spec, collect_activities(resolved.spec), subworkflows=subworkflows.records
        )
    assert edited_class.__typeflux_workflow_type__ != original_type

    with pytest.raises(Exception, match=original_type.replace(".", r"\.")):
        await _replayer(edited_class).replay_workflow(
            _history(fixture_path, "subworkflow_plain.json")
        )


@pytest.mark.asyncio
async def test_composition_edit_makes_recorded_history_unreplayable(
    fixture_path: Path,
    tmp_path: Path,
) -> None:
    # A `when` literal edit changes the spec digest, so the edited composition
    # registers a different workflow type: the recorded history cannot reach it.
    edited = (
        (fixture_path / "composition.yaml")
        .read_text(encoding="utf-8")
        .replace("eq: deep", "eq: deeper")
    )
    edited_path = tmp_path / "edited.yaml"
    edited_path.write_text(edited, encoding="utf-8")

    from typeflux.yaml import collect_activities, create_workflow, load_yaml_spec

    original_class = _workflow_class(fixture_path, "composition.yaml")
    edited_spec = load_yaml_spec(edited_path, load_dotenv=False)
    edited_class = create_workflow(edited_spec, collect_activities(edited_spec))

    original_type = original_class.__typeflux_workflow_type__
    assert edited_class.__typeflux_workflow_type__ != original_type

    with pytest.raises(Exception, match=original_type.replace(".", r"\.")):
        await _replayer(edited_class).replay_workflow(
            _history(fixture_path, "composition_full.json")
        )


@pytest.mark.asyncio
async def test_graph_edit_makes_recorded_history_unreplayable(
    fixture_path: Path,
    tmp_path: Path,
) -> None:
    # A renamed step changes the spec digest, so the edited graph registers a
    # different workflow type: the recorded history cannot reach it at all.
    edited = (
        (fixture_path / "plain.yaml")
        .read_text(encoding="utf-8")
        .replace("- id: first", "- id: first_renamed")
    )
    edited_path = tmp_path / "edited.yaml"
    edited_path.write_text(edited, encoding="utf-8")

    from typeflux.yaml import collect_activities, create_workflow, load_yaml_spec

    original_class = _workflow_class(fixture_path, "plain.yaml")
    edited_spec = load_yaml_spec(edited_path, load_dotenv=False)
    edited_class = create_workflow(edited_spec, collect_activities(edited_spec))

    original_type = original_class.__typeflux_workflow_type__
    assert edited_class.__typeflux_workflow_type__ != original_type

    with pytest.raises(Exception, match=original_type.replace(".", r"\.")):
        await _replayer(edited_class).replay_workflow(_history(fixture_path, "plain.json"))


@pytest.mark.asyncio
async def test_graph_edit_makes_recorded_cached_map_history_unreplayable(
    fixture_path: Path,
    tmp_path: Path,
) -> None:
    # Renaming the map step changes the spec digest, so the edited graph
    # registers a different workflow type: the cached-map history (prep/release
    # commands included) cannot reach it at all.
    edited = (
        (fixture_path / "cached_map.yaml")
        .read_text(encoding="utf-8")
        .replace("- id: mapped", "- id: mapped_renamed")
    )
    edited_path = tmp_path / "edited.yaml"
    edited_path.write_text(edited, encoding="utf-8")

    from typeflux.yaml import collect_activities, create_workflow, load_yaml_spec

    original_class = _workflow_class(fixture_path, "cached_map.yaml")
    edited_spec = load_yaml_spec(edited_path, load_dotenv=False)
    edited_class = create_workflow(edited_spec, collect_activities(edited_spec))

    original_type = original_class.__typeflux_workflow_type__
    assert edited_class.__typeflux_workflow_type__ != original_type

    with pytest.raises(Exception, match=original_type.replace(".", r"\.")):
        await _replayer(edited_class).replay_workflow(_history(fixture_path, "cached_map.json"))
