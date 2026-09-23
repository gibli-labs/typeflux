"""Composition slice 2 (#55): the Python generator's `parallel:` blocks + `when:`
gating — spec parsing (incl. every load-time rejection), call-spec building, the
real-type validation rules (merge/gate/optionality, when-path roots, gate-root
guaranteed-on-every-path, review-route tails), interpreter semantics (concurrent
branches, collect merge, skip/early-exit, payload guard, per-branch event
subsequences, unit totals), and the digest-stability regressions that pin V1 specs
byte-identical through the new code paths. Mirrors the TS slice-1 coverage
(composition-spec.test.ts + workflows-composition.test.ts) behaviorally.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from textwrap import dedent, indent
from typing import Any

import pytest
from pydantic import ValidationError

from typeflux.yaml import collect_activities, create_workflow, load_yaml_spec
from typeflux.yaml.spec import WorkflowSpec
from typeflux.yaml.workflow import (
    ParallelCallSpec,
    WhenGate,
    WhenLeaf,
    flatten_call_specs,
    render_when_gate,
)

# ---------------------------------------------------------------------------
# Fixture project (the design's §3.1 disclosure-review shape, TS test parity)
# ---------------------------------------------------------------------------

PROJECT = "yaml_composition_project"


def _write_composition_project(tmp_path: Path) -> None:
    for name in tuple(sys.modules):
        if name == PROJECT or name.startswith(f"{PROJECT}."):
            del sys.modules[name]
    package = tmp_path / PROJECT
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "schemas.py").write_text(
        dedent(
            """
            from pydantic import BaseModel


            class Attachment(BaseModel):
                name: str


            class Disclosure(BaseModel):
                doc: str
                attachments: list[Attachment] = []


            class Classification(BaseModel):
                needs_legal: bool
                severity: int
                risk: float
                route: str


            class LegalScreen(BaseModel):
                escalate: bool
                screen: str


            class LegalAssessment(BaseModel):
                assessment: str


            class MedicalReview(BaseModel):
                review: str


            class ReviewBundle(BaseModel):
                legal: LegalAssessment | None
                medical: MedicalReview


            class BothOptionalBundle(BaseModel):
                legal: LegalAssessment | None
                medical: MedicalReview | None


            class NonOptionalBundle(BaseModel):
                legal: LegalAssessment
                medical: MedicalReview


            class MisnamedBundle(BaseModel):
                legal: LegalAssessment | None
                surgical: MedicalReview


            class ScreenBundle(BaseModel):
                legal: LegalScreen
                medical: MedicalReview


            class ReviewOutcome(BaseModel):
                outcome: str


            class OutcomeBatch(BaseModel):
                outcomes: list[ReviewOutcome]
            """
        ),
        encoding="utf-8",
    )


COMPOSITION_STEPS = """\
- id: classify
  activity: classify_disclosure
- id: reviews
  parallel:
    branches:
      - id: legal
        when: { path: classify.needs_legal, eq: true }
        steps:
          - id: legal_screen_step
            activity: legal_screen
          - id: legal_assess_step
            activity: legal_assess
      - id: medical
        steps:
          - id: medical_review_step
            activity: medical_review
    collect:
      output: schemas:ReviewBundle
      max_bytes: 1500000
- id: consolidate
  activity: consolidate_reviews
"""


def _yaml_text(
    steps: str = COMPOSITION_STEPS, *, lifecycle: str = "", output: str = "schemas:ReviewOutcome"
) -> str:
    lifecycle_block = indent(lifecycle, "  ") if lifecycle else ""
    header = dedent(
        f"""\
        project: {PROJECT}
        name: disclosure_review
        task_queue: composition-queue
        runtime:
          temporal:
            address: localhost:7233
          registry:
            type: inline
            prompts:
              p: prompt {{{{doc}}}}
          provider:
            type: fake
        activities:
          definitions:
            - {{ name: classify_disclosure, input: "schemas:Disclosure", output: "schemas:Classification", prompt: p }}
            - {{ name: legal_screen, input: "schemas:Classification", output: "schemas:LegalScreen", prompt: p }}
            - {{ name: legal_assess, input: "schemas:LegalScreen", output: "schemas:LegalAssessment", prompt: p }}
            - {{ name: medical_review, input: "schemas:Classification", output: "schemas:MedicalReview", prompt: p }}
            - {{ name: consolidate_reviews, input: "schemas:ReviewBundle", output: "schemas:ReviewOutcome", prompt: p }}
            - {{ name: consolidate_screen, input: "schemas:ScreenBundle", output: "schemas:ReviewOutcome", prompt: p }}
            - {{ name: legal_rescreen_activity, input: "schemas:LegalScreen", output: "schemas:LegalScreen", prompt: p }}
            - {{ name: deep_analysis, input: "schemas:ReviewOutcome", output: "schemas:ReviewOutcome", prompt: p }}
            - {{ name: to_batch, input: "schemas:Classification", output: "schemas:OutcomeBatch", prompt: p }}
            - {{ name: review_attachment, input: "schemas:Attachment", output: "schemas:ReviewOutcome", prompt: p }}
        workflow:
          name: DisclosureReviewWorkflow
          input: schemas:Disclosure
          output: {output}
        """
    )
    return header + lifecycle_block + "  steps:\n" + indent(steps, "    ")


def _build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, yaml_text: str) -> type:
    _write_composition_project(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    path = tmp_path / "typeflux.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    spec = load_yaml_spec(path, load_dotenv=False)
    return create_workflow(spec, collect_activities(spec))


def _calls(workflow_cls: type) -> tuple:
    return getattr(workflow_cls, "__typeflux_activity_calls__")


# ---------------------------------------------------------------------------
# Spec parsing + call-spec building
# ---------------------------------------------------------------------------


def test_builds_composition_call_specs_with_normalized_when(tmp_path, monkeypatch) -> None:
    workflow_cls = _build(tmp_path, monkeypatch, _yaml_text())
    calls = _calls(workflow_cls)
    assert [type(call).__name__ for call in calls] == [
        "ActivityCallSpec",
        "ParallelCallSpec",
        "ActivityCallSpec",
    ]
    block = calls[1]
    assert isinstance(block, ParallelCallSpec)
    assert [branch.branch_id for branch in block.branches] == ["legal", "medical"]
    assert block.branches[0].when == WhenGate(
        mode="leaf",
        predicates=(WhenLeaf(path="classify.needs_legal", op="eq", value=True),),
    )
    assert block.branches[1].when is None
    assert [call.step_id for call in block.branches[0].calls] == [
        "legal_screen_step",
        "legal_assess_step",
    ]
    assert block.collect.output_type.__name__ == "ReviewBundle"
    assert block.collect.max_bytes == 1_500_000
    # Leaf flattening (used by every per-activity projection).
    assert [call.step_id for call in flatten_call_specs(calls)] == [
        "classify",
        "legal_screen_step",
        "legal_assess_step",
        "medical_review_step",
        "consolidate",
    ]


def test_collect_max_bytes_defaults_on_and_zero_disables(tmp_path, monkeypatch) -> None:
    defaulted = _build(
        tmp_path,
        monkeypatch,
        _yaml_text(COMPOSITION_STEPS.replace("\n      max_bytes: 1500000", "")),
    )
    assert _calls(defaulted)[1].collect.max_bytes == 1_500_000
    disabled = _build(
        tmp_path,
        monkeypatch,
        _yaml_text(COMPOSITION_STEPS.replace("max_bytes: 1500000", "max_bytes: 0")),
    )
    assert _calls(disabled)[1].collect.max_bytes == 0


def test_normalizes_all_any_composition_and_every_operator(tmp_path, monkeypatch) -> None:
    steps = COMPOSITION_STEPS.replace(
        "when: { path: classify.needs_legal, eq: true }",
        "when: { any: [ { path: classify.risk, gte: 0.5 }, "
        "{ path: classify.route, in: [legal, mixed] }, "
        "{ path: classify.needs_legal, exists: true } ] }",
    )
    workflow_cls = _build(tmp_path, monkeypatch, _yaml_text(steps))
    gate = _calls(workflow_cls)[1].branches[0].when
    assert gate == WhenGate(
        mode="any",
        predicates=(
            WhenLeaf(path="classify.risk", op="gte", value=0.5),
            WhenLeaf(path="classify.route", op="in", value=("legal", "mixed")),
            WhenLeaf(path="classify.needs_legal", op="exists", value=True),
        ),
    )
    assert render_when_gate(gate) == (
        'any(classify.risk >= 0.5, classify.route in ["legal","mixed"], '
        "classify.needs_legal exists)"
    )


def test_when_rendering_matches_the_cross_edition_condition_text() -> None:
    # The rendered text is cross-edition wire data (step_skipped events + topology
    # condition edges) — pin the exact strings the TS renderPlanWhen produces.
    cases = [
        (WhenLeaf("a.b", "eq", True), "a.b == true"),
        (WhenLeaf("a.b", "eq", None), "a.b == null"),
        (WhenLeaf("a.b", "neq", "x"), 'a.b != "x"'),
        (WhenLeaf("a.b", "lt", 0.3), "a.b < 0.3"),
        (WhenLeaf("a.b", "lte", 3), "a.b <= 3"),
        (WhenLeaf("a.b", "gt", 3.0), "a.b > 3"),
        (WhenLeaf("a.b", "gte", 3), "a.b >= 3"),
        (WhenLeaf("a.b", "in", ("x", 1, True)), 'a.b in ["x",1,true]'),
        (WhenLeaf("a.b", "exists", True), "a.b exists"),
        (WhenLeaf("a.b", "exists", False), "a.b not exists"),
    ]
    for leaf, expected in cases:
        assert render_when_gate(WhenGate(mode="leaf", predicates=(leaf,))) == expected
    assert (
        render_when_gate(WhenGate(mode="all", predicates=(cases[0][0], cases[3][0])))
        == "all(a.b == true, a.b < 0.3)"
    )
    # Number formatting follows ECMAScript Number::toString exactly (verified
    # against `node -e 'console.log(JSON.stringify(v))'`): fixed notation for
    # decimal exponents in (-6, 21], unpadded exponential otherwise — Python's
    # repr() would diverge on all of these.
    js_numbers = [
        (0.00001, "0.00001"),
        (0.000001, "0.000001"),
        (1e-7, "1e-7"),
        (2.5e-8, "2.5e-8"),
        (1e16, "10000000000000000"),
        (1.5e21, "1.5e+21"),
        (-0.00001, "-0.00001"),
        (10**21, "1e+21"),
    ]
    for literal, rendered in js_numbers:
        gate = WhenGate(mode="leaf", predicates=(WhenLeaf("a.b", "lt", literal),))
        assert render_when_gate(gate) == f"a.b < {rendered}"


def test_when_exists_reads_declared_fields_only() -> None:
    # TS resolves wire objects with Object.hasOwn, so only DATA resolves; a
    # Python model attribute that is code (`copy`, `model_dump`) must not read
    # as existing (verifier finding, #55 slice 2).
    from pydantic import BaseModel

    from typeflux.yaml.workflow import evaluate_when_gate

    class Result(BaseModel):
        flag: bool

    context = {"step": Result(flag=True)}

    def exists(path: str) -> bool:
        return evaluate_when_gate(
            WhenGate(mode="leaf", predicates=(WhenLeaf(path, "exists", True),)), context
        )

    assert exists("step.flag") is True
    assert exists("step.copy") is False
    assert exists("step.model_dump") is False
    assert exists("step.missing") is False
    # A non-exists operator over a non-field raises loudly instead of comparing code.
    with pytest.raises(KeyError):
        evaluate_when_gate(
            WhenGate(mode="leaf", predicates=(WhenLeaf("step.model_dump", "eq", "x"),)),
            context,
        )


_STEP = {"id": "s1", "activity": "classify"}


def _workflow_dict(steps: list[dict]) -> dict:
    return {"name": "W", "input": "schemas:In", "output": "schemas:Out", "steps": steps}


def test_rejects_a_leaf_with_zero_or_two_operators() -> None:
    with pytest.raises(ValidationError, match="exactly one operator"):
        WorkflowSpec.model_validate(
            _workflow_dict([_STEP, {"id": "s2", "activity": "a", "when": {"path": "s1.x"}}])
        )
    with pytest.raises(ValidationError, match="exactly one operator"):
        WorkflowSpec.model_validate(
            _workflow_dict(
                [
                    _STEP,
                    {"id": "s2", "activity": "a", "when": {"path": "s1.x", "eq": True, "gte": 1}},
                ]
            )
        )


def test_rejects_boolean_literals_on_ordering_operators() -> None:
    # TS parity (whenOrderable = number | string): Pydantic's lax coercion would
    # otherwise silently read `lt: true` as 1.
    with pytest.raises(ValidationError, match="require a number or string"):
        WorkflowSpec.model_validate(
            _workflow_dict(
                [_STEP, {"id": "s2", "activity": "a", "when": {"path": "s1.x", "lt": True}}]
            )
        )


def test_eq_null_is_a_real_operator() -> None:
    spec = WorkflowSpec.model_validate(
        _workflow_dict([_STEP, {"id": "s2", "activity": "a", "when": {"path": "s1.x", "eq": None}}])
    )
    from typeflux.yaml.workflow import when_gate_from_spec

    gate = when_gate_from_spec(spec.steps[1].when)
    assert gate.predicates[0] == WhenLeaf(path="s1.x", op="eq", value=None)


def test_rejects_nested_all_any_one_composition_level() -> None:
    # Decision D1: one all/any level — a nested composition fails leaf validation.
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(
            _workflow_dict(
                [
                    _STEP,
                    {
                        "id": "s2",
                        "activity": "a",
                        "when": {"all": [{"any": [{"path": "s1.x", "gte": 0.5}]}]},
                    },
                ]
            )
        )


def test_rejects_when_predicate_with_the_named_injected_predicates_pointer() -> None:
    with pytest.raises(ValidationError, match="named injected predicates"):
        WorkflowSpec.model_validate(
            _workflow_dict(
                [_STEP, {"id": "s2", "activity": "a", "when": {"predicate": "legal_gate"}}]
            )
        )


def test_standalone_spec_rejects_workflow_ref_naming_the_project_requirement(
    tmp_path, monkeypatch
) -> None:
    # Slice 3 (#55 §1): a `workflow:` step parses (it is valid syntax now), but a spec
    # loaded STANDALONE (no project manifest) rejects at graph build naming the
    # project-manifest requirement — sub-workflow refs resolve through the project only.
    steps = (
        "- id: classify\n  activity: classify_disclosure\n- id: sub\n  workflow: sibling_review\n"
    )
    with pytest.raises(ValueError, match="loaded standalone"):
        _build(tmp_path, monkeypatch, _yaml_text(steps))
    # `map.workflow` fan-outs reject the same way when loaded standalone.
    map_steps = (
        "- id: fan\n"
        "  map:\n"
        "    workflow: sibling_review\n"
        "    over: input.attachments\n"
        "    concurrency: 2\n"
        "    collect: { output: schemas:OutcomeBatch, field: outcomes }\n"
    )
    with pytest.raises(ValueError, match="loaded standalone"):
        _build(tmp_path, monkeypatch, _yaml_text(map_steps, output="schemas:OutcomeBatch"))


def test_map_step_rejects_both_and_neither_activity_and_workflow() -> None:
    # A map step targets exactly one of `activity` / `workflow` (#55 §3.4 XOR).
    for target in ({"activity": "a", "workflow": "child"}, {}):
        with pytest.raises(ValidationError, match="exactly one"):
            WorkflowSpec.model_validate(
                _workflow_dict(
                    [
                        _STEP,
                        {
                            "id": "fan",
                            "map": {
                                **target,
                                "over": "s1.items",
                                "concurrency": 2,
                                "collect": {"output": "schemas:X", "field": "items"},
                            },
                        },
                    ]
                )
            )


def test_rejects_a_parallel_block_without_collect_or_with_empty_branches() -> None:
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(
            _workflow_dict(
                [
                    _STEP,
                    {
                        "id": "p",
                        "parallel": {
                            "branches": [{"id": "b", "steps": [{"id": "s", "activity": "a"}]}]
                        },
                    },
                ]
            )
        )
    with pytest.raises(ValidationError, match=r"parallel\.branches must not be empty"):
        WorkflowSpec.model_validate(
            _workflow_dict(
                [
                    _STEP,
                    {"id": "p", "parallel": {"branches": [], "collect": {"output": "schemas:X"}}},
                ]
            )
        )
    with pytest.raises(ValidationError, match="parallel branch steps must not be empty"):
        WorkflowSpec.model_validate(
            _workflow_dict(
                [
                    _STEP,
                    {
                        "id": "p",
                        "parallel": {
                            "branches": [{"id": "b", "steps": []}],
                            "collect": {"output": "schemas:X"},
                        },
                    },
                ]
            )
        )


def test_rejects_a_step_with_both_activity_and_parallel() -> None:
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(
            _workflow_dict(
                [
                    {
                        "id": "s",
                        "activity": "a",
                        "parallel": {
                            "branches": [{"id": "b", "steps": [{"id": "n", "activity": "a"}]}],
                            "collect": {"output": "schemas:X"},
                        },
                    }
                ]
            )
        )


def _parallel_steps(branch_id: str = "legal", nested_id: str = "medical_review_step") -> list[dict]:
    return [
        _STEP,
        {
            "id": "p",
            "parallel": {
                "branches": [
                    {"id": branch_id, "steps": [{"id": "b1", "activity": "a"}]},
                    {"id": "medical", "steps": [{"id": nested_id, "activity": "a"}]},
                ],
                "collect": {"output": "schemas:X"},
            },
        },
    ]


def test_enforces_the_flat_id_namespace_across_branches_and_nested_steps() -> None:
    with pytest.raises(ValidationError, match="duplicate workflow step id"):
        WorkflowSpec.model_validate(_workflow_dict(_parallel_steps(branch_id="s1")))
    with pytest.raises(ValidationError, match="duplicate workflow step id"):
        WorkflowSpec.model_validate(_workflow_dict(_parallel_steps(nested_id="b1")))
    with pytest.raises(ValidationError, match="reserved workflow step id"):
        WorkflowSpec.model_validate(_workflow_dict(_parallel_steps(nested_id="input")))


def test_rejects_parallel_nesting_beyond_3_with_the_subworkflow_hint() -> None:
    def nest(depth: int) -> dict:
        if depth == 0:
            return {"id": "leaf", "activity": "a"}
        return {
            "id": f"p{depth}",
            "parallel": {
                "branches": [{"id": f"b{depth}", "steps": [nest(depth - 1)]}],
                "collect": {"output": "schemas:X"},
            },
        }

    WorkflowSpec.model_validate(_workflow_dict([nest(3)]))
    with pytest.raises(ValidationError, match="deeper than 3.*sub-workflow"):
        WorkflowSpec.model_validate(_workflow_dict([nest(4)]))


def test_review_routes_target_top_level_steps_only() -> None:
    body = _workflow_dict(_parallel_steps())
    body["steps"].append({"id": "tail", "activity": "a"})
    body["lifecycle"] = {
        "enabled": True,
        "review": {
            "after_step": "s1",
            "user_decisions": {"approve": {"route": "b1"}},
        },
    }
    with pytest.raises(ValidationError, match="routes to unknown step"):
        WorkflowSpec.model_validate(body)


# ---------------------------------------------------------------------------
# Real-type validation (the Python-only discipline)
# ---------------------------------------------------------------------------


def test_rejects_a_when_path_whose_root_is_not_available_at_the_gate(tmp_path, monkeypatch) -> None:
    # A sibling branch's result is not addressable (it may not exist yet).
    sibling = _yaml_text(
        COMPOSITION_STEPS.replace("path: classify.needs_legal", "path: medical_review_step.flag")
    )
    with pytest.raises(ValueError, match="sibling-branch results are not addressable"):
        _build(tmp_path, monkeypatch, sibling)
    # A forward reference to a step that has not run.
    forward = _yaml_text(
        COMPOSITION_STEPS.replace("path: classify.needs_legal", "path: consolidate.flag")
    )
    with pytest.raises(ValueError, match="is not\\s+available there"):
        _build(tmp_path, monkeypatch, forward)
    # Own-branch earlier steps ARE available (a mid-branch gate over its own
    # branch's prior step; the branch terminal keeps the gate's type).
    _build(tmp_path, monkeypatch, _yaml_text(MID_BRANCH_GATE_STEPS))


def test_gated_branch_inner_results_are_not_roots_after_the_block(tmp_path, monkeypatch) -> None:
    def gate_after_block(path: str) -> str:
        # Gate a TAIL step (running value ReviewOutcome == workflow.output) so only
        # the root-availability rule is under test, not the gate-typing rule.
        return COMPOSITION_STEPS + (
            f"- id: deep_analysis_step\n  when: {{ path: {path}, exists: true }}\n  activity: deep_analysis\n"
        )

    # legal_screen_step lives in the when-gated legal branch: it may never have run
    # even though the block completed, so a later gate must not read it.
    with pytest.raises(ValueError, match="'legal_screen_step' is not\\s+available"):
        _build(tmp_path, monkeypatch, _yaml_text(gate_after_block("legal_screen_step.escalate")))
    # medical_review_step is in an UNGATED branch: guaranteed once the block completed.
    _build(tmp_path, monkeypatch, _yaml_text(gate_after_block("medical_review_step.review")))


def test_review_route_tail_gate_must_not_read_a_root_the_jump_skips(tmp_path, monkeypatch) -> None:
    steps = COMPOSITION_STEPS.replace(
        "- id: consolidate\n  activity: consolidate_reviews",
        "- id: consolidate\n  activity: consolidate_reviews\n"
        "- id: finalize\n  when: { path: consolidate.outcome, eq: done }\n  activity: deep_analysis",
    )
    yaml_text = _yaml_text(
        steps,
        lifecycle=(
            "lifecycle:\n"
            "  enabled: true\n"
            "  review:\n"
            "    after_step: reviews\n"
            "    user_decisions:\n"
            "      skip: { route: finalize }\n"
        ),
    )
    with pytest.raises(ValueError, match="routes past step 'consolidate'"):
        _build(tmp_path, monkeypatch, yaml_text)


GATED_TAIL_ROUTE_STEPS = """\
- id: classify
  activity: classify_disclosure
- id: to_batch
  activity: to_batch
- id: review_attachments
  when: { path: classify.needs_legal, eq: true }
  map:
    activity: review_attachment
    over: input.attachments
    concurrency: 2
    collect:
      output: schemas:OutcomeBatch
      field: outcomes
"""


def _gated_tail_route_yaml(route: str) -> str:
    return _yaml_text(
        GATED_TAIL_ROUTE_STEPS,
        lifecycle=(
            "lifecycle:\n"
            "  enabled: true\n"
            "  review:\n"
            "    after_step: classify\n"
            "    user_decisions:\n"
            f"      go: {{ route: {route} }}\n"
        ),
        output="schemas:OutcomeBatch",
    )


def test_review_route_into_a_gated_tail_must_exit_with_workflow_output(
    tmp_path, monkeypatch
) -> None:
    # Codex round: the normal path types OutcomeBatch -> gated map -> OutcomeBatch
    # (the main-walk gate check passes), but a route jumping from the
    # Classification checkpoint straight to the gated MAP step — which consumes
    # no running value, so the tail chain check cannot object — would, on a false
    # gate, complete the workflow with Classification despite
    # workflow.output: OutcomeBatch. Load-time rejection, exactly like the
    # top-level early-exit rule.
    with pytest.raises(TypeError, match="routes into a when-gated tail.*Classification"):
        _build(tmp_path, monkeypatch, _gated_tail_route_yaml("review_attachments"))
    # The accepting case: routing to to_batch restores OutcomeBatch before the gate.
    _build(tmp_path, monkeypatch, _gated_tail_route_yaml("to_batch"))


def test_gate_typing_top_level_gate_must_exit_with_workflow_output(tmp_path, monkeypatch) -> None:
    steps = COMPOSITION_STEPS.replace(
        "- id: consolidate\n  activity: consolidate_reviews",
        "- id: consolidate\n  when: { path: classify.route, eq: consolidate }\n  activity: consolidate_reviews",
    )
    with pytest.raises(TypeError, match="would complete the workflow with ReviewBundle"):
        _build(tmp_path, monkeypatch, _yaml_text(steps))


def test_gate_typing_mid_branch_gate_must_exit_with_branch_terminal(tmp_path, monkeypatch) -> None:
    # Gating legal_assess_step makes LegalScreen a potential branch result while
    # the branch terminates with LegalAssessment.
    steps = COMPOSITION_STEPS.replace(
        "- id: legal_assess_step\n            activity: legal_assess",
        "- id: legal_assess_step\n            when: { path: legal_screen_step.escalate, eq: true }\n            activity: legal_assess",
    )
    with pytest.raises(TypeError, match="potential result of branch 'legal'"):
        _build(tmp_path, monkeypatch, _yaml_text(steps))


def test_chain_step_after_the_block_consumes_the_collect_output(tmp_path, monkeypatch) -> None:
    steps = COMPOSITION_STEPS.replace(
        "output: schemas:ReviewBundle", "output: schemas:BothOptionalBundle"
    )
    with pytest.raises(TypeError, match="'consolidate' expects ReviewBundle"):
        _build(tmp_path, monkeypatch, _yaml_text(steps))


def test_chain_branch_first_steps_consume_the_block_input(tmp_path, monkeypatch) -> None:
    # Without classify, the block input is Disclosure but legal_screen expects
    # Classification. (The legal branch's gate is dropped too — its `classify`
    # root would otherwise fail the when-root check first.)
    steps = COMPOSITION_STEPS.replace(
        "- id: classify\n  activity: classify_disclosure\n", ""
    ).replace("        when: { path: classify.needs_legal, eq: true }\n", "")
    with pytest.raises(TypeError, match="'legal_screen_step' expects Classification"):
        _build(tmp_path, monkeypatch, _yaml_text(steps))


def test_merge_check_collect_fields_must_be_exactly_the_branch_ids(tmp_path, monkeypatch) -> None:
    steps = COMPOSITION_STEPS.replace(
        "output: schemas:ReviewBundle", "output: schemas:MisnamedBundle"
    )
    with pytest.raises(TypeError, match="fields must be exactly the branch ids"):
        _build(tmp_path, monkeypatch, _yaml_text(steps))


def test_merge_check_gated_branch_requires_an_optional_field(tmp_path, monkeypatch) -> None:
    steps = COMPOSITION_STEPS.replace(
        "output: schemas:ReviewBundle", "output: schemas:NonOptionalBundle"
    )
    with pytest.raises(TypeError, match="must be\\s+Optional"):
        _build(tmp_path, monkeypatch, _yaml_text(steps))


def test_merge_check_field_type_must_match_branch_terminal(tmp_path, monkeypatch) -> None:
    # The legal branch terminates with LegalScreen instead of LegalAssessment.
    steps = COMPOSITION_STEPS.replace(
        "- id: legal_assess_step\n            activity: legal_assess",
        "- id: legal_assess_step\n            activity: legal_rescreen_activity",
    )
    with pytest.raises(TypeError, match="collect field 'legal' expects LegalAssessment"):
        _build(tmp_path, monkeypatch, _yaml_text(steps))


# ---------------------------------------------------------------------------
# Interpreter semantics (in-process; live dev-server proof rides
# test_live_yaml_composition.py + the composition replay fixture)
# ---------------------------------------------------------------------------


def _register_activities(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any] | None = None,
) -> dict[str, list[Any]]:
    import temporalio.workflow

    calls: dict[str, list[Any]] = {}
    from yaml_composition_project.schemas import (  # type: ignore[import-not-found]
        Classification,
        LegalAssessment,
        LegalScreen,
        MedicalReview,
        ReviewOutcome,
    )

    defaults: dict[str, Any] = {
        "classify_disclosure": lambda arg: Classification(
            needs_legal=True, severity=1, risk=0.1, route="x"
        ),
        "legal_screen": lambda arg: LegalScreen(escalate=False, screen="legal-screened"),
        "legal_assess": lambda arg: LegalAssessment(assessment="legal-assessed"),
        "medical_review": lambda arg: MedicalReview(review="medical-reviewed"),
        "consolidate_reviews": lambda arg: ReviewOutcome(outcome="done"),
        "deep_analysis": lambda arg: ReviewOutcome(outcome="deep"),
    }
    defaults.update(overrides or {})

    async def fake_execute_activity(name: str, arg: Any, **kwargs: Any) -> Any:
        calls.setdefault(name, []).append(arg)
        result = defaults[name](arg)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    monkeypatch.setattr(temporalio.workflow, "execute_activity", fake_execute_activity)
    return calls


@pytest.mark.asyncio
async def test_parallel_branches_run_concurrently_and_collect_by_branch_id(
    tmp_path, monkeypatch
) -> None:
    workflow_cls = _build(tmp_path, monkeypatch, _yaml_text())
    from yaml_composition_project.schemas import Disclosure

    in_flight = {"active": 0, "max": 0}

    async def slow(result_factory):
        in_flight["active"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["active"])
        try:
            await asyncio.sleep(0.02)
            return result_factory()
        finally:
            in_flight["active"] -= 1

    from yaml_composition_project.schemas import LegalScreen, MedicalReview

    calls = _register_activities(
        monkeypatch,
        {
            "legal_screen": lambda arg: slow(
                lambda: LegalScreen(escalate=False, screen="legal-screened")
            ),
            "medical_review": lambda arg: slow(lambda: MedicalReview(review="medical-reviewed")),
        },
    )
    result = await workflow_cls().run(Disclosure(doc="d"))
    assert result.outcome == "done"
    # consolidate consumed the collect object: fields ARE the branch ids.
    bundle = calls["consolidate_reviews"][0]
    assert bundle.legal.assessment == "legal-assessed"
    assert bundle.medical.review == "medical-reviewed"
    assert list(type(bundle).model_fields) == ["legal", "medical"]
    # Branch first steps consumed the BLOCK input (classify's output).
    assert calls["legal_screen"][0].needs_legal is True
    assert calls["medical_review"][0].needs_legal is True
    # Chained branch step consumed its predecessor.
    assert calls["legal_assess"][0].screen == "legal-screened"
    # Both branches were genuinely in flight together.
    assert in_flight["max"] == 2


@pytest.mark.asyncio
async def test_gated_out_branch_contributes_none_and_records_step_skipped(
    tmp_path, monkeypatch
) -> None:
    workflow_cls = _build(
        tmp_path,
        monkeypatch,
        _yaml_text(
            COMPOSITION_STEPS, lifecycle="lifecycle:\n  enabled: true\n  cancellation: false\n"
        ),
    )
    from yaml_composition_project.schemas import Classification, Disclosure

    calls = _register_activities(
        monkeypatch,
        {
            "classify_disclosure": lambda arg: Classification(
                needs_legal=False, severity=1, risk=0.1, route="x"
            )
        },
    )
    instance = workflow_cls()
    await instance.run(Disclosure(doc="d"))
    bundle = calls["consolidate_reviews"][0]
    assert bundle.legal is None
    assert bundle.medical.review == "medical-reviewed"
    # The legal branch never ran.
    assert "legal_screen" not in calls
    status = instance._typeflux_lifecycle.status()
    skipped = [event for event in status.events if event.event == "step_skipped"]
    assert len(skipped) == 1
    assert skipped[0].step_id == "legal"
    assert skipped[0].condition == "classify.needs_legal == true"
    started = [event.step_id for event in status.events if event.event == "step_started"]
    assert "legal_screen_step" not in started
    # Skipped units released: progress completes at 100%.
    assert status.completed_units == status.total_units


@pytest.mark.asyncio
async def test_if_else_two_mutually_exclusive_gates_run_exactly_one_branch(
    tmp_path, monkeypatch
) -> None:
    steps = dedent(
        """\
        - id: classify
          activity: classify_disclosure
        - id: routing
          parallel:
            branches:
              - id: legal
                when: { path: classify.risk, lt: 0.3 }
                steps:
                  - id: fast_screen
                    activity: legal_screen
                  - id: fast_assess
                    activity: legal_assess
              - id: medical
                when: { path: classify.risk, gte: 0.3 }
                steps:
                  - id: full_review
                    activity: medical_review
            collect:
              output: schemas:BothOptionalBundle
        - id: consolidate
          activity: consolidate_reviews
        """
    )
    yaml_text = _yaml_text(steps).replace(
        'name: consolidate_reviews, input: "schemas:ReviewBundle"',
        'name: consolidate_reviews, input: "schemas:BothOptionalBundle"',
    )
    from yaml_composition_project.schemas import Classification, Disclosure

    for risk, ran, skipped in ((0.1, "legal", "medical"), (0.9, "medical", "legal")):
        workflow_cls = _build(tmp_path, monkeypatch, yaml_text)
        calls = _register_activities(
            monkeypatch,
            {
                "classify_disclosure": lambda arg, risk=risk: Classification(
                    needs_legal=True, severity=1, risk=risk, route="x"
                )
            },
        )
        await workflow_cls().run(Disclosure(doc="d"))
        bundle = calls["consolidate_reviews"][0]
        assert getattr(bundle, skipped) is None
        assert getattr(bundle, ran) is not None


@pytest.mark.asyncio
async def test_early_exit_false_top_level_gate_completes_with_the_running_value(
    tmp_path, monkeypatch
) -> None:
    steps = COMPOSITION_STEPS + (
        "- id: deep_analysis_step\n"
        "  when: { path: classify.severity, gte: 3 }\n"
        "  activity: deep_analysis\n"
    )
    workflow_cls = _build(
        tmp_path,
        monkeypatch,
        _yaml_text(steps, lifecycle="lifecycle:\n  enabled: true\n  cancellation: false\n"),
    )
    from yaml_composition_project.schemas import Disclosure

    calls = _register_activities(monkeypatch)
    instance = workflow_cls()
    result = await instance.run(Disclosure(doc="d"))
    # severity 1 < 3: deep_analysis is skipped; the workflow returns consolidate's output.
    assert result.outcome == "done"
    assert "deep_analysis" not in calls
    status = instance._typeflux_lifecycle.status()
    assert status.state == "completed"
    skipped = [event for event in status.events if event.event == "step_skipped"]
    assert len(skipped) == 1
    assert skipped[0].step_id == "deep_analysis_step"
    assert skipped[0].condition == "classify.severity >= 3"
    assert status.completed_units == status.total_units


@pytest.mark.asyncio
async def test_early_exit_gate_passing_runs_the_tail(tmp_path, monkeypatch) -> None:
    steps = COMPOSITION_STEPS + (
        "- id: deep_analysis_step\n"
        "  when: { path: classify.severity, gte: 3 }\n"
        "  activity: deep_analysis\n"
    )
    workflow_cls = _build(tmp_path, monkeypatch, _yaml_text(steps))
    from yaml_composition_project.schemas import Classification, Disclosure

    _register_activities(
        monkeypatch,
        {
            "classify_disclosure": lambda arg: Classification(
                needs_legal=True, severity=5, risk=0.9, route="x"
            )
        },
    )
    result = await workflow_cls().run(Disclosure(doc="d"))
    assert result.outcome == "deep"


MID_BRANCH_GATE_STEPS = """\
- id: classify
  activity: classify_disclosure
- id: reviews
  parallel:
    branches:
      - id: legal
        steps:
          - id: legal_screen_step
            activity: legal_screen
          - id: legal_rescreen
            when: { path: legal_screen_step.escalate, eq: true }
            activity: legal_rescreen_activity
      - id: medical
        steps:
          - id: medical_review_step
            activity: medical_review
    collect:
      output: schemas:ScreenBundle
- id: consolidate
  activity: consolidate_screen
"""


@pytest.mark.asyncio
async def test_mid_branch_gate_skips_the_remainder_and_contributes_the_running_value(
    tmp_path, monkeypatch
) -> None:
    workflow_cls = _build(
        tmp_path,
        monkeypatch,
        _yaml_text(
            MID_BRANCH_GATE_STEPS,
            lifecycle="lifecycle:\n  enabled: true\n  cancellation: false\n",
        ),
    )
    from yaml_composition_project.schemas import Disclosure, ReviewOutcome

    calls = _register_activities(
        monkeypatch,
        {"consolidate_screen": lambda arg: ReviewOutcome(outcome="done")},
    )
    instance = workflow_cls()
    await instance.run(Disclosure(doc="d"))
    bundle = calls["consolidate_screen"][0]
    # legal_screen returned escalate=False -> legal_rescreen gated out; the branch
    # contributes the running value at the gate.
    assert bundle.legal.screen == "legal-screened"
    assert "legal_rescreen_activity" not in calls
    status = instance._typeflux_lifecycle.status()
    skipped = [event for event in status.events if event.event == "step_skipped"]
    assert skipped[0].step_id == "legal_rescreen"
    assert skipped[0].condition == "legal_screen_step.escalate == true"


@pytest.mark.asyncio
async def test_enforces_collect_max_bytes_on_the_merged_payload(tmp_path, monkeypatch) -> None:
    workflow_cls = _build(
        tmp_path,
        monkeypatch,
        _yaml_text(COMPOSITION_STEPS.replace("max_bytes: 1500000", "max_bytes: 128")),
    )
    from yaml_composition_project.schemas import Disclosure, MedicalReview

    _register_activities(
        monkeypatch,
        {"medical_review": lambda arg: MedicalReview(review="x" * 512)},
    )
    with pytest.raises(BaseException) as excinfo:
        await workflow_cls().run(Disclosure(doc="d"))
    assert getattr(excinfo.value, "type", None) == "TypefluxParallelCollectPayloadTooLarge"
    assert getattr(excinfo.value, "non_retryable", False) is True


@pytest.mark.asyncio
async def test_a_failing_branch_fails_the_workflow_and_cancels_siblings(
    tmp_path, monkeypatch
) -> None:
    workflow_cls = _build(tmp_path, monkeypatch, _yaml_text())
    from yaml_composition_project.schemas import Disclosure

    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def hanging_legal(arg: Any) -> Any:
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise

    async def failing_medical(arg: Any) -> Any:
        await sibling_started.wait()
        raise RuntimeError("medical backend down")

    _register_activities(
        monkeypatch,
        {
            "legal_screen": lambda arg: hanging_legal(arg),
            "medical_review": lambda arg: failing_medical(arg),
        },
    )
    with pytest.raises(RuntimeError, match="medical backend down"):
        await workflow_cls().run(Disclosure(doc="d"))
    await asyncio.wait_for(sibling_cancelled.wait(), timeout=1)


@pytest.mark.asyncio
async def test_per_branch_event_subsequences_stay_ordered_and_units_count(
    tmp_path, monkeypatch
) -> None:
    workflow_cls = _build(
        tmp_path,
        monkeypatch,
        _yaml_text(
            COMPOSITION_STEPS, lifecycle="lifecycle:\n  enabled: true\n  cancellation: false\n"
        ),
    )
    from yaml_composition_project.schemas import Disclosure, LegalScreen

    async def slow_screen(arg: Any) -> Any:
        await asyncio.sleep(0.01)
        return LegalScreen(escalate=False, screen="legal-screened")

    _register_activities(monkeypatch, {"legal_screen": lambda arg: slow_screen(arg)})
    instance = workflow_cls()
    await instance.run(Disclosure(doc="d"))
    status = instance._typeflux_lifecycle.status()
    started = [event.step_id for event in status.events if event.event == "step_started"]
    # The block itself, then branch steps in each branch's declared order
    # (subsequence), then the post-block step — the global interleaving inside the
    # block is deliberately NOT pinned (#55 §5.1).
    assert started[0] == "classify"
    assert started[1] == "reviews"
    assert started.index("legal_screen_step") < started.index("legal_assess_step")
    assert started.index("legal_assess_step") < started.index("consolidate")
    assert started.index("medical_review_step") < started.index("consolidate")
    # Progress accounted every activity exactly once: 1 + (2 + 1 branch units) + 1.
    assert status.total_units == 5
    assert status.completed_units == 5


@pytest.mark.asyncio
async def test_all_branches_gated_out_yields_an_all_none_collect_object(
    tmp_path, monkeypatch
) -> None:
    steps = COMPOSITION_STEPS.replace(
        "- id: medical\n        steps:",
        "- id: medical\n        when: { path: classify.needs_legal, eq: true }\n        steps:",
    ).replace("output: schemas:ReviewBundle", "output: schemas:BothOptionalBundle")
    yaml_text = _yaml_text(steps).replace(
        'name: consolidate_reviews, input: "schemas:ReviewBundle"',
        'name: consolidate_reviews, input: "schemas:BothOptionalBundle"',
    )
    workflow_cls = _build(tmp_path, monkeypatch, yaml_text)
    from yaml_composition_project.schemas import Classification, Disclosure

    calls = _register_activities(
        monkeypatch,
        {
            "classify_disclosure": lambda arg: Classification(
                needs_legal=False, severity=1, risk=0.1, route="x"
            )
        },
    )
    await workflow_cls().run(Disclosure(doc="d"))
    bundle = calls["consolidate_reviews"][0]
    assert bundle.legal is None
    assert bundle.medical is None


@pytest.mark.asyncio
async def test_cooperative_cancel_mid_parallel_cancels_branch_work(tmp_path, monkeypatch) -> None:
    # The cancel-wait task rides the parallel wait set exactly like the map
    # runner's: a typeflux_request_cancel landing mid-block cancels the in-flight
    # branch activities and terminates with TypefluxWorkflowCancelled.
    workflow_cls = _build(
        tmp_path,
        monkeypatch,
        _yaml_text(COMPOSITION_STEPS, lifecycle="lifecycle:\n  enabled: true\n"),
    )
    from yaml_composition_project.schemas import Disclosure

    branch_started = asyncio.Event()
    branch_cancelled = asyncio.Event()

    async def hanging(arg: Any) -> Any:
        branch_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            branch_cancelled.set()
            raise

    _register_activities(
        monkeypatch,
        {"legal_screen": lambda arg: hanging(arg), "medical_review": lambda arg: hanging(arg)},
    )
    instance = workflow_cls()
    run_task = asyncio.create_task(instance.run(Disclosure(doc="d")))
    await asyncio.wait_for(branch_started.wait(), timeout=1)
    instance.typeflux_request_cancel("operator says stop")
    with pytest.raises(BaseException) as excinfo:
        await run_task
    assert getattr(excinfo.value, "type", None) == "TypefluxWorkflowCancelled"
    await asyncio.wait_for(branch_cancelled.wait(), timeout=1)
    status = instance._typeflux_lifecycle.status()
    assert status.state == "cancelled"
    assert status.cancellation_reason == "operator says stop"


# ---------------------------------------------------------------------------
# Digest identity
# ---------------------------------------------------------------------------

# Pinned spec digests + registered types. These moved once at #299 when GENERATOR_VERSION
# bumped "4" -> "5" (compensation schedules an unwind command, so the generator re-registers
# every type — the accepted pre-adoption cutover, mirroring the TS PLAN_INTERPRETER_VERSION
# 1 -> 2 bump). The bump is the ONLY reason they changed: with the version pinned back to "4"
# these examples produce their pre-#299 digests byte-for-byte (compensation is present-only —
# a compensation-free call payload serializes identically). A change here NOT explained by a
# generator-version bump breaks every frozen workflow.version in the field.
V1_EXAMPLE_IDENTITIES = {
    "contract_risk_review": (
        "ContractRiskReviewWorkflow.7d0d416e25fe",
        "7d0d416e25fe4cb35bfa9ee699a3b0daae552bdfd3f31adbcf49dfda828bc9fb",
    ),
    "financial_claims_marketing_review": (
        "FinancialClaimsMarketingReviewWorkflow.87c1f6976621",
        "87c1f6976621d08b8784eb28f381b9542c4e1911a4c286e3928b6944e5012d45",
    ),
    "insurance_claim_review": (
        "InsuranceClaimReviewWorkflow.25e24415aead",
        "25e24415aead57746810809922f98e0f411d2cf304de3aaec5138b91f3e24444",
    ),
    "lifecycle_review": (
        "LifecycleReviewWorkflow.7087d993cd99",
        "7087d993cd993237a44489c38797d237837e9c711ab8b2bb109f1aa9bb05a48c",
    ),
    "multimodal_claim_review": (
        "MultimodalClaimReviewWorkflow.14b70c9502cf",
        "14b70c9502cf37d3106633ae3a3f45f41caf2c86e8812a76f58b3c37bbe22dc4",
    ),
    "regulated_disclosure_review": (
        "RegulatedDisclosureReviewWorkflow.afd82b11a5e1",
        "afd82b11a5e103dd3c4182847ac371a3c4d98796e6f8b9afad05d562de4193ba",
    ),
    "support_triage_langfuse": (
        "SupportTriageYamlWorkflow.04166787755c",
        "04166787755c9c527329a7219260e52de8efae2048688751a89b29cb645c2a2b",
    ),
}


@pytest.mark.parametrize(("example", "identity"), sorted(V1_EXAMPLE_IDENTITIES.items()))
def test_v1_example_keeps_its_pre_composition_digest(
    example: str, identity: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    examples_dir = Path(__file__).resolve().parents[1] / "examples"
    monkeypatch.syspath_prepend(str(examples_dir))
    spec = load_yaml_spec(examples_dir / example / "typeflux.yaml", load_dotenv=False)
    workflow_cls = create_workflow(spec, collect_activities(spec))
    workflow_type, digest = identity
    assert getattr(workflow_cls, "__typeflux_workflow_type__") == workflow_type
    assert getattr(workflow_cls, "__typeflux_spec_digest__") == digest
    # V1 call specs carry NO composition keys: every `when` is None, so the digest
    # payload is byte-identical to the pre-composition serialization.
    assert all(call.when is None for call in _calls(workflow_cls))


def test_topology_projects_composition_shapes_in_the_normative_emission_order(
    tmp_path, monkeypatch
) -> None:
    # The emission order is normative for cross-edition byte parity (TS
    # bundle-topology.ts module doc): depth-first — each step's node, then per
    # branch (declared order) the branch edge, the branch's nodes/edges, and its
    # collect edge; review edges last.
    from typeflux.project.bundle import _bundle_topology

    steps = COMPOSITION_STEPS + (
        "- id: deep_analysis_step\n"
        "  when: { path: classify.severity, gte: 3 }\n"
        "  activity: deep_analysis\n"
    )
    yaml_text = _yaml_text(steps)
    workflow_cls = _build(tmp_path, monkeypatch, yaml_text)
    from typeflux.yaml import load_yaml_spec as _load

    spec = _load(tmp_path / "typeflux.yaml", load_dotenv=False)
    topology = _bundle_topology(workflow_cls, spec)
    assert [node.model_dump(mode="json", exclude_none=True) for node in topology.nodes] == [
        {"id": "classify", "kind": "activity", "activity": "classify_disclosure"},
        {"id": "reviews", "kind": "parallel"},
        {"id": "legal_screen_step", "kind": "activity", "activity": "legal_screen"},
        {"id": "legal_assess_step", "kind": "activity", "activity": "legal_assess"},
        {"id": "medical_review_step", "kind": "activity", "activity": "medical_review"},
        {"id": "consolidate", "kind": "activity", "activity": "consolidate_reviews"},
        {"id": "deep_analysis_step", "kind": "activity", "activity": "deep_analysis"},
    ]
    assert [edge.model_dump(mode="json", exclude_none=True) for edge in topology.edges] == [
        {"source": "classify", "target": "reviews", "kind": "sequential"},
        {
            "source": "reviews",
            "target": "legal_screen_step",
            "kind": "branch",
            "condition": "classify.needs_legal == true",
        },
        {"source": "legal_screen_step", "target": "legal_assess_step", "kind": "sequential"},
        {"source": "legal_assess_step", "target": "reviews", "kind": "collect"},
        {"source": "reviews", "target": "medical_review_step", "kind": "branch"},
        {"source": "medical_review_step", "target": "reviews", "kind": "collect"},
        {"source": "reviews", "target": "consolidate", "kind": "sequential"},
        {
            "source": "consolidate",
            "target": "deep_analysis_step",
            "kind": "conditional",
            "condition": "classify.severity >= 3",
        },
    ]


def test_bundle_steps_flatten_to_leaf_activity_steps(tmp_path, monkeypatch) -> None:
    from typeflux.project.bundle import _bundle_steps

    workflow_cls = _build(tmp_path, monkeypatch, _yaml_text())
    steps = _bundle_steps(workflow_cls)
    assert [step.id for step in steps] == [
        "classify",
        "legal_screen_step",
        "legal_assess_step",
        "medical_review_step",
        "consolidate",
    ]
    assert all(step.kind in ("activity", "map") for step in steps)


def test_composition_digest_is_deterministic_and_moves_on_every_new_surface_edit(
    tmp_path, monkeypatch
) -> None:
    def digest_of(steps: str) -> str:
        return getattr(_build(tmp_path, monkeypatch, _yaml_text(steps)), "__typeflux_spec_digest__")

    base = digest_of(COMPOSITION_STEPS)
    assert base == digest_of(COMPOSITION_STEPS)
    # A `when` literal edit, a step gate addition, a branch reorder, and a
    # collect-bound edit each move the digest — spec_digest change ⇒ new
    # registered type, inherited for free.
    assert digest_of(COMPOSITION_STEPS.replace("eq: true", "eq: false")) != base
    tail = COMPOSITION_STEPS + (
        "- id: deep_analysis_step\n"
        "  when: { path: classify.severity, gte: 3 }\n"
        "  activity: deep_analysis\n"
    )
    assert digest_of(tail) != base
    reordered = COMPOSITION_STEPS.replace(
        "      - id: legal\n"
        "        when: { path: classify.needs_legal, eq: true }\n"
        "        steps:\n"
        "          - id: legal_screen_step\n"
        "            activity: legal_screen\n"
        "          - id: legal_assess_step\n"
        "            activity: legal_assess\n"
        "      - id: medical\n"
        "        steps:\n"
        "          - id: medical_review_step\n"
        "            activity: medical_review\n",
        "      - id: medical\n"
        "        steps:\n"
        "          - id: medical_review_step\n"
        "            activity: medical_review\n"
        "      - id: legal\n"
        "        when: { path: classify.needs_legal, eq: true }\n"
        "        steps:\n"
        "          - id: legal_screen_step\n"
        "            activity: legal_screen\n"
        "          - id: legal_assess_step\n"
        "            activity: legal_assess\n",
    )
    assert reordered != COMPOSITION_STEPS
    assert digest_of(reordered) != base
    assert digest_of(COMPOSITION_STEPS.replace("max_bytes: 1500000", "max_bytes: 900000")) != base
