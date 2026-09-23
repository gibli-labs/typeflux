"""Direct unit tests for typeflux.project.bundle I/O and helpers."""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from conftest import make_minimal_project, write_project_file
from typeflux.project import load_project_spec, resolve_workflow_bundle
from typeflux.project.bundle import (
    _MISSING,
    BundleRetryPolicy,
    ResolvedWorkflowBundle,
    _bundle_retry_policy,
    _interval_seconds,
    _normalize_remote,
    _path_get,
)


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return make_minimal_project(
        tmp_path,
        monkeypatch,
        "bundle_unit_project",
        project_files={
            "policies/openai-only.yaml": """
            version: "1"
            name: openai-only
            providers:
              allowed:
                openai: {}
            """,
        },
        extra_manifest="""
        policies:
          openai-only: policies/openai-only.yaml
        """,
    )


def test_bundle_payload_round_trips_through_model_validate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    bundle = resolve_workflow_bundle(project, workflow_id="workflow", environment_id="local")
    # Write: the JSON payload the control plane serves...
    payload = json.loads(json.dumps(bundle.to_dict()))

    # Known lossy edge of the exclude_none rendering: a runtime_effective knob
    # whose *effective value* is None (the fake provider has no default model;
    # max_backoff_seconds defaults to unbounded) loses its required "value" key
    # in the payload, so it must be reinstated before strict revalidation.
    dropped = [entry for entry in payload["runtime_effective"] if "value" not in entry]
    assert {entry["path"] for entry in dropped} == {
        "provider.model",
        "provider_retry.max_backoff_seconds",
    }
    for entry in dropped:
        entry["value"] = None

    # ...read: reloads into the same strict model and re-serializes identically.
    reloaded = ResolvedWorkflowBundle.model_validate(payload)

    rerendered = reloaded.to_dict()
    for entry in rerendered["runtime_effective"]:
        entry.setdefault("value", None)
    assert rerendered == payload
    assert reloaded.bundle_version == "1"
    assert reloaded.workflow.workflow_name == "BundleDemoWorkflow"
    assert reloaded.workflow.spec_digest == bundle.workflow.spec_digest
    assert [activity.name for activity in reloaded.activities] == ["first"]
    assert [step.id for step in reloaded.steps] == ["first"]


def test_bundle_model_rejects_unknown_fields_and_foreign_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)
    payload = resolve_workflow_bundle(
        project, workflow_id="workflow", environment_id="local"
    ).to_dict()

    with pytest.raises(ValidationError):
        ResolvedWorkflowBundle.model_validate({**payload, "attacker_field": True})
    with pytest.raises(ValidationError):
        ResolvedWorkflowBundle.model_validate({**payload, "bundle_version": "2"})


def test_bundle_stays_inspectable_when_policy_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    # The fake provider violates the openai-only policy: validation reports
    # the failure, but the bundle still resolves for inspection.
    bundle = resolve_workflow_bundle(
        project,
        workflow_id="workflow",
        environment_id="local",
        policy_ids=("openai-only",),
    )

    assert bundle.validation.ok is False
    assert bundle.validation.issues
    assert bundle.policy is not None
    assert bundle.policy.selected_policy_ids == ("openai-only",)
    assert bundle.workflow.workflow_name == "BundleDemoWorkflow"


def _risk_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # A risk policy bound to `workflow`: `min_tier` lifts the (undeclared) workflow to
    # policy_gated, whose macro requires a review gate the minimal single-step workflow
    # lacks — so the bundle surfaces an UNSATISFIED requirement (#300 slice 2).
    return make_minimal_project(
        tmp_path,
        monkeypatch,
        "risk_bundle_project",
        project_files={
            "policies/risk.yaml": """
            version: "1"
            name: risk
            risk_tiers:
              min_tier: policy_gated
              policy_gated:
                require_review: true
            """,
        },
        extra_manifest="""
        policies:
          risk: policies/risk.yaml
        validation:
          targets:
            local:
              environment: local
              workflows:
                - workflow
              policies:
                - risk
        """,
    )


def test_bundle_surfaces_risk_tier_from_the_bound_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _risk_setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    # No explicit policy_ids: the risk policy is AUTO-bound (like `policy`), so the tier
    # posture tracks the policy the workflow is actually governed by.
    bundle = resolve_workflow_bundle(project, workflow_id="workflow", environment_id="local")

    assert bundle.risk_tier is not None
    assert bundle.risk_tier.declared == "safe"
    assert bundle.risk_tier.effective == "policy_gated"
    assert bundle.risk_tier.floor_source == "policy_floor"
    assert bundle.risk_tier.cascade is None
    assert [(r.name, r.satisfied) for r in bundle.risk_tier.requirements] == [
        ("require_review", False)
    ]

    # The DTO round-trips through the strict model; exclude_none omits the absent cascade.
    payload = bundle.to_dict()
    assert "cascade" not in payload["risk_tier"]
    reloaded = ResolvedWorkflowBundle.model_validate(payload)
    assert reloaded.risk_tier is not None and reloaded.risk_tier.effective == "policy_gated"


def test_bundle_surfaces_require_payload_codec_from_a_human_gated_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #188 D188-2: a human_gated tier implies require_payload_codec via the SAME macro
    # mechanism as require_redaction. The minimal workflow declares no payload codec, so
    # the bundle surfaces the requirement as UNSATISFIED (bundle shows what admission enforces).
    manifest = make_minimal_project(
        tmp_path,
        monkeypatch,
        "codec_bundle_project",
        project_files={
            "policies/regulated.yaml": """
            version: "1"
            name: regulated
            risk_tiers:
              min_tier: human_gated
              human_gated:
                require_payload_codec: true
            """,
        },
        extra_manifest="""
        policies:
          regulated: policies/regulated.yaml
        validation:
          targets:
            local:
              environment: local
              workflows:
                - workflow
              policies:
                - regulated
        """,
    )
    project = load_project_spec(manifest)

    bundle = resolve_workflow_bundle(project, workflow_id="workflow", environment_id="local")

    assert bundle.risk_tier is not None
    assert bundle.risk_tier.effective == "human_gated"
    assert ("require_payload_codec", False) in [
        (r.name, r.satisfied) for r in bundle.risk_tier.requirements
    ]


def test_bundle_surfaces_require_declared_as_an_unsatisfied_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `require_declared: true` + an undeclared workflow is an admission DENIAL — the
    # bundle must not read clean (declared: safe, all satisfied). It surfaces as a
    # requirement entry (first), unsatisfied, so the console escalates it.
    manifest = make_minimal_project(
        tmp_path,
        monkeypatch,
        "declared_bundle_project",
        project_files={
            "policies/risk.yaml": """
            version: "1"
            name: risk
            risk_tiers:
              require_declared: true
            """,
        },
        extra_manifest="""
        policies:
          risk: policies/risk.yaml
        validation:
          targets:
            local:
              environment: local
              workflows:
                - workflow
              policies:
                - risk
        """,
    )
    project = load_project_spec(manifest)
    bundle = resolve_workflow_bundle(project, workflow_id="workflow", environment_id="local")

    assert bundle.risk_tier is not None
    assert bundle.risk_tier.declared == "safe"  # unset reads as safe…
    assert [(r.name, r.satisfied) for r in bundle.risk_tier.requirements] == [
        ("require_declared", False)  # …but the demanded declaration is visibly missing
    ]
    # The admission side agrees: the policy_risk_tier check fails on the declaration.
    risk_check = next(
        check for check in bundle.validation.checks if check.code == "policy_risk_tier"
    )
    assert risk_check.status == "failed"
    assert "must declare workflow.risk_tier" in (risk_check.message or "")


def test_bundle_risk_tier_shows_the_closure_cascade_for_a_composed_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # D300-3 on the wire: a safe parent embedding a human_gated child surfaces the
    # closure LIFT in its bundle — the SAME cascade the closure admission check computes.
    package_name = "cascade_bundle_project"
    for name in tuple(sys.modules):
        if name == package_name or name.startswith(f"{package_name}."):
            del sys.modules[name]
    monkeypatch.syspath_prepend(str(tmp_path))
    package = tmp_path / package_name
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    write_project_file(
        package / "schemas.py",
        """
        from pydantic import BaseModel


        class InputModel(BaseModel):
            value: str


        class OutputModel(BaseModel):
            value: str
        """,
    )

    def workflow_yaml(name: str, *, risk_tier: str, extra_workflow: str, steps: str) -> str:
        return (
            f"project: {package_name}\n"
            f"name: {name}\n"
            f"task_queue: {name}-queue\n"
            "runtime:\n"
            "  temporal:\n"
            "    address: localhost:7233\n"
            "  registry:\n"
            "    type: inline\n"
            "    prompts:\n"
            "      first: first {{value}}\n"
            "  provider:\n"
            "    type: fake\n"
            "  observability:\n"
            "    type: none\n"
            "activities:\n"
            "  definitions:\n"
            "    - name: first\n"
            "      input: schemas:InputModel\n"
            # Self-chaining IO so the two-step child (s0 -> s1) type-checks.
            "      output: schemas:InputModel\n"
            "      prompt: first\n"
            "workflow:\n"
            f"  name: {name.capitalize()}Workflow\n"
            "  input: schemas:InputModel\n"
            "  output: schemas:InputModel\n"
            f"  risk_tier: {risk_tier}\n"
            f"{extra_workflow}"
            "  steps:\n"
            f"{steps}"
        )

    review = (
        "  lifecycle:\n"
        "    enabled: true\n"
        "    review:\n"
        "      after_step: s0\n"
        "      user_decisions:\n"
        "        approve:\n"
        "          route: s1\n"
    )
    (tmp_path / "parent.yaml").write_text(
        workflow_yaml(
            "parent",
            risk_tier="safe",
            extra_workflow="",
            steps="    - id: call\n      workflow: child\n",
        ),
        encoding="utf-8",
    )
    (tmp_path / "child.yaml").write_text(
        workflow_yaml(
            "child",
            risk_tier="human_gated",
            extra_workflow=review,
            steps=("    - id: s0\n      activity: first\n    - id: s1\n      activity: first\n"),
        ),
        encoding="utf-8",
    )
    write_project_file(
        tmp_path / "policies" / "risk.yaml",
        """
        version: "1"
        name: risk
        risk_tiers:
          human_gated:
            require_review: true
        """,
    )
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    manifest = tmp_path / "typeflux.project.yaml"
    manifest.write_text(
        'version: "1"\n'
        "name: cascade-demo\n"
        "workflows:\n"
        "  - id: parent\n"
        "    path: parent.yaml\n"
        "  - id: child\n"
        "    path: child.yaml\n"
        "environments:\n"
        "  local: env.yaml\n"
        "policies:\n"
        "  risk: policies/risk.yaml\n"
        "validation:\n"
        "  targets:\n"
        "    local:\n"
        "      environment: local\n"
        "      workflows:\n"
        "        - parent\n"
        "        - child\n"
        "      policies:\n"
        "        - risk\n",
        encoding="utf-8",
    )

    project = load_project_spec(manifest)
    bundle = resolve_workflow_bundle(project, workflow_id="parent", environment_id="local")

    assert bundle.risk_tier is not None
    # `effective` is ALWAYS what admission enforces: the cascade LIFTS the top-level
    # posture (a safe parent embedding a human_gated child IS human_gated), with
    # floor_source naming the lifting member and the LIFTED tier's requirements.
    assert bundle.risk_tier.declared == "safe"
    assert bundle.risk_tier.effective == "human_gated"
    assert bundle.risk_tier.floor_source == "cascade:child"
    assert [(r.name, r.satisfied) for r in bundle.risk_tier.requirements] == [
        ("require_review", False)
    ]
    cascade = bundle.risk_tier.cascade
    assert cascade is not None
    assert cascade.lifted_by == "child"
    assert cascade.effective == "human_gated"
    # The parent has no review gate, so the lifted require_review is unsatisfied.
    assert [(r.name, r.satisfied) for r in cascade.requirements] == [("require_review", False)]
    # On the wire: the cascade is present and the payload names the lifting member.
    payload = bundle.to_dict()
    assert payload["risk_tier"]["cascade"]["lifted_by"] == "child"
    # The admission side agrees: the closure check fails naming the SAME lift.
    closure = next(
        check for check in bundle.validation.checks if check.code == "policy_subworkflow_closure"
    )
    assert closure.status == "failed"
    assert closure.details["risk_tier_cascade"]["lifted_by"] == "child"
    assert closure.details["risk_tier_cascade"]["cascade_effective"] == "human_gated"


def test_bundle_omits_risk_tier_when_no_policy_constrains_tiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The default project's policy has no risk_tiers dimension (and none is bound without
    # explicit ids), so the bundle omits risk_tier entirely (exclude_none).
    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    bundle = resolve_workflow_bundle(project, workflow_id="workflow", environment_id="local")
    assert bundle.risk_tier is None
    assert "risk_tier" not in bundle.to_dict()

    # Even with the non-risk policy explicitly applied, nothing constrains tiers.
    with_policy = resolve_workflow_bundle(
        project, workflow_id="workflow", environment_id="local", policy_ids=("openai-only",)
    )
    assert with_policy.risk_tier is None


def test_normalize_remote_scrubs_credentials_and_rejects_non_http() -> None:
    assert _normalize_remote("git@github.com:acme/demo.git") == "https://github.com/acme/demo"
    assert _normalize_remote("https://github.com/acme/demo.git") == "https://github.com/acme/demo"
    # Embedded tokens must never reach bundle JSON; ports survive the scrub.
    assert _normalize_remote("https://user:tok@host:8443/acme/demo") == (
        "https://host:8443/acme/demo"
    )
    assert _normalize_remote("ssh://git@host/acme/demo") is None
    assert _normalize_remote("not a url") is None


def test_path_get_traverses_dotted_paths_and_signals_missing() -> None:
    data = {"provider_retry": {"max_attempts": 5}, "provider": {"model": None}}

    assert _path_get(data, "provider_retry.max_attempts") == 5
    # An explicit None is a real project-set value, distinct from missing.
    assert _path_get(data, "provider.model") is None
    assert _path_get(data, "provider_retry.missing") is _MISSING
    assert _path_get(data, "absent.max_attempts") is _MISSING
    # Traversal through a leaf value cannot descend further.
    assert _path_get(data, "provider_retry.max_attempts.deeper") is _MISSING


def test_retry_policy_projection_maps_intervals_to_seconds() -> None:
    assert _bundle_retry_policy(None) is None
    assert _interval_seconds(timedelta(minutes=1, seconds=30)) == 90.0
    assert _interval_seconds(None) is None

    projected = _bundle_retry_policy(
        SimpleNamespace(
            maximum_attempts=3,
            initial_interval=timedelta(seconds=1),
            maximum_interval=None,
            backoff_coefficient=2.0,
        )
    )

    assert projected == BundleRetryPolicy(
        maximum_attempts=3,
        initial_interval_seconds=1.0,
        maximum_interval_seconds=None,
        backoff_coefficient=2.0,
    )


# --- erasure posture (#795) -------------------------------------------------------------


def _patch_workflow_yaml(
    tmp_path: Path, *, runtime_extra: str = "", workflow_extra: str = ""
) -> None:
    path = tmp_path / "workflow.yaml"
    text = path.read_text(encoding="utf-8")
    if runtime_extra:
        text = text.replace("runtime:\n  temporal:", f"runtime:\n{runtime_extra}  temporal:", 1)
    if workflow_extra:
        text = text.replace("workflow:\n", f"workflow:\n{workflow_extra}", 1)
    path.write_text(text, encoding="utf-8")


def test_bundle_omits_erasure_without_load_bearing_declarations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    bundle = resolve_workflow_bundle(project, workflow_id="workflow", environment_id="local")
    assert bundle.erasure is None
    assert "erasure" not in bundle.to_dict()


def test_bundle_discloses_cache_erasure_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#795: declared subjects + cache_erasure surface the erasure posture — the declared
    requirement and the honest resolution rule (the store itself is code-injected)."""
    manifest = _setup(tmp_path, monkeypatch)
    _patch_workflow_yaml(
        tmp_path,
        runtime_extra="  cache_erasure: targeted\n",
        workflow_extra="  subjects:\n    - from: input.value\n",
    )
    project = load_project_spec(manifest)

    bundle = resolve_workflow_bundle(project, workflow_id="workflow", environment_id="local")
    assert bundle.erasure is not None
    assert bundle.erasure.subject_selectors == 1
    assert bundle.erasure.cache.declared == "targeted"
    # EXACT cross-edition string (the TS edition pins the same literal): hand-copied
    # constants drift silently without a byte-level pin on each side.
    assert bundle.erasure.cache.behavior == (
        "targeted per-subject invalidation (declared REQUIRED: wiring a store without "
        "SubjectErasableCacheStore fails runtime assembly, an erase run on the cache "
        "surface without a wired store fails loudly, and a deployment that wires no "
        "cache store satisfies the requirement vacuously — an empty cache has nothing "
        "to erase)"
    )


def test_bundle_discloses_wired_store_dependent_behavior_for_subjects_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    _patch_workflow_yaml(tmp_path, workflow_extra="  subjects:\n    - from: input.value\n")
    project = load_project_spec(manifest)

    bundle = resolve_workflow_bundle(project, workflow_id="workflow", environment_id="local")
    assert bundle.erasure is not None
    assert bundle.erasure.cache.declared == "any"
    assert bundle.erasure.cache.behavior == (
        "wired-store dependent: targeted per-subject invalidation when the injected "
        "CacheStore implements SubjectErasableCacheStore, else the documented "
        "full-cache-flush fallback (the erasure receipt records which behavior ran)"
    )
