"""Risk tiers (#300 slice 1) — Python edition.

A risk tier is a NAMED MACRO over existing controls: the workflow DECLARES a tier
(``workflow.risk_tier``); the project policy's ``risk_tiers`` dimension DEFINES the
floor + what each tier requires; ``_validate_risk_tier`` expands the effective tier's
block into the SAME predicates the standalone checks use, fail-closed. Covers: enum
validation, digest invariance, the ordered-enum/OR/intersect merge rules (incl.
``extends`` chains), floor-lift, ``require_declared``, ``prohibited`` deny, each
``require_*`` macro + ``constrain_providers`` interplay, the closure cascade, the three
admission gate classes, and the ``RiskTierContributor`` evidence/exclusions/tag.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from typeflux.metadata import (
    CompensationContributor,
    RiskTierContributor,
    WorkflowMetadataContext,
    redaction_exclusions,
)
from typeflux.project import load_project_spec
from typeflux.project.environment import resolve_project_workflow
from typeflux.project.policy import (
    TypefluxProjectPolicySpec,
    _merge_policy_value,
    compose_project_policies,
)
from typeflux.project.policy_enforcement import (
    ProjectPolicyEnforcementError,
    build_project_policy_runtime_guard,
    evaluate_risk_tier,
    evaluate_workflow_risk_tier,
    validate_project_policy,
    validate_subworkflow_closure_policy,
)
from typeflux.yaml import collect_activities, create_workflow, load_yaml_spec
from typeflux.yaml.spec import (
    RISK_TIER_ORDER,
    WorkflowCompensateSpec,
    WorkflowMapCollectSpec,
    WorkflowMapSpec,
    WorkflowMapStepSpec,
    WorkflowParallelBranchSpec,
    WorkflowParallelCollectSpec,
    WorkflowParallelSpec,
    WorkflowParallelStepSpec,
    WorkflowSpec,
    WorkflowStepSpec,
)

# ── unit resolved-spec factory (mirrors test_project.py's SimpleNamespace pattern) ──


def _resolved(
    *,
    risk_tier: str | None = None,
    gates: list[Any] | None = None,
    lifecycle_enabled: bool = True,
    definitions: list[Any] | None = None,
    steps: list[Any] | None = None,
    redaction_enabled: bool = True,
    provider: str = "openai",
    model: str | None = "gpt-4o-mini",
) -> Any:
    lifecycle = (
        SimpleNamespace(
            enabled=lifecycle_enabled,
            review=None,
            resolved_gates=lambda: list(gates or []),
        )
        if gates is not None
        else None
    )
    spec = SimpleNamespace(
        workflow=SimpleNamespace(risk_tier=risk_tier, lifecycle=lifecycle, steps=steps or []),
        activities=SimpleNamespace(definitions=definitions or []),
        runtime=SimpleNamespace(
            observability=SimpleNamespace(redaction=SimpleNamespace(enabled=redaction_enabled)),
            provider=SimpleNamespace(type=provider, model=model),
        ),
    )
    return SimpleNamespace(spec=spec, workflow_id="w")


def _validate(resolved: Any, payload: dict[str, Any]) -> Any:
    from typeflux.project.policy_enforcement import _validate_risk_tier

    return _validate_risk_tier(resolved, payload)


def _moderated_def(name: str) -> Any:
    return SimpleNamespace(name=name, moderation=SimpleNamespace(on_violation="block"))


def _unmoderated_def(name: str) -> Any:
    return SimpleNamespace(name=name, moderation=None)


def _side_effecting_def(name: str, *, side_effecting: bool = True) -> Any:
    # `evaluate_risk_tier` reads only `.name` and `.side_effecting` off a definition, so a
    # duck-typed namespace suffices (parity with `_moderated_def`).
    return SimpleNamespace(name=name, side_effecting=side_effecting)


def _activity_step(step_id: str, activity: str, *, compensate: bool = False) -> WorkflowStepSpec:
    # Real step models — the require_compensation predicate isinstance-checks step kinds.
    return WorkflowStepSpec(
        id=step_id,
        activity=activity,
        compensate=WorkflowCompensateSpec(activity=activity) if compensate else None,
    )


def _map_step(step_id: str, activity: str, *, compensate: bool = False) -> WorkflowMapStepSpec:
    # A `map:` step fanning an activity — the predicate must treat it as a side-effecting
    # STEP (it invokes the activity per item and accepts compensate:).
    return WorkflowMapStepSpec(
        id=step_id,
        map=WorkflowMapSpec(
            activity=activity,
            over="items",
            concurrency=1,
            collect=WorkflowMapCollectSpec(output="schemas:Out", field="results"),
        ),
        compensate=WorkflowCompensateSpec(activity=activity) if compensate else None,
    )


def _parallel_step(step_id: str, *branch_steps: Any) -> WorkflowParallelStepSpec:
    # A `parallel:` step whose single branch holds the given (activity) steps — the
    # predicate must recurse into branches.
    return WorkflowParallelStepSpec(
        id=step_id,
        parallel=WorkflowParallelSpec(
            branches=[WorkflowParallelBranchSpec(id="b0", steps=list(branch_steps))],
            collect=WorkflowParallelCollectSpec(output="schemas:Out"),
        ),
    )


# ── spec: enum validation + digest invariance ──────────────────────────────────


def test_risk_tier_enum_accepts_the_four_values_and_rejects_others() -> None:
    steps = [{"id": "s1", "activity": "a"}]
    for tier in RISK_TIER_ORDER:
        WorkflowSpec(name="w", input="m:I", output="m:O", steps=steps, risk_tier=tier)
    # Unset defaults to None (never materialized — parity with optional governance fields).
    assert WorkflowSpec(name="w", input="m:I", output="m:O", steps=steps).risk_tier is None
    with pytest.raises(ValidationError):
        WorkflowSpec(name="w", input="m:I", output="m:O", steps=steps, risk_tier="critical")


def test_risk_tier_is_digest_invariant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # risk_tier is governance metadata with no control-flow effect (D300), so it must
    # NOT enter workflow_spec_digest: a spec with/without it (and across tiers) is one
    # deterministic program and must register as the same workflow type.
    package = tmp_path / "risk_digest_project"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "schemas.py").write_text(
        "from pydantic import BaseModel\n\n\nclass In(BaseModel):\n    x: str\n\n\nclass Out(BaseModel):\n    y: str\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    def _digest(risk_line: str) -> str:
        text = (
            "project: risk_digest_project\n"
            "name: demo\n"
            "task_queue: q\n"
            "runtime:\n"
            "  registry: { type: inline, prompts: { p: hi } }\n"
            "  provider: { type: fake }\n"
            "activities:\n"
            "  definitions:\n"
            "    - { name: a, input: schemas:In, output: schemas:Out, prompt: p }\n"
            "workflow:\n"
            "  name: DemoWorkflow\n"
            "  input: schemas:In\n"
            "  output: schemas:Out\n"
            f"{risk_line}"
            "  steps:\n"
            "    - { id: s1, activity: a }\n"
        )
        path = tmp_path / "typeflux.yaml"
        path.write_text(text, encoding="utf-8")
        spec = load_yaml_spec(path, load_dotenv=False)
        return create_workflow(spec, collect_activities(spec)).__typeflux_spec_digest__

    unset = _digest("")
    assert unset == _digest("  risk_tier: safe\n")
    assert unset == _digest("  risk_tier: human_gated\n")
    assert unset == _digest("  risk_tier: prohibited\n")


# ── policy spec + hash invariance ──────────────────────────────────────────────


def test_unset_risk_tiers_drops_from_payload_so_policy_hash_is_stable() -> None:
    # Same drop-when-unset rule as composition: an all-default risk_tiers block must not
    # appear in to_payload, so adding the dimension can't move an existing policy_hash.
    plain = TypefluxProjectPolicySpec(name="p")
    assert "risk_tiers" not in plain.to_payload()
    governed = TypefluxProjectPolicySpec(
        name="p", risk_tiers={"min_tier": "human_gated", "human_gated": {"require_review": True}}
    )
    assert governed.to_payload()["risk_tiers"] == {
        "min_tier": "human_gated",
        "human_gated": {"require_review": True},
    }


# ── merge rules ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("policy_gated", "human_gated", "human_gated"),
        ("human_gated", "safe", "human_gated"),
        ("safe", "prohibited", "prohibited"),
        ("policy_gated", "policy_gated", "policy_gated"),
    ],
)
def test_min_tier_merges_to_the_highest_tier(left: str, right: str, expected: str) -> None:
    path = "p.risk_tiers.min_tier"
    assert _merge_policy_value(left, right, path=path) == expected
    # Order-insensitive (composition is commutative).
    assert _merge_policy_value(right, left, path=path) == expected


def test_require_flags_or_and_constrain_providers_intersect() -> None:
    or_path = "p.risk_tiers.human_gated.require_review"
    assert _merge_policy_value(False, True, path=or_path) is True
    assert _merge_policy_value(False, False, path=or_path) is False
    # D300-6: constrain_providers is providers.allowed's mapping shape — shared
    # provider keys survive the intersection and their models lists intersect.
    intersect_path = "p.risk_tiers.human_gated.constrain_providers"
    merged = _merge_policy_value(
        {"openai": {}, "anthropic": {"models": ["claude-sonnet-4-6", "claude-haiku-4"]}},
        {"anthropic": {"models": ["claude-haiku-4", "claude-opus-4"]}, "gemini": {}},
        path=intersect_path,
    )
    assert merged == {"anthropic": {"models": ["claude-haiku-4"]}}


def test_extends_chain_composes_highest_floor_and_or_flags(tmp_path: Path) -> None:
    # org floor policy_gated + tenant floor human_gated ⇒ human_gated; require flags OR.
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "org.yaml").write_text(
        'version: "1"\nname: org\nrisk_tiers:\n  min_tier: policy_gated\n'
        "  human_gated: { require_review: true }\n",
        encoding="utf-8",
    )
    (tmp_path / "policies" / "tenant.yaml").write_text(
        'version: "1"\nname: tenant\nextends: [org]\nrisk_tiers:\n  min_tier: human_gated\n'
        "  human_gated: { require_redaction: true }\n",
        encoding="utf-8",
    )
    (tmp_path / "w.yaml").write_text(
        "project: proj\nname: w\ntask_queue: q\n"
        "runtime:\n  registry: { type: inline, prompts: { p/x: hi } }\n"
        "  provider: { type: openai, model: gpt-4o-mini }\n"
        "activities:\n  definitions:\n"
        "    - { name: a, input: schemas:In, output: schemas:Out, prompt: p/x }\n"
        "workflow:\n  name: w\n  input: schemas:In\n  output: schemas:Out\n"
        "  steps:\n    - { id: s0, activity: a }\n",
        encoding="utf-8",
    )
    (tmp_path / "typeflux.project.yaml").write_text(
        'version: "1"\nname: proj\nworkflows:\n  - { id: w, path: w.yaml }\npolicies:\n'
        "  org: policies/org.yaml\n  tenant: policies/tenant.yaml\n",
        encoding="utf-8",
    )
    project = load_project_spec(tmp_path / "typeflux.project.yaml")
    composed = compose_project_policies(project, ("tenant",))
    risk = composed.payload["risk_tiers"]
    assert risk["min_tier"] == "human_gated"
    assert risk["human_gated"] == {"require_review": True, "require_redaction": True}


# ── evaluate / _validate_risk_tier ─────────────────────────────────────────────


def test_no_risk_tiers_dimension_is_skipped() -> None:
    assert evaluate_risk_tier(_resolved().spec, {}) is None
    assert _validate(_resolved(), {}).status == "skipped"


def test_floor_lifts_effective_tier_and_expands_the_lifted_block() -> None:
    payload = {"risk_tiers": {"min_tier": "human_gated", "human_gated": {"require_review": True}}}
    # safe declared, floor human_gated, no review gate ⇒ lifted + unsatisfied ⇒ fail.
    check = _validate(_resolved(risk_tier="safe"), payload)
    assert check.status == "failed"
    assert check.details["effective"] == "human_gated"
    assert check.details["floor_source"] == "policy_floor"
    assert "require_review" in (check.message or "")
    # Satisfied once a review gate exists.
    ok = _validate(_resolved(risk_tier="human_gated", gates=[object()]), payload)
    assert ok.status == "passed"
    assert ok.details["requirements"] == [{"name": "require_review", "satisfied": True}]


def test_require_declared_rejects_undeclared_workflow() -> None:
    payload = {"risk_tiers": {"require_declared": True}}
    fail = _validate(_resolved(risk_tier=None), payload)
    assert fail.status == "failed"
    assert "must declare workflow.risk_tier" in (fail.message or "")
    assert _validate(_resolved(risk_tier="safe"), payload).status == "passed"


def test_prohibited_effective_tier_denies_admission() -> None:
    fail = _validate(_resolved(risk_tier="prohibited"), {"risk_tiers": {"safe": {}}})
    assert fail.status == "failed"
    assert "denies admission" in (fail.message or "")
    # A prohibited FLOOR denies every workflow regardless of its declaration.
    floor = _validate(_resolved(risk_tier="safe"), {"risk_tiers": {"min_tier": "prohibited"}})
    assert floor.status == "failed"
    assert floor.details["effective"] == "prohibited"


def test_each_require_macro_satisfied_and_unsatisfied() -> None:
    def block(**kw: Any) -> dict[str, Any]:
        return {"risk_tiers": {"human_gated": kw}}

    tier = {"risk_tier": "human_gated"}
    # require_review
    assert _validate(_resolved(**tier), block(require_review=True)).status == "failed"
    assert (
        _validate(_resolved(**tier, gates=[object()]), block(require_review=True)).status
        == "passed"
    )
    # require_moderation
    assert (
        _validate(
            _resolved(**tier, definitions=[_unmoderated_def("a")]), block(require_moderation=True)
        ).status
        == "failed"
    )
    assert (
        _validate(
            _resolved(**tier, definitions=[_moderated_def("a")]), block(require_moderation=True)
        ).status
        == "passed"
    )
    # require_redaction
    assert (
        _validate(_resolved(**tier, redaction_enabled=False), block(require_redaction=True)).status
        == "failed"
    )
    assert (
        _validate(_resolved(**tier, redaction_enabled=True), block(require_redaction=True)).status
        == "passed"
    )
    # require_compensation (#299 D299-5): a side-effecting step without compensate fails;
    # declaring compensate: satisfies it.
    uncovered = _validate(
        _resolved(
            **tier,
            definitions=[_side_effecting_def("a")],
            steps=[_activity_step("s0", "a")],
        ),
        block(require_compensation=True),
    )
    assert uncovered.status == "failed"
    assert "require_compensation" in (uncovered.message or "")
    assert (
        _validate(
            _resolved(
                **tier,
                definitions=[_side_effecting_def("a")],
                steps=[_activity_step("s0", "a", compensate=True)],
            ),
            block(require_compensation=True),
        ).status
        == "passed"
    )


def test_require_compensation_ignores_non_side_effecting_steps() -> None:
    # #299 D299-5: only STEPS invoking a `side_effecting` activity are constrained. A step
    # whose activity is NOT declared side-effecting never needs compensate, and a definition
    # marked side_effecting that no step invokes is likewise inert.
    tier = {"risk_tier": "human_gated"}
    payload = {"risk_tiers": {"human_gated": {"require_compensation": True}}}
    # side_effecting: false on the definition ⇒ the step is unconstrained.
    assert (
        _validate(
            _resolved(
                **tier,
                definitions=[_side_effecting_def("a", side_effecting=False)],
                steps=[_activity_step("s0", "a")],
            ),
            payload,
        ).status
        == "passed"
    )
    # No side_effecting definitions at all ⇒ nothing to compensate.
    assert (
        _validate(
            _resolved(**tier, definitions=[_moderated_def("a")], steps=[_activity_step("s0", "a")]),
            payload,
        ).status
        == "passed"
    )


def test_require_compensation_covers_map_steps() -> None:
    # #299 D299-5: a `map:` step fanning a side-effecting activity IS a side-effecting step
    # (governance fail-open surface — pin it). Without compensate → fails; with → passes.
    tier = {"risk_tier": "human_gated"}
    payload = {"risk_tiers": {"human_gated": {"require_compensation": True}}}
    uncovered = _validate(
        _resolved(**tier, definitions=[_side_effecting_def("a")], steps=[_map_step("m0", "a")]),
        payload,
    )
    assert uncovered.status == "failed"
    assert "require_compensation" in (uncovered.message or "")
    assert (
        _validate(
            _resolved(
                **tier,
                definitions=[_side_effecting_def("a")],
                steps=[_map_step("m0", "a", compensate=True)],
            ),
            payload,
        ).status
        == "passed"
    )


def test_require_compensation_recurses_parallel_branches() -> None:
    # #299 D299-5: a side-effecting activity step nested inside a `parallel:` branch must be
    # found by the recursive walk (another fail-open surface — pin it).
    tier = {"risk_tier": "human_gated"}
    payload = {"risk_tiers": {"human_gated": {"require_compensation": True}}}
    uncovered = _validate(
        _resolved(
            **tier,
            definitions=[_side_effecting_def("a")],
            steps=[_parallel_step("p0", _activity_step("s0", "a"))],
        ),
        payload,
    )
    assert uncovered.status == "failed"
    assert "require_compensation" in (uncovered.message or "")
    # Compensating the branch step satisfies it.
    assert (
        _validate(
            _resolved(
                **tier,
                definitions=[_side_effecting_def("a")],
                steps=[_parallel_step("p0", _activity_step("s0", "a", compensate=True))],
            ),
            payload,
        ).status
        == "passed"
    )


def test_require_review_ignores_inert_disabled_lifecycle_gates() -> None:
    # codex P1: a review block under a DISABLED (or default-off) lifecycle never
    # executes — the runtime only attaches gates when lifecycle.enabled is true — so
    # declared-but-inert gates must NOT satisfy require_review (fail-open otherwise).
    payload = {"risk_tiers": {"human_gated": {"require_review": True}}}
    disabled = _validate(
        _resolved(risk_tier="human_gated", gates=[object()], lifecycle_enabled=False), payload
    )
    assert disabled.status == "failed"
    assert "require_review" in (disabled.message or "")
    enabled = _validate(
        _resolved(risk_tier="human_gated", gates=[object()], lifecycle_enabled=True), payload
    )
    assert enabled.status == "passed"


def test_policy_review_require_review_routes_ignores_disabled_lifecycle() -> None:
    # The shared gates helper also strengthens the pre-existing policy_review check:
    # require_review_routes must reject a disabled-lifecycle spec whose gates are inert.
    from typeflux.project.policy_enforcement import _validate_review

    payload = {"review": {"require_review_routes": True}}
    disabled = _validate_review(_resolved(gates=[object()], lifecycle_enabled=False), payload)
    assert disabled.status == "failed"
    assert "review routes are required" in (disabled.message or "")
    enabled_gate = SimpleNamespace(invalid_user_decision="warn", id="g")
    enabled = _validate_review(_resolved(gates=[enabled_gate], lifecycle_enabled=True), payload)
    assert enabled.status == "passed"


def test_constrain_providers_must_pass_the_tier_allowlist() -> None:
    # D300-6: provider-ONLY constraint (empty allowance = any model of that provider).
    payload = {"risk_tiers": {"human_gated": {"constrain_providers": {"anthropic": {}}}}}
    fail = _validate(_resolved(risk_tier="human_gated", provider="openai"), payload)
    assert fail.status == "failed"
    assert "constrain_providers" in (fail.message or "")
    assert (
        _validate(_resolved(risk_tier="human_gated", provider="anthropic"), payload).status
        == "passed"
    )


def test_constrain_providers_checks_the_model_like_providers_allowed() -> None:
    # D300-6: the macro reuses the EXACT providers.allowed predicate, so a per-provider
    # models list rejects a disallowed model — not just a disallowed provider type.
    payload = {
        "risk_tiers": {
            "human_gated": {"constrain_providers": {"openai": {"models": ["gpt-4o-mini"]}}}
        }
    }
    ok = _validate(
        _resolved(risk_tier="human_gated", provider="openai", model="gpt-4o-mini"), payload
    )
    assert ok.status == "passed"
    bad_model = _validate(
        _resolved(risk_tier="human_gated", provider="openai", model="gpt-4o"), payload
    )
    assert bad_model.status == "failed"
    assert "constrain_providers" in (bad_model.message or "")


# ── project fixtures: cascade + gate classes ───────────────────────────────────


def _write_risk_project(
    tmp_path: Path,
    *,
    risk_tiers_yaml: str,
    workflows: dict[str, dict[str, str]],
    target_workflows: list[str],
    side_effecting: bool = False,
) -> Path:
    (tmp_path / "environments").mkdir(exist_ok=True)
    (tmp_path / "environments" / "local.yaml").write_text("name: local\n", encoding="utf-8")
    (tmp_path / "policies").mkdir(exist_ok=True)
    (tmp_path / "policies" / "risk.yaml").write_text(
        f'version: "1"\nname: risk\nrisk_tiers:\n{risk_tiers_yaml}\n', encoding="utf-8"
    )
    side_effecting_field = ", side_effecting: true" if side_effecting else ""
    for name, cfg in workflows.items():
        risk_line = f"  risk_tier: {cfg['risk_tier']}\n" if cfg.get("risk_tier") else ""
        lifecycle = cfg.get("lifecycle", "")
        (tmp_path / f"{name}.yaml").write_text(
            "project: proj\n"
            f"name: {name}\n"
            "task_queue: q\n"
            "runtime:\n"
            "  registry: { type: inline, prompts: { p/x: hi } }\n"
            "  provider: { type: openai, model: gpt-4o-mini }\n"
            "activities:\n"
            "  definitions:\n"
            f"    - {{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x{side_effecting_field} }}\n"
            "workflow:\n"
            f"  name: {name}\n"
            "  input: schemas:In\n"
            "  output: schemas:Out\n"
            f"{risk_line}"
            f"{lifecycle}"
            "  steps:\n"
            f"{cfg['steps']}\n",
            encoding="utf-8",
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
        "  risk: policies/risk.yaml\n"
        "validation:\n"
        "  targets:\n"
        "    local:\n"
        "      environment: local\n"
        "      workflows:\n"
        f"{target_list}\n"
        "      policies:\n"
        "        - risk\n",
        encoding="utf-8",
    )
    return tmp_path / "typeflux.project.yaml"


_REVIEW_LIFECYCLE = (
    "  lifecycle:\n"
    "    enabled: true\n"
    "    review:\n"
    "      after_step: s0\n"
    "      user_decisions: { approve: { route: s1 } }\n"
)


def test_closure_cascade_lifts_a_safe_parent_to_the_child_tier(tmp_path: Path) -> None:
    # D300-3: a safe parent embedding a human_gated child is itself evaluated at
    # human_gated. The parent has no review gate, so require_review fails the cascade,
    # naming the lifting child.
    manifest = _write_risk_project(
        tmp_path,
        risk_tiers_yaml="  human_gated: { require_review: true }",
        workflows={
            "parent": {"risk_tier": "safe", "steps": "    - { id: call, workflow: child }"},
            "child": {
                "risk_tier": "human_gated",
                "steps": "    - { id: s0, activity: a }\n    - { id: s1, activity: a }",
                "lifecycle": _REVIEW_LIFECYCLE,
            },
        },
        target_workflows=["parent", "child"],
    )
    project = load_project_spec(manifest)
    resolved = resolve_project_workflow(project, workflow_id="parent", environment_id="local")
    policy = compose_project_policies(project, ("risk",))
    closure = validate_subworkflow_closure_policy(
        project=project, resolved=resolved, policy=policy, environment_id="local"
    )
    assert closure is not None and closure.status == "failed"
    assert "cascade" in (closure.message or "") and "child" in (closure.message or "")
    cascade = closure.details["risk_tier_cascade"]
    assert cascade["parent_effective"] == "safe"
    assert cascade["cascade_effective"] == "human_gated"
    assert cascade["lifted_by"] == "child"


def test_require_compensation_rides_the_closure_cascade(tmp_path: Path) -> None:
    # #299 D299-5: require_compensation lifts through sub-workflows on the SAME cascade
    # walk as the other requirements (no second traversal). A safe parent with an
    # uncovered side-effecting step is fine standalone, but a human_gated child lifts it to
    # a tier that requires compensation, so the parent's own uncovered step fails the
    # closure check.
    manifest = _write_risk_project(
        tmp_path,
        risk_tiers_yaml="  human_gated: { require_compensation: true }",
        workflows={
            "parent": {
                "risk_tier": "safe",
                "steps": ("    - { id: s0, activity: a }\n    - { id: call, workflow: child }"),
            },
            "child": {
                "risk_tier": "human_gated",
                # The child's own side-effecting step is covered, so its own check passes.
                "steps": "    - id: s0\n      activity: a\n      compensate: { activity: a }",
            },
        },
        target_workflows=["parent", "child"],
        side_effecting=True,
    )
    project = load_project_spec(manifest)
    resolved = resolve_project_workflow(project, workflow_id="parent", environment_id="local")
    policy = compose_project_policies(project, ("risk",))
    closure = validate_subworkflow_closure_policy(
        project=project, resolved=resolved, policy=policy, environment_id="local"
    )
    assert closure is not None and closure.status == "failed"
    assert "require_compensation" in (closure.message or "")
    cascade = closure.details["risk_tier_cascade"]
    assert cascade["cascade_effective"] == "human_gated"
    assert cascade["lifted_by"] == "child"


def test_risk_tier_check_fails_all_three_admission_gate_classes(tmp_path: Path) -> None:
    # A prohibited-floor policy denies every workflow. It must fail at (1) the validate
    # report, (2) the runtime-guard admission, (3) the deploy gate — all funnel through
    # validate_project_policy, so one registered check lands everywhere (like #298).
    manifest = _write_risk_project(
        tmp_path,
        risk_tiers_yaml="  min_tier: prohibited",
        workflows={"solo": {"steps": "    - { id: s0, activity: a }"}},
        target_workflows=["solo"],
    )
    project = load_project_spec(manifest)
    resolved = resolve_project_workflow(project, workflow_id="solo", environment_id="local")
    policy = compose_project_policies(project, ("risk",))

    # (1) validate report
    checks = validate_project_policy(project=project, resolved=resolved, policy=policy)
    risk_check = next(c for c in checks if c.code == "policy_risk_tier")
    assert risk_check.status == "failed"

    # (2) runtime-guard admission (fail-closed)
    with pytest.raises(ProjectPolicyEnforcementError, match="denies admission"):
        build_project_policy_runtime_guard(project=project, resolved=resolved)

    # (3) deploy gate
    from typeflux.project.deployment import ProjectDeploymentError, _admit_policy

    with pytest.raises(ProjectDeploymentError, match="policy_risk_tier"):
        _admit_policy(project=project, resolved=resolved, explicit_policy_ids=("risk",))


# ── contributor ────────────────────────────────────────────────────────────────


def test_risk_tier_contributor_stamps_safe_evidence_and_a_single_tag() -> None:
    contributor = RiskTierContributor(
        declared="safe",
        effective="human_gated",
        floor_source="policy_floor",
        satisfied_controls=["require_review", "require_redaction"],
    )
    contribution = contributor.workflow(
        WorkflowMetadataContext(workflow_name="w", workflow_id="wid", task_queue="q")
    )
    risk = contribution.workflow_metadata["typeflux"]["risk_tier"]
    assert risk == {
        "declared": "safe",
        "effective": "human_gated",
        "floor_source": "policy_floor",
        "satisfied_controls": ["require_review", "require_redaction"],
    }
    assert contribution.search_tags == ("typeflux.risk_tier:human_gated",)
    # Every stamped field is redaction-exempt (tier/control names are safe governance
    # evidence — never prompts or secrets), including the execution-manifest mirror.
    exclusions = set(contribution.redaction_exclusions)
    for field in ("declared", "effective", "floor_source", "satisfied_controls"):
        assert f"typeflux.risk_tier.{field}" in exclusions
        assert f"typeflux.execution_manifest.contributions.risk_tier.{field}" in exclusions


def test_risk_tier_contributor_is_inert_without_data() -> None:
    # Parity with AdmissionContributor's only-when-ran rule: no data ⇒ no metadata, no
    # tag, no exclusions (an ungoverned workflow's manifest is byte-unchanged).
    contributor = RiskTierContributor()
    contribution = contributor.workflow(
        WorkflowMetadataContext(workflow_name="w", workflow_id="wid", task_queue="q")
    )
    assert contribution.workflow_metadata == {}
    assert contribution.search_tags == ()
    assert contribution.redaction_exclusions == ()
    assert redaction_exclusions((contributor,)) == ()


# ── slice 2: CompensationContributor (#299 D299-5) ──────────────────────────────


def test_compensation_contributor_stamps_planned_steps_and_status() -> None:
    # Records only the PLANNED graph step ids (sorted, deduped) + a static "declared"
    # status — the #299 "manifests record only planned steps" criterion by construction.
    contributor = CompensationContributor(declared_steps=["charge", "book", "charge"])
    contribution = contributor.workflow(
        WorkflowMetadataContext(workflow_name="w", workflow_id="wid", task_queue="q")
    )
    comp = contribution.workflow_metadata["typeflux"]["compensation"]
    assert comp == {"declared_steps": ["book", "charge"], "status": "declared"}
    assert contribution.workflow_manifest["contributions"]["compensation"] == comp
    assert contribution.search_tags == ("typeflux.compensation:declared",)
    exclusions = set(contribution.redaction_exclusions)
    for field in ("declared_steps", "status"):
        assert f"typeflux.compensation.{field}" in exclusions
        assert f"typeflux.execution_manifest.contributions.compensation.{field}" in exclusions


def test_compensation_contributor_from_spec_walks_compensating_steps() -> None:
    # from_spec collects every step id that declares compensate: (a duck-typed walk over
    # workflow.steps). A non-saga workflow yields an inert contributor (byte-unchanged).
    saga = SimpleNamespace(
        workflow=SimpleNamespace(
            steps=[
                _activity_step("book", "a", compensate=True),
                _activity_step("charge", "a", compensate=False),
            ]
        )
    )
    contributor = CompensationContributor.from_spec(saga)
    assert contributor.declared_steps == ("book",)
    assert contributor.payload == {"declared_steps": ["book"], "status": "declared"}

    plain = SimpleNamespace(
        workflow=SimpleNamespace(steps=[_activity_step("s0", "a", compensate=False)])
    )
    inert = CompensationContributor.from_spec(plain)
    assert inert.payload == {}
    contribution = inert.workflow(
        WorkflowMetadataContext(workflow_name="w", workflow_id="wid", task_queue="q")
    )
    assert contribution.workflow_metadata == {}
    assert contribution.search_tags == ()
    assert redaction_exclusions((inert,)) == ()


def test_side_effecting_is_digest_invariant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #299 D299-5: side_effecting is governance metadata on a definition — like risk_tier
    # and moderation it must NOT enter workflow_spec_digest (definitions are not digest
    # inputs), so marking an activity side-effecting registers the same workflow type.
    package = tmp_path / "se_digest_project"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "schemas.py").write_text(
        "from pydantic import BaseModel\n\n\nclass In(BaseModel):\n    x: str\n\n\n"
        "class Out(BaseModel):\n    y: str\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    def _digest(se_field: str) -> str:
        text = (
            "project: se_digest_project\n"
            "name: demo\n"
            "task_queue: q\n"
            "runtime:\n"
            "  registry: { type: inline, prompts: { p: hi } }\n"
            "  provider: { type: fake }\n"
            "activities:\n"
            "  definitions:\n"
            f"    - {{ name: a, input: schemas:In, output: schemas:Out, prompt: p{se_field} }}\n"
            "workflow:\n"
            "  name: DemoWorkflow\n"
            "  input: schemas:In\n"
            "  output: schemas:Out\n"
            "  steps:\n"
            "    - { id: s1, activity: a }\n"
        )
        path = tmp_path / "typeflux.yaml"
        path.write_text(text, encoding="utf-8")
        spec = load_yaml_spec(path, load_dotenv=False)
        return create_workflow(spec, collect_activities(spec)).__typeflux_spec_digest__

    assert _digest("") == _digest(", side_effecting: true")


# ── slice 2: workflow posture (evaluate_workflow_risk_tier, feeds BundleRiskTier) ──
#
# The bundle DTO (BundleRiskTier) is populated from this SAME result — the DTO wiring +
# exclude_none is covered in test_project_bundle.py, where projects have importable
# schemas; here we pin the posture logic (base + closure cascade) directly, since
# `evaluate_workflow_risk_tier` resolves specs only (no workflow-class build).


def _risk_posture(tmp_path: Path, workflow_id: str, **kwargs: Any) -> Any:
    manifest = _write_risk_project(tmp_path, **kwargs)
    project = load_project_spec(manifest)
    resolved = resolve_project_workflow(project, workflow_id=workflow_id, environment_id="local")
    policy = compose_project_policies(project, ("risk",))
    return evaluate_workflow_risk_tier(
        project, resolved=resolved, policy=policy, environment_id="local"
    )


def test_workflow_posture_populates_base_from_the_floor_lift(tmp_path: Path) -> None:
    # An undeclared workflow lifted by a policy floor: the posture (which feeds the bundle
    # DTO) carries the SAME evaluation the policy_risk_tier check computes — one satisfied
    # and one unsatisfied macro requirement (no lifecycle ⇒ no review), no cascade.
    posture = _risk_posture(
        tmp_path,
        "solo",
        risk_tiers_yaml=(
            "  min_tier: policy_gated\n"
            "  policy_gated:\n"
            "    require_review: true\n"
            "    constrain_providers: { openai: { models: [gpt-4o-mini] } }"
        ),
        workflows={"solo": {"steps": "    - { id: s0, activity: a }"}},
        target_workflows=["solo"],
    )
    assert posture is not None
    assert posture.base.declared == "safe"  # unset reads as safe
    assert posture.base.effective == "policy_gated"  # lifted by min_tier
    assert posture.base.floor == "policy_gated"
    assert posture.base.floor_source == "policy_floor"
    assert posture.cascade is None
    assert {req.name: req.satisfied for req in posture.base.requirements} == {
        "require_review": False,
        "constrain_providers": True,
    }


def test_workflow_posture_is_none_when_policy_declares_no_risk_tiers(tmp_path: Path) -> None:
    # A policy with an all-default (dropped) risk_tiers block leaves nothing in play, so
    # the posture — and therefore the bundle's risk_tier — is omitted entirely.
    posture = _risk_posture(
        tmp_path,
        "solo",
        risk_tiers_yaml="  {}",
        workflows={"solo": {"steps": "    - { id: s0, activity: a }"}},
        target_workflows=["solo"],
    )
    assert posture is None


def test_workflow_posture_carries_the_closure_cascade_for_a_composed_workflow(
    tmp_path: Path,
) -> None:
    # D300-3 in the surfacing path: a safe parent embedding a human_gated child carries the
    # closure LIFT — the SAME cascade the closure admission check computes, from the shared
    # `_risk_tier_cascade` helper.
    posture = _risk_posture(
        tmp_path,
        "parent",
        risk_tiers_yaml="  human_gated: { require_review: true }",
        workflows={
            "parent": {"risk_tier": "safe", "steps": "    - { id: call, workflow: child }"},
            "child": {
                "risk_tier": "human_gated",
                "steps": "    - { id: s0, activity: a }\n    - { id: s1, activity: a }",
                "lifecycle": _REVIEW_LIFECYCLE,
            },
        },
        target_workflows=["parent", "child"],
    )
    assert posture is not None
    assert posture.base.effective == "safe"  # the parent's OWN declared+floor tier
    assert posture.cascade is not None
    assert posture.cascade.lifted_by == "child"
    assert posture.cascade.evaluation.effective == "human_gated"
    # The parent has no review gate, so the lifted require_review is unsatisfied here too.
    assert {req.name: req.satisfied for req in posture.cascade.evaluation.requirements} == {
        "require_review": False
    }


def test_guard_build_fails_closed_on_elevated_child_under_dimensionless_policy(
    tmp_path: Path,
) -> None:
    # #788 review P1: a SAFE parent bound to a policy with NO risk_tiers dimension
    # references a human_gated child. Pre-fix the parent's guard built happily and the
    # child's tier ran unenforced; the binding check now follows the closure.
    manifest = _write_risk_project(
        tmp_path,
        risk_tiers_yaml="  {}",
        workflows={
            "parent": {"risk_tier": "safe", "steps": "    - { id: call, workflow: child }"},
            "child": {
                "risk_tier": "human_gated",
                "steps": "    - { id: s0, activity: a }",
            },
        },
        target_workflows=["parent", "child"],
    )
    # Overwrite the policy WITHOUT any risk_tiers dimension (the writer always adds one).
    (tmp_path / "policies" / "risk.yaml").write_text(
        'version: "1"\nname: risk\nproviders:\n  allowed:\n    openai: {}\n',
        encoding="utf-8",
    )
    project = load_project_spec(manifest)
    resolved = resolve_project_workflow(project, workflow_id="parent", environment_id="local")
    from typeflux.project.policy_enforcement import (
        ProjectPolicyEnforcementError,
        build_project_policy_runtime_guard,
        max_declared_closure_risk_tier,
    )

    assert (
        max_declared_closure_risk_tier(project, resolved=resolved, environment_id="local")
        == "human_gated"
    )
    with pytest.raises(ProjectPolicyEnforcementError, match="sub-workflow closure"):
        build_project_policy_runtime_guard(project=project, resolved=resolved)


def test_guard_build_fails_closed_on_hollow_risk_tiers_dimension(tmp_path: Path) -> None:
    # #788 review M4: a policy with a PRESENT but hollow risk_tiers dimension
    # (min_tier only — no requirements, no denial for the effective tier) must not
    # defeat the fail-close via --policy or a binding.
    manifest = _write_risk_project(
        tmp_path,
        risk_tiers_yaml="  min_tier: safe",
        workflows={
            "solo": {"risk_tier": "human_gated", "steps": "    - { id: s0, activity: a }"},
        },
        target_workflows=["solo"],
    )
    project = load_project_spec(manifest)
    resolved = resolve_project_workflow(project, workflow_id="solo", environment_id="local")
    from typeflux.project.policy_enforcement import (
        ProjectPolicyEnforcementError,
        build_project_policy_runtime_guard,
    )

    with pytest.raises(ProjectPolicyEnforcementError, match="hollow"):
        build_project_policy_runtime_guard(project=project, resolved=resolved)


def test_validate_reports_skipped_for_tier_bound_only_in_another_environment(
    tmp_path: Path,
) -> None:
    # #788 review 1(d): a prod-only elevated workflow must not fail `validate
    # --environment local` — it reports skipped naming the bound environments, and
    # only an actual start in local fails closed at guard build.
    _write_risk_project(
        tmp_path,
        risk_tiers_yaml="  human_gated: { require_review: true }",
        workflows={
            "solo": {
                "risk_tier": "human_gated",
                "steps": "    - { id: s0, activity: a }\n    - { id: s1, activity: a }",
                "lifecycle": _REVIEW_LIFECYCLE,
            },
        },
        target_workflows=["solo"],
    )
    # Rewrite the target to bind ONLY the prod environment.
    project_yaml = tmp_path / "typeflux.project.yaml"
    text = project_yaml.read_text(encoding="utf-8")
    (tmp_path / "environments" / "prod.yaml").write_text("name: prod\n", encoding="utf-8")
    text = text.replace(
        "environments:\n  local: environments/local.yaml\n",
        "environments:\n  local: environments/local.yaml\n  prod: environments/prod.yaml\n",
    )
    text = text.replace("      environment: local\n", "      environment: prod\n")
    project_yaml.write_text(text, encoding="utf-8")

    from typeflux.project.validation import validate_project_bundle

    report = validate_project_bundle(load_project_spec(project_yaml), environment_id="local")
    statuses = {check.code: check for check in report.resolved_workflows[0].checks}
    binding = statuses["risk_tier_binding"]
    assert binding.status == "skipped"
    assert "prod" in (binding.message or "")
    # And the guard still fails closed when someone actually tries to run it in local.
    from typeflux.project.policy_enforcement import (
        ProjectPolicyEnforcementError,
        build_project_policy_runtime_guard,
    )

    project = load_project_spec(project_yaml)
    resolved = resolve_project_workflow(project, workflow_id="solo", environment_id="local")
    with pytest.raises(ProjectPolicyEnforcementError, match="UNENFORCED"):
        build_project_policy_runtime_guard(project=project, resolved=resolved)


def test_admit_spec_rejects_elevated_tier_under_hollow_policy(tmp_path: Path) -> None:
    # #788 review round 8/9: external admission shares the binding gap — an elevated
    # spec under a bound-but-hollow risk_tiers dimension is REJECTED, not admitted.
    manifest = _write_risk_project(
        tmp_path,
        risk_tiers_yaml="  min_tier: safe",
        workflows={
            "solo": {"risk_tier": "human_gated", "steps": "    - { id: s0, activity: a }"},
        },
        target_workflows=["solo"],
    )
    from typeflux.project.admission import admit_spec

    project = load_project_spec(manifest)
    spec_text = (tmp_path / "solo.yaml").read_text(encoding="utf-8")
    report = admit_spec(spec_text, project, "local", origin="external", workflow_id="solo")
    assert report.admitted is False
    binding = {check.code: check for check in report.checks}["risk_tier_binding"]
    assert binding.status == "failed"
    assert "hollow" in (binding.message or "")
