"""Composition ceilings policy dimension (#298 Phase A) — Python edition.

Covers each ceiling's reject + pass, the most-restrictive (min / AND) merge across
composed policies, the nesting>3 policy-load error, and the map-over-workflow gate.
Closure-member and sub-workflow-depth enforcement live in
``test_composition_closure_depth`` below (they need the transitive walk).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from typeflux.project.policy import (
    ComposedProjectPolicy,
    TypefluxProjectPolicySpec,
    _merge_policy_payloads,
)
from typeflux.project.policy_enforcement import (
    _collect_composition_metrics,
    _validate_composition,
)
from typeflux.yaml.spec import TypefluxYamlSpec


def _spec(steps: list[dict[str, Any]], *, with_map_workflow: bool = False) -> TypefluxYamlSpec:
    return TypefluxYamlSpec.model_validate(
        {
            "project": "p",
            "name": "n",
            "task_queue": "q",
            "runtime": {
                "registry": {"type": "inline", "prompts": {"p/x": "hi"}},
                "provider": {"type": "openai", "model": "gpt-4o-mini"},
            },
            "activities": {
                "definitions": [
                    {"name": "a", "input": "schemas:In", "output": "schemas:Out", "prompt": "p/x"}
                ]
            },
            "workflow": {
                "name": "W",
                "input": "schemas:In",
                "output": "schemas:Out",
                "steps": steps,
            },
        }
    )


def _policy(composition: dict[str, Any]) -> ComposedProjectPolicy:
    return ComposedProjectPolicy(
        selected_policy_ids=("c",),
        applied_policy_ids=("c",),
        policy_names=("c",),
        policy_hash="h",
        payload={"composition": composition},
    )


def _check(spec: TypefluxYamlSpec, composition: dict[str, Any]):
    return _validate_composition(SimpleNamespace(spec=spec), _policy(composition).payload)


_MAP_WF = {
    "id": "m",
    "map": {
        "workflow": "child",
        "over": "input.items",
        "concurrency": 2,
        "collect": {"output": "schemas:Out", "field": "result"},
    },
}
_PARALLEL = {
    "id": "par",
    "parallel": {
        "branches": [
            {"id": "b1", "steps": [{"id": "b1s", "activity": "a"}]},
            {"id": "b2", "steps": [{"id": "b2s", "activity": "a"}]},
            {"id": "b3", "steps": [{"id": "b3s", "activity": "a"}]},
        ],
        "collect": {"output": "schemas:Out"},
    },
}


def test_unset_composition_skips() -> None:
    result = _check(_spec([{"id": "s", "activity": "a"}]), {})
    assert result.status == "skipped"


def test_max_steps_reject_and_pass() -> None:
    spec = _spec([{"id": f"s{i}", "activity": "a"} for i in range(4)])
    rejected = _check(spec, {"max_steps": 3})
    assert rejected.status == "failed"
    assert "flattened step count 4 exceeds composition ceiling 3" in (rejected.message or "")
    assert _check(spec, {"max_steps": 4}).status == "passed"


def test_max_total_steps_bounds_a_single_workflow() -> None:
    # For a non-composed workflow (no closure check emitted), the tree total IS the
    # workflow's own flattened count, enforced here; the closure walk owns the
    # composed-tree sum (test_composition_closure_depth).
    spec = _spec([{"id": f"s{i}", "activity": "a"} for i in range(4)])
    rejected = _check(spec, {"max_total_steps": 3})
    assert rejected.status == "failed"
    assert "tree-wide composition ceiling max_total_steps 3" in (rejected.message or "")
    assert _check(spec, {"max_total_steps": 4}).status == "passed"


def test_max_parallel_width_reject_and_pass() -> None:
    spec = _spec([{"id": "s0", "activity": "a"}, _PARALLEL])
    rejected = _check(spec, {"max_parallel_width": 2})
    assert rejected.status == "failed"
    assert "'par'" in (rejected.message or "") and "width 3" in (rejected.message or "")
    assert _check(spec, {"max_parallel_width": 3}).status == "passed"


def test_max_parallel_nesting_reject_and_pass() -> None:
    nested = {
        "id": "outer",
        "parallel": {
            "branches": [{"id": "ob", "steps": [_PARALLEL]}],
            "collect": {"output": "schemas:Out"},
        },
    }
    spec = _spec([nested])  # nesting depth 2
    rejected = _check(spec, {"max_parallel_nesting": 1})
    assert rejected.status == "failed"
    assert "nesting depth 2" in (rejected.message or "")
    assert _check(spec, {"max_parallel_nesting": 2}).status == "passed"


def test_allow_map_over_workflow_gate() -> None:
    spec = _spec([{"id": "s0", "activity": "a"}, _MAP_WF])
    rejected = _check(spec, {"allow_map_over_workflow": False})
    assert rejected.status == "failed"
    assert "'m'" in (rejected.message or "")
    assert _check(spec, {"allow_map_over_workflow": True}).status == "passed"
    # A workflow with no map.workflow passes a forbidding policy.
    assert (
        _check(_spec([{"id": "s", "activity": "a"}]), {"allow_map_over_workflow": False}).status
        == "passed"
    )


def test_flattened_count_includes_parallel_branch_steps() -> None:
    # 1 leaf + 1 parallel container + 3 branch steps = 5 flattened nodes.
    metrics = _collect_composition_metrics(
        _spec([{"id": "s0", "activity": "a"}, _PARALLEL]).workflow.steps
    )
    assert metrics.flattened_step_count == 5
    assert metrics.max_parallel_width == 3
    assert metrics.max_parallel_nesting == 1


def test_merge_is_most_restrictive() -> None:
    a = TypefluxProjectPolicySpec.model_validate(
        {
            "name": "a",
            "composition": {
                "max_steps": 10,
                "max_parallel_width": 4,
                "allow_map_over_workflow": True,
            },
        }
    )
    b = TypefluxProjectPolicySpec.model_validate(
        {
            "name": "b",
            "composition": {
                "max_steps": 6,
                "max_parallel_width": 8,
                "allow_map_over_workflow": False,
            },
        }
    )
    pa = a.to_payload(include_extends=False)
    pb = b.to_payload(include_extends=False)
    for key in ("version", "name", "description"):
        pa.pop(key, None)
        pb.pop(key, None)
    merged = _merge_policy_payloads(pa, pb, path="x")["composition"]
    # max_* → min; allow_map_over_workflow → AND.
    assert merged == {"max_steps": 6, "max_parallel_width": 4, "allow_map_over_workflow": False}


def test_max_total_steps_merges_to_min() -> None:
    a = TypefluxProjectPolicySpec.model_validate(
        {"name": "a", "composition": {"max_total_steps": 100}}
    )
    b = TypefluxProjectPolicySpec.model_validate(
        {"name": "b", "composition": {"max_total_steps": 40}}
    )
    pa = a.to_payload(include_extends=False)
    pb = b.to_payload(include_extends=False)
    for key in ("version", "name", "description"):
        pa.pop(key, None)
        pb.pop(key, None)
    merged = _merge_policy_payloads(pa, pb, path="x")["composition"]
    assert merged == {"max_total_steps": 40}


def test_nesting_above_hard_ceiling_is_load_error() -> None:
    with pytest.raises(Exception) as exc:
        TypefluxProjectPolicySpec.model_validate(
            {"name": "x", "composition": {"max_parallel_nesting": 4}}
        )
    assert "may only tighten" in str(exc.value)
    # <= the hard ceiling is accepted.
    TypefluxProjectPolicySpec.model_validate(
        {"name": "x", "composition": {"max_parallel_nesting": 3}}
    )


def test_zero_ceiling_rejection_names_the_alternatives() -> None:
    # >= 1 by contract: 0 is never a silent no-op, and the error names the dedicated
    # spellings for "forbid" (allow_map_over_workflow: false; omit from targets).
    for field in ("max_steps", "max_total_steps", "max_parallel_width", "max_subworkflow_depth"):
        with pytest.raises(Exception) as exc:
            TypefluxProjectPolicySpec.model_validate({"name": "x", "composition": {field: 0}})
        assert "allow_map_over_workflow: false" in str(exc.value), field
        assert "validation targets" in str(exc.value), field


def test_composition_unset_does_not_change_policy_hash(tmp_path: Path) -> None:
    # composition defaults are dropped by to_payload, so a policy that declares none
    # composes to the byte-identical hash it did before the dimension existed.
    plain = TypefluxProjectPolicySpec.model_validate(
        {"name": "p", "providers": {"allowed": {"openai": {"models": ["gpt-4o-mini"]}}}}
    )
    assert "composition" not in plain.to_payload()
