"""Composition closure + sub-workflow-depth admission (#298 Phase A) — Python edition.

The transitive-closure walk validates every referenced child against the PARENT's
composed policy (so a child that overflows a ceiling fails the parent's admission), and
carries the ``max_subworkflow_depth`` ceiling (the parent is depth 0, measured as the
longest reference chain).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from typeflux.project import load_project_spec
from typeflux.project.deployment import (
    ProjectDeploymentError,
    _admit_policy,
)
from typeflux.project.environment import resolve_project_workflow
from typeflux.project.policy import compose_project_policies
from typeflux.project.policy_enforcement import validate_subworkflow_closure_policy


def _leaf_activity_steps(count: int) -> str:
    steps = "\n".join(f"    - {{ id: s{i}, activity: a }}" for i in range(count))
    return steps


def _write_workflow(path: Path, *, name: str, steps_yaml: str, with_activity: bool = True) -> None:
    activities = (
        "activities:\n  definitions:\n"
        "    - { name: a, input: schemas:In, output: schemas:Out, prompt: p/x }\n"
        if with_activity
        else "activities: {}\n"
    )
    path.write_text(
        "project: proj\n"
        f"name: {name}\n"
        "task_queue: q\n"
        "runtime:\n"
        "  registry: { type: inline, prompts: { p/x: hi } }\n"
        "  provider: { type: openai, model: gpt-4o-mini }\n"
        f"{activities}"
        "workflow:\n"
        f"  name: {name}\n"
        "  input: schemas:In\n"
        "  output: schemas:Out\n"
        "  steps:\n"
        f"{steps_yaml}\n",
        encoding="utf-8",
    )


def _build_project(
    tmp_path: Path,
    *,
    composition: str,
    workflows: dict[str, str],
    target_workflows: list[str],
) -> Path:
    (tmp_path / "environments").mkdir()
    (tmp_path / "environments" / "local.yaml").write_text("name: local\n", encoding="utf-8")
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "comp.yaml").write_text(
        f'version: "1"\nname: comp\ncomposition:\n{composition}\n', encoding="utf-8"
    )
    for name, steps_yaml in workflows.items():
        _write_workflow(
            tmp_path / f"{name}.yaml",
            name=name,
            steps_yaml=steps_yaml,
            with_activity="activity:" in steps_yaml,
        )
    workflow_entries = "\n".join(f"  - {{ id: {name}, path: {name}.yaml }}" for name in workflows)
    target_list = "\n".join(f"        - {name}" for name in target_workflows)
    (tmp_path / "typeflux.project.yaml").write_text(
        'version: "1"\n'
        "name: proj\n"
        "workflows:\n"
        f"{workflow_entries}\n"
        "environments:\n"
        "  local: environments/local.yaml\n"
        "policies:\n"
        "  comp: policies/comp.yaml\n"
        "validation:\n"
        "  targets:\n"
        "    local:\n"
        "      environment: local\n"
        "      workflows:\n"
        f"{target_list}\n"
        "      policies:\n"
        "        - comp\n",
        encoding="utf-8",
    )
    return tmp_path / "typeflux.project.yaml"


def _closure_check(manifest: Path, parent_id: str):
    project = load_project_spec(manifest)
    resolved = resolve_project_workflow(project, workflow_id=parent_id, environment_id="local")
    policy = compose_project_policies(project, ("comp",))
    return validate_subworkflow_closure_policy(
        project=project, resolved=resolved, policy=policy, environment_id="local"
    )


def test_closure_member_violation_fails_the_parent(tmp_path: Path) -> None:
    # Parent has 1 step (passes max_steps=3); the child has 5 (fails). The parent's own
    # _validate_composition passes, but the closure walk admits the child under the
    # parent's policy and the child's overflow fails the parent's closure check.
    manifest = _build_project(
        tmp_path,
        composition="  max_steps: 3",
        workflows={
            "parent": "    - { id: call, workflow: child }",
            "child": _leaf_activity_steps(5),
        },
        target_workflows=["parent", "child"],
    )
    check = _closure_check(manifest, "parent")
    assert check is not None
    assert check.status == "failed"
    assert "sub-workflow 'child'" in (check.message or "")
    assert "policy_composition_ceilings" in (check.message or "")


def test_subworkflow_depth_ceiling(tmp_path: Path) -> None:
    # root -> mid -> leaf is depth 2 (root is depth 0). A ceiling of 1 fails naming the
    # deepest chain; a ceiling of 2 admits it.
    workflows = {
        "root": "    - { id: call, workflow: mid }",
        "mid": "    - { id: call, workflow: leaf }",
        "leaf": "    - { id: s0, activity: a }",
    }
    reject_dir = tmp_path / "reject"
    reject_dir.mkdir()
    reject = _build_project(
        reject_dir,
        composition="  max_subworkflow_depth: 1",
        workflows=workflows,
        target_workflows=["root", "mid", "leaf"],
    )
    check = _closure_check(reject, "root")
    assert check is not None and check.status == "failed"
    assert "depth 2 exceeds composition ceiling 1" in (check.message or "")
    assert "root -> mid -> leaf" in (check.message or "")

    allow_dir = tmp_path / "allow"
    allow_dir.mkdir()
    allow = _build_project(
        allow_dir,
        composition="  max_subworkflow_depth: 2",
        workflows=workflows,
        target_workflows=["root", "mid", "leaf"],
    )
    ok = _closure_check(allow, "root")
    assert ok is not None and ok.status == "passed"
    assert ok.details["subworkflow_depth"] == 2


def test_max_total_steps_sums_across_the_closure(tmp_path: Path) -> None:
    # Decomposition evasion (#298 review): each member individually passes the
    # per-member max_steps bound, but the TREE-WIDE sum exceeds max_total_steps —
    # the closure walk rejects the composed program, naming the per-member breakdown.
    workflows = {
        "parent": "    - { id: c1, workflow: childa }\n    - { id: c2, workflow: childb }",
        "childa": _leaf_activity_steps(4),
        "childb": _leaf_activity_steps(4),
    }
    reject_dir = tmp_path / "reject"
    reject_dir.mkdir()
    reject = _build_project(
        reject_dir,
        # Each member is <= 4 steps (parent 2, children 4+4); the sum is 10.
        composition="  max_steps: 4\n  max_total_steps: 9",
        workflows=workflows,
        target_workflows=["parent", "childa", "childb"],
    )
    check = _closure_check(reject, "parent")
    assert check is not None and check.status == "failed"
    assert "total step count 10 exceeds composition ceiling max_total_steps 9" in (
        check.message or ""
    )
    assert "childa: 4" in (check.message or "")
    assert check.details["closure_total_steps"] == 10

    allow_dir = tmp_path / "allow"
    allow_dir.mkdir()
    allow = _build_project(
        allow_dir,
        composition="  max_steps: 4\n  max_total_steps: 10",
        workflows=workflows,
        target_workflows=["parent", "childa", "childb"],
    )
    ok = _closure_check(allow, "parent")
    assert ok is not None and ok.status == "passed"
    assert ok.details["closure_total_steps"] == 10


def test_deploy_gate_rejects_composition_ceiling_violation(tmp_path: Path) -> None:
    # The deploy admission gate (_admit_policy → _deployment_policy_checks →
    # validate_project_policy) fails closed when the workflow overflows a ceiling,
    # proving the new check composes into the deploy check-report shape (#298 gate 4).
    manifest = _build_project(
        tmp_path,
        composition="  max_steps: 2",
        workflows={"solo": _leaf_activity_steps(4)},
        target_workflows=["solo"],
    )
    project = load_project_spec(manifest)
    resolved = resolve_project_workflow(project, workflow_id="solo", environment_id="local")
    with pytest.raises(ProjectDeploymentError) as exc:
        _admit_policy(project, resolved, explicit_policy_ids=("comp",))
    assert "policy_composition_ceilings" in str(exc.value)
