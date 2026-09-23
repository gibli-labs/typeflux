"""Composition contract obligations (#55): the topology DTO round-trips the
composition shapes (both editions share one schema; the Python generator emits them
since slice 2 — see test_yaml_composition.py), and the YAML loader stub-rejects the
still-unwired sub-workflow keys with pointer errors instead of generic
unrecognized-key noise.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from typeflux.project.bundle import (
    BundleTopology,
    BundleTopologyEdge,
    BundleTopologyNode,
)
from typeflux.yaml.spec import WorkflowSpec

_CONFORMANCE_FIXTURES = (
    Path(__file__).resolve().parents[3] / "contracts" / "controlplane" / "conformance" / "fixtures"
)


def test_conformance_composition_topology_is_byte_identical_across_editions() -> None:
    # The recorded bundle-workflow-composition case pins BOTH editions' bundles
    # (base = python-cp, editions.ts-cp = the TS server). Everything
    # edition-native (schema identity, digests) legitimately diverges — the
    # composition TOPOLOGY is the cross-edition normative artifact (#55 §7) and
    # must stay byte-identical, re-recordings included.
    case = json.loads((_CONFORMANCE_FIXTURES / "bundle-workflow-composition.json").read_text())
    base = case["response"]["body"]["topology"]
    ts = case["editions"]["ts-cp"]["response"]["body"]["topology"]
    assert json.dumps(base, sort_keys=True) == json.dumps(ts, sort_keys=True)
    # And the shapes are really there: a parallel node plus all three new edge kinds.
    assert {node["kind"] for node in base["nodes"]} == {"activity", "map", "parallel"}
    assert {edge["kind"] for edge in base["edges"]} == {
        "sequential",
        "branch",
        "collect",
        "conditional",
    }


def _composition_topology() -> dict:
    """The TS resolver's projection of the design's §3.1 shape (bundle-topology.ts)."""
    return {
        "nodes": [
            {"id": "classify", "kind": "activity", "activity": "classify_disclosure"},
            {"id": "reviews", "kind": "parallel"},
            {"id": "legal_screen", "kind": "activity", "activity": "legal_screen"},
            {"id": "medical_review", "kind": "map", "activity": "medical_review"},
            {"id": "consolidate", "kind": "activity", "activity": "consolidate_reviews"},
        ],
        "edges": [
            {"source": "classify", "target": "reviews", "kind": "sequential"},
            {
                "source": "reviews",
                "target": "legal_screen",
                "kind": "branch",
                "condition": "classify.needs_legal == true",
            },
            {"source": "legal_screen", "target": "reviews", "kind": "collect"},
            {"source": "reviews", "target": "medical_review", "kind": "branch"},
            {"source": "medical_review", "target": "reviews", "kind": "collect"},
            {
                "source": "reviews",
                "target": "consolidate",
                "kind": "conditional",
                "condition": 'classify.route == "consolidate"',
            },
        ],
    }


def test_topology_dto_round_trips_composition_shapes() -> None:
    topology = BundleTopology.model_validate(_composition_topology())
    assert topology.nodes[1].kind == "parallel"
    assert topology.nodes[1].activity is None
    assert {edge.kind for edge in topology.edges} == {
        "sequential",
        "branch",
        "collect",
        "conditional",
    }
    # Round-trip: dump -> validate -> dump is a fixed point.
    dumped = topology.model_dump(mode="json")
    assert BundleTopology.model_validate(dumped).model_dump(mode="json") == dumped


def test_topology_dto_omits_absent_activity_under_exclude_none() -> None:
    # The bundle wire shape uses exclude_none (ResolvedWorkflowBundle.to_dict), so a
    # parallel node serializes WITHOUT an activity key — matching the TS projection.
    node = BundleTopologyNode(id="reviews", kind="parallel")
    assert node.model_dump(mode="json", exclude_none=True) == {"id": "reviews", "kind": "parallel"}
    # V1 nodes keep the key.
    v1 = BundleTopologyNode(id="s", kind="activity", activity="a")
    assert v1.model_dump(mode="json", exclude_none=True) == {
        "id": "s",
        "kind": "activity",
        "activity": "a",
    }


def test_topology_dto_accepts_workflow_node_kind() -> None:
    # Slice 3 (#55 §7): a sub-workflow node is `kind="workflow"` carrying the child's
    # MANIFEST id under `workflow`; it calls no PARENT activity, so `activity` is absent.
    node = BundleTopologyNode(id="assess", kind="workflow", workflow="assess_claim")
    assert node.model_dump(mode="json", exclude_none=True) == {
        "id": "assess",
        "kind": "workflow",
        "workflow": "assess_claim",
    }


def test_topology_dto_rejects_unknown_kinds() -> None:
    with pytest.raises(ValidationError):
        BundleTopologyNode(id="x", kind="loop")
    with pytest.raises(ValidationError):
        BundleTopologyEdge(source="a", target="b", kind="joins")


_STEP = {"id": "s1", "activity": "classify"}


def _workflow(steps: list[dict]) -> dict:
    return {"name": "W", "input": "schemas:In", "output": "schemas:Out", "steps": steps}


def test_loader_accepts_parallel_and_when_since_slice_2() -> None:
    # Slice 2 (#55): the composition surface is WIRED — the slice-1 stub
    # rejections are gone and the keys parse into the step models.
    spec = WorkflowSpec.model_validate(
        _workflow(
            [
                _STEP,
                {"id": "s2", "activity": "deep", "when": {"path": "s1.severity", "gte": 3}},
                {
                    "id": "reviews",
                    "parallel": {
                        "branches": [
                            {"id": "legal", "steps": [{"id": "screen", "activity": "screen"}]}
                        ],
                        "collect": {"output": "schemas:X"},
                    },
                },
            ]
        )
    )
    assert [step.id for step in spec.steps] == ["s1", "s2", "reviews"]


def test_loader_accepts_workflow_steps_since_slice_3() -> None:
    # Slice 3 (#55 §3.4): the `workflow:` surface is WIRED — the slice-2 stub rejection
    # is gone and the key parses into the sub-workflow step model. (The project-manifest
    # requirement is enforced at graph build, not the loader — see test_yaml_composition.)
    spec = WorkflowSpec.model_validate(_workflow([_STEP, {"id": "s2", "workflow": "child"}]))
    assert [step.id for step in spec.steps] == ["s1", "s2"]


def test_loader_accepts_map_workflow_and_rejects_both_targets() -> None:
    step = {
        "id": "fan",
        "map": {
            "workflow": "child",
            "over": "s1.items",
            "concurrency": 2,
            "collect": {"output": "schemas:X", "field": "items"},
        },
    }
    spec = WorkflowSpec.model_validate(_workflow([_STEP, step]))
    assert spec.steps[1].map.workflow == "child"
    # XOR: a map with both `activity` and `workflow` rejects at load (#55 §3.4).
    with pytest.raises(ValidationError, match=r"exactly one"):
        WorkflowSpec.model_validate(
            _workflow([_STEP, {"id": "fan", "map": {**step["map"], "activity": "a"}}])
        )


def test_v1_steps_still_load() -> None:
    spec = WorkflowSpec.model_validate(
        _workflow(
            [
                _STEP,
                {
                    "id": "fan",
                    "map": {
                        "activity": "assess",
                        "over": "s1.items",
                        "concurrency": 2,
                        "collect": {"output": "schemas:X", "field": "items"},
                    },
                },
            ]
        )
    )
    assert [step.id for step in spec.steps] == ["s1", "fan"]
