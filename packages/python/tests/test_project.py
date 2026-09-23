from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from typeflux import load_project_spec as top_level_load_project_spec
from typeflux.execution.starter import workflow_invocation_metadata
from typeflux.project import (
    ProjectPolicyError,
    TypefluxProjectSpec,
    compose_project_policies,
    discover_project_workflows,
    load_project_environment,
    load_project_policies,
    load_project_policy,
    load_project_spec,
    load_project_workflow_specs,
    project_environment_context,
    resolve_project_workflow,
    resolve_workflow_bundle,
    validate_project,
    validate_project_bundle,
    workflow_drain_status,
)
from typeflux.project import __main__ as project_cli
from typeflux.project.environment import ProjectEnvironmentError
from typeflux.yaml.runtime import _yaml_metadata_contributors


def test_project_manifest_loads_and_discovers_workflow_paths(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "workflows" / "first"
    workflow_dir.mkdir(parents=True)
    _write_workflow_yaml(
        workflow_dir / "typeflux.yaml",
        yaml_name="first",
        workflow_name="FirstWorkflow",
    )
    explicit_path = tmp_path / "workflows" / "second.yaml"
    _write_workflow_yaml(explicit_path, yaml_name="second", workflow_name="SecondWorkflow")
    _write_reference_files(tmp_path)
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: first
            directory: workflows/first
          - id: second
            path: workflows/second.yaml
        environments:
          local: environments/local.yaml
        policies:
          base: policies/base.yaml
        validation:
          targets:
            local:
              workflows: [first, second]
              environment: local
              policies: [base]
        """,
    )

    project = load_project_spec(project_path)
    discovered = discover_project_workflows(project)
    report = validate_project(project)

    assert top_level_load_project_spec(project_path).name == "demo-project"
    assert [workflow.id for workflow in discovered] == ["first", "second"]
    assert discovered[0].resolved_path == workflow_dir / "typeflux.yaml"
    assert discovered[1].resolved_path == explicit_path
    assert report.ok is True
    assert [workflow.workflow_name for workflow in report.workflows] == [
        "FirstWorkflow",
        "SecondWorkflow",
    ]


def test_project_manifest_relative_path_stays_anchored_after_chdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    workflow_dir = project_dir / "workflows" / "first"
    _write_workflow_yaml(
        workflow_dir / "typeflux.yaml",
        yaml_name="first",
        workflow_name="FirstWorkflow",
    )
    _write_reference_files(project_dir)
    _write_project_yaml(
        project_dir,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: first
            directory: ./workflows/../workflows/first
        environments:
          local: ./environments/../environments/local.yaml
        policies:
          base: ./policies/../policies/base.yaml
        validation:
          targets:
            local:
              workflows: [first]
              environment: local
              policies: [base]
        """,
    )

    monkeypatch.chdir(project_dir)
    project = load_project_spec("typeflux.project.yaml")
    monkeypatch.chdir(tmp_path)

    discovered = discover_project_workflows(project)
    report = validate_project(project)

    assert project.manifest_path == project_dir / "typeflux.project.yaml"
    assert discovered[0].resolved_path == workflow_dir / "typeflux.yaml"
    assert report.ok is True


def test_project_manifest_default_path_is_anchored_when_model_constructed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    monkeypatch.chdir(project_dir)
    project = TypefluxProjectSpec.model_validate(
        {
            "version": "1",
            "name": "demo-project",
            "workflows": [{"id": "first", "path": "workflow.yaml"}],
        }
    )
    monkeypatch.chdir(tmp_path)

    discovered = discover_project_workflows(project)

    assert project.manifest_path == project_dir / "typeflux.project.yaml"
    assert project.project_dir == project_dir
    assert discovered[0].resolved_path == project_dir / "workflow.yaml"


def test_project_policy_loads_composes_and_hashes(tmp_path: Path) -> None:
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    env_dir = tmp_path / "environments"
    env_dir.mkdir()
    (env_dir / "local.yaml").write_text("name: local\n", encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "base.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: base
            providers:
              allowed:
                openai:
                  models: [gpt-4o-mini, gpt-4.1]
            observability:
              allowed_backends: [none, langfuse]
            imports:
              allowed_module_roots: [examples]
            """
        ),
        encoding="utf-8",
    )
    (policy_dir / "regulated.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: regulated
            extends: [base]
            providers:
              allowed:
                openai:
                  models: [gpt-4.1]
            observability:
              allowed_backends: [langfuse]
            runtime:
              temporal:
                allowed_regions: [us-east]
                require_tls: true
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            environments:
              local: environments/local.yaml
            policies:
              base: policies/base.yaml
              regulated: policies/regulated.yaml
            validation:
              targets:
                local:
                  workflows: [workflow]
                  environment: local
                  policies: [regulated]
            """,
        )
    )

    base = load_project_policy(project, "base")
    loaded = load_project_policies(project, ("regulated",))
    composed = compose_project_policies(project, ("regulated",))
    recomposed = compose_project_policies(project, ("regulated",))
    report = validate_project(project)

    assert base.name == "base"
    assert base.policy_id == "base"
    assert base.policy_path == policy_dir / "base.yaml"
    assert [policy.name for policy in loaded] == ["regulated"]
    assert composed.applied_policy_ids == ("base", "regulated")
    assert composed.payload["providers"]["allowed"]["openai"]["models"] == [
        "gpt-4.1",
    ]
    assert composed.payload["observability"]["allowed_backends"] == ["langfuse"]
    assert composed.payload["runtime"]["temporal"]["require_tls"] is True
    assert len(composed.policy_hash) == 64
    assert composed.policy_hash == recomposed.policy_hash
    assert report.ok is True


def test_project_policy_overlay_narrows_allowed_providers(tmp_path: Path) -> None:
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "base.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: base
            providers:
              allowed:
                openai:
                  models: [gpt-4.1, gpt-4o]
                anthropic:
                  models: [claude-3-5-sonnet]
            """
        ),
        encoding="utf-8",
    )
    (policy_dir / "regulated.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: regulated
            extends: [base]
            providers:
              allowed:
                openai:
                  models: [gpt-4.1]
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            policies:
              base: policies/base.yaml
              regulated: policies/regulated.yaml
            """,
        )
    )

    composed = compose_project_policies(project, ("regulated",))

    allowed = composed.payload["providers"]["allowed"]
    assert sorted(allowed) == ["openai"]
    assert allowed["openai"]["models"] == ["gpt-4.1"]


def test_project_policy_preserves_provider_allowance_without_model_constraint(
    tmp_path: Path,
) -> None:
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "base.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: base
            providers:
              allowed:
                fake: {}
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            policies:
              base: policies/base.yaml
            """,
        )
    )

    composed = compose_project_policies(project, ("base",))

    assert composed.payload["providers"]["allowed"] == {"fake": {}}


def test_project_policy_rejects_non_overlapping_provider_allowlists(tmp_path: Path) -> None:
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "base.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: base
            providers:
              allowed:
                openai:
                  models: [gpt-4.1]
            """
        ),
        encoding="utf-8",
    )
    (policy_dir / "regulated.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: regulated
            extends: [base]
            providers:
              allowed:
                anthropic:
                  models: [claude-3-5-sonnet]
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            policies:
              base: policies/base.yaml
              regulated: policies/regulated.yaml
            """,
        )
    )

    # Non-empty disjoint provider allow-lists are a policy contradiction, not an
    # implied deny-all. Use `providers.allowed: {}` for intentional deny-all.
    with pytest.raises(ProjectPolicyError, match="no overlapping keys"):
        compose_project_policies(project, ("regulated",))


def test_runtime_policy_guard_enforces_bound_provider_identity() -> None:
    # Regression for the admission/activity provider-identity mismatch: the guard
    # must enforce against the bound spec provider type (what the policy and
    # admission key on), not a caller-derived object name. Otherwise a provider
    # whose runtime object name differs from its spec type would pass admission
    # but be blocked at activity execution under a different key.
    from typeflux.project.policy import ComposedProjectPolicy
    from typeflux.project.policy_enforcement import (
        ProjectPolicyEnforcementError,
        RuntimePolicyGuard,
    )

    policy = ComposedProjectPolicy(
        selected_policy_ids=("p",),
        applied_policy_ids=("p",),
        policy_names=("p",),
        policy_hash="hash",
        payload={"providers": {"allowed": {"openai": {"models": ["gpt-4o-mini"]}}}},
    )
    guard = RuntimePolicyGuard(policy=policy, provider_name="openai")

    # Caller passes a differing object-derived name; the bound "openai" identity wins.
    guard.enforce_provider_model(provider_name="open-a-i", provider_model="gpt-4o-mini")

    # The bound identity still enforces the model allow-list.
    with pytest.raises(ProjectPolicyEnforcementError, match="gpt-4o"):
        guard.enforce_provider_model(provider_name="open-a-i", provider_model="gpt-4o")


def _semantics_guard(**semantics):
    from typeflux.project.policy import ComposedProjectPolicy
    from typeflux.project.policy_enforcement import RuntimePolicyGuard

    policy = ComposedProjectPolicy(
        selected_policy_ids=("p",),
        applied_policy_ids=("p",),
        policy_names=("p",),
        policy_hash="hash",
        payload={"semantics": semantics},
    )
    return RuntimePolicyGuard(policy=policy)


def test_moderation_policy_required_and_require_block() -> None:
    # #158 PR2: required mandates a moderator; require_block forbids flag-only.
    from typeflux.project.policy_enforcement import ProjectPolicyEnforcementError

    guard = _semantics_guard(required=True, require_block=True)

    with pytest.raises(ProjectPolicyEnforcementError, match="must declare moderation"):
        guard.enforce_moderation_config(
            activity_name="a", moderation_configured=False, on_violation=None
        )
    with pytest.raises(ProjectPolicyEnforcementError, match="on_violation='block'"):
        guard.enforce_moderation_config(
            activity_name="a", moderation_configured=True, on_violation="flag"
        )
    # A block-configured moderated activity satisfies both.
    guard.enforce_moderation_config(
        activity_name="a", moderation_configured=True, on_violation="block"
    )
    # No semantics policy → no constraint.
    _semantics_guard().enforce_moderation_config(
        activity_name="a", moderation_configured=False, on_violation=None
    )
    # require_block implies moderation must be present (no fail-open by omitting it):
    # require_block=True alone rejects an unmoderated activity even without required.
    with pytest.raises(ProjectPolicyEnforcementError, match="must declare moderation"):
        _semantics_guard(require_block=True).enforce_moderation_config(
            activity_name="a", moderation_configured=False, on_violation=None
        )


def test_moderation_policy_block_escalates_on_category_and_score() -> None:
    # #158 PR2: policy can force a block on a disallowed category or score, even
    # when the activity itself only flags.
    cat_guard = _semantics_guard(categories=["hate", "violence"])
    assert (
        cat_guard.moderation_policy_block(
            activity_name="a", categories=("violence",), max_score=0.1
        )
        == "moderation policy blocked activity 'a': disallowed category violence"
    )
    assert (
        cat_guard.moderation_policy_block(activity_name="a", categories=("benign",), max_score=None)
        is None
    )

    score_guard = _semantics_guard(score_threshold=0.8)
    assert "score 0.9 >= threshold 0.8" in score_guard.moderation_policy_block(
        activity_name="a", categories=(), max_score=0.9
    )
    assert (
        score_guard.moderation_policy_block(activity_name="a", categories=(), max_score=0.5) is None
    )
    # No verdict score → threshold can't fire.
    assert (
        score_guard.moderation_policy_block(activity_name="a", categories=(), max_score=None)
        is None
    )


def test_validate_semantics_admission_over_yaml_activities() -> None:
    # #158 follow-up: with moderation in the YAML activity spec, admission verifies
    # required/require_block before deploy (no longer a pure runtime backstop).
    from types import SimpleNamespace

    from typeflux.project.policy_enforcement import _validate_semantics
    from typeflux.yaml.spec import ActivityDefinitionSpec, ModerationSpec

    def resolved_with(definitions: list[ActivityDefinitionSpec]) -> Any:
        return SimpleNamespace(
            spec=SimpleNamespace(activities=SimpleNamespace(definitions=definitions))
        )

    unmoderated = ActivityDefinitionSpec(name="a", input="m:I", output="m:O", prompt="p")
    blocked = ActivityDefinitionSpec(
        name="b",
        input="m:I",
        output="m:O",
        prompt="p",
        moderation=ModerationSpec(provider="openai", on_violation="block"),
    )
    flagged = ActivityDefinitionSpec(
        name="c",
        input="m:I",
        output="m:O",
        prompt="p",
        moderation=ModerationSpec(provider="openai", on_violation="flag"),
    )

    required = {"semantics": {"required": True}}
    fail = _validate_semantics(resolved_with([unmoderated, blocked]), required)
    assert fail.status == "failed"
    assert "must declare moderation" in (fail.message or "")

    assert _validate_semantics(resolved_with([blocked]), required).status == "passed"

    require_block = {"semantics": {"require_block": True}}
    block_fail = _validate_semantics(resolved_with([flagged]), require_block)
    assert block_fail.status == "failed"
    assert "on_violation='block'" in (block_fail.message or "")

    # No semantics policy → skipped (unconstrained).
    assert _validate_semantics(resolved_with([unmoderated]), {}).status == "skipped"


def test_validate_imports_governs_moderator_callables() -> None:
    # #158 review (Bugbot): the project-policy imports layer must govern custom
    # moderator callables like other type: custom extension imports — a locked
    # policy can't be bypassed by an activity moderator outside policy roots.
    from types import SimpleNamespace

    from typeflux.project.policy_enforcement import _validate_imports
    from typeflux.yaml.spec import (
        ActivityDefinitionSpec,
        ImportPolicySpec,
        ModerationSpec,
    )

    def resolved_with(definitions: list[ActivityDefinitionSpec], *, allow: bool = True) -> Any:
        runtime = SimpleNamespace(
            imports=ImportPolicySpec(allow_moderator_callable=allow),
            provider=SimpleNamespace(provider_class=None),
            registry=SimpleNamespace(registry_class=None),
            observability=SimpleNamespace(backend_class=None),
        )
        return SimpleNamespace(
            spec=SimpleNamespace(
                project="proj",
                runtime=runtime,
                activities=SimpleNamespace(definitions=definitions, modules=[]),
            )
        )

    custom = ActivityDefinitionSpec(
        name="c",
        input="m:I",
        output="m:O",
        prompt="p",
        moderation=ModerationSpec(moderator="other_pkg.mod:fn", on_violation="block"),
    )

    # Policy forbids moderator callables → both the broader flag and the activity fail.
    forbid = _validate_imports(
        resolved_with([custom], allow=True), {"imports": {"allow_moderator_callable": False}}
    )
    assert forbid.status == "failed"
    assert "allow_moderator_callable is broader" in (forbid.message or "")
    assert "moderation.moderator is not allowed" in (forbid.message or "")

    # Policy allows callables but restricts roots → an out-of-root moderator fails.
    roots = _validate_imports(
        resolved_with([custom]), {"imports": {"allowed_module_roots": ["approved_pkg"]}}
    )
    assert roots.status == "failed"
    assert "outside policy roots" in (roots.message or "")

    # An in-root moderator passes the root check.
    in_root = ActivityDefinitionSpec(
        name="d",
        input="m:I",
        output="m:O",
        prompt="p",
        moderation=ModerationSpec(moderator="approved_pkg.mod:fn", on_violation="block"),
    )
    ok = _validate_imports(
        resolved_with([in_root]), {"imports": {"allowed_module_roots": ["approved_pkg"]}}
    )
    assert ok.status == "passed"

    # #382: a built-in provider moderator (provider: gemini) is NOT a callable
    # import, so a locked allow_moderator_callable: false must not reject it — the
    # gate keys on moderation.moderator, which the provider path leaves unset.
    gemini_builtin = ActivityDefinitionSpec(
        name="g",
        input="m:I",
        output="m:O",
        prompt="p",
        moderation=ModerationSpec(provider="gemini", on_violation="block"),
    )
    builtin_ok = _validate_imports(
        resolved_with([gemini_builtin], allow=False),
        {"imports": {"allow_moderator_callable": False}},
    )
    assert builtin_ok.status == "passed"


def test_moderation_policy_merges_most_restrictively(tmp_path: Path) -> None:
    # #158 PR2: composing two policies unions categories, takes the lower
    # (stricter) score_threshold, and ORs required/require_block.
    from typeflux.project.policy import compose_project_policies

    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "a.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: a
            semantics:
              required: true
              categories: [hate]
              score_threshold: 0.9
            """
        ),
        encoding="utf-8",
    )
    (policy_dir / "b.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: b
            semantics:
              require_block: true
              categories: [violence]
              score_threshold: 0.6
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            policies:
              a: policies/a.yaml
              b: policies/b.yaml
            """,
        )
    )
    composed = compose_project_policies(project, ("a", "b"))
    semantics = composed.payload["semantics"]
    assert semantics["required"] is True
    assert semantics["require_block"] is True
    assert sorted(semantics["categories"]) == ["hate", "violence"]
    assert semantics["score_threshold"] == 0.6  # the stricter (lower) wins


def test_project_policy_hash_is_independent_of_selection_order(tmp_path: Path) -> None:
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "p1.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: p1
            observability:
              allowed_backends: [none, langfuse]
            """
        ),
        encoding="utf-8",
    )
    (policy_dir / "p2.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: p2
            observability:
              allowed_backends: [langfuse, none]
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            policies:
              p1: policies/p1.yaml
              p2: policies/p2.yaml
            """,
        )
    )

    forward = compose_project_policies(project, ("p1", "p2"))
    reverse = compose_project_policies(project, ("p2", "p1"))

    assert forward.policy_hash == reverse.policy_hash
    assert forward.payload["observability"]["allowed_backends"] == ["langfuse", "none"]
    assert reverse.payload["observability"]["allowed_backends"] == ["langfuse", "none"]


def test_project_policy_rejects_unknown_fields(tmp_path: Path) -> None:
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "bad.yaml").write_text(
        "version: '1'\nname: bad\nfuture_policy_rules:\n  require_review: later\n",
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            policies:
              bad: policies/bad.yaml
            """,
        )
    )

    report = validate_project(project)

    assert report.ok is False
    assert [issue.code for issue in report.issues] == ["invalid_policy_yaml"]


def test_project_policy_rejects_unknown_extends(tmp_path: Path) -> None:
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "regulated.yaml").write_text(
        "version: '1'\nname: regulated\nextends: [missing]\n",
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            policies:
              regulated: policies/regulated.yaml
            """,
        )
    )

    with pytest.raises(ProjectPolicyError, match="unknown project policy"):
        compose_project_policies(project, ("regulated",))

    report = validate_project(project)

    assert report.ok is False
    assert {issue.code for issue in report.issues} == {"invalid_policy_composition"}


def test_project_policy_merges_boolean_constraints_by_polarity(tmp_path: Path) -> None:
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "base.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: base
            runtime:
              temporal:
                require_tls: true
            imports:
              allow_provider_class: true
            """
        ),
        encoding="utf-8",
    )
    (policy_dir / "unsafe.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: unsafe
            runtime:
              temporal:
                require_tls: false
            imports:
              allow_provider_class: false
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            policies:
              base: policies/base.yaml
              unsafe: policies/unsafe.yaml
            validation:
              targets:
                local:
                  workflows: [workflow]
                  policies: [base, unsafe]
            """,
        )
    )

    composed = compose_project_policies(project, ("base", "unsafe"))

    assert composed.payload["runtime"]["temporal"]["require_tls"] is True
    assert composed.payload["imports"]["allow_provider_class"] is False
    report = validate_project(project)

    assert report.ok is True


def test_project_policy_rejects_conflicting_ambiguous_numeric_constraints(tmp_path: Path) -> None:
    # Retry/backoff scalars have no clean monotonic "stricter" direction, so
    # conflicting values still hard-fail (no silent blanket min/max).
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "base.yaml").write_text(
        "version: '1'\nname: base\nruntime:\n  provider_retry:\n    max_attempts: 3\n",
        encoding="utf-8",
    )
    (policy_dir / "unsafe.yaml").write_text(
        "version: '1'\nname: unsafe\nruntime:\n  provider_retry:\n    max_attempts: 5\n",
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            policies:
              base: policies/base.yaml
              unsafe: policies/unsafe.yaml
            validation:
              targets:
                local:
                  workflows: [workflow]
                  policies: [base, unsafe]
            """,
        )
    )

    with pytest.raises(ProjectPolicyError, match="conflicting project policy values"):
        compose_project_policies(project, ("base", "unsafe"))

    report = validate_project(project)

    assert report.ok is False
    assert {issue.code for issue in report.issues} == {"invalid_target_policy_composition"}


def test_project_policy_merges_monotonic_numerics_to_most_restrictive(tmp_path: Path) -> None:
    # max_bytes / max_concurrent merge to the smaller (stricter) value;
    # min_interval_seconds merges to the larger (stricter) value, at any depth.
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "base.yaml").write_text(
        _dedent(
            """
            version: '1'
            name: base
            artifacts:
              max_bytes: 2048
            runtime:
              provider_limits:
                default:
                  max_concurrent: 8
                  min_interval_seconds: 0.1
                providers:
                  openai:
                    limits:
                      max_concurrent: 6
            """
        ),
        encoding="utf-8",
    )
    (policy_dir / "strict.yaml").write_text(
        _dedent(
            """
            version: '1'
            name: strict
            artifacts:
              max_bytes: 1024
            runtime:
              provider_limits:
                default:
                  max_concurrent: 4
                  min_interval_seconds: 0.25
                providers:
                  openai:
                    limits:
                      max_concurrent: 3
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            policies:
              base: policies/base.yaml
              strict: policies/strict.yaml
            """,
        )
    )

    composed = compose_project_policies(project, ("base", "strict"))
    payload = composed.payload

    assert payload["artifacts"]["max_bytes"] == 1024
    limits = payload["runtime"]["provider_limits"]
    assert limits["default"]["max_concurrent"] == 4
    assert limits["default"]["min_interval_seconds"] == 0.25
    assert limits["providers"]["openai"]["limits"]["max_concurrent"] == 3


def test_project_policy_rejects_conflicting_allowlists(tmp_path: Path) -> None:
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "base.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: base
            observability:
              allowed_backends: [langfuse]
            """
        ),
        encoding="utf-8",
    )
    (policy_dir / "local.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: local
            extends: [base]
            observability:
              allowed_backends: [none]
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            policies:
              base: policies/base.yaml
              local: policies/local.yaml
            validation:
              targets:
                local:
                  workflows: [workflow]
                  policies: [local]
            """,
        )
    )

    # Non-empty disjoint sequence allow-lists are a policy contradiction, not an
    # implied deny-all. Use `allowed_backends: []` for intentional deny-all.
    with pytest.raises(ProjectPolicyError, match="conflicting project policy allow-list"):
        compose_project_policies(project, ("local",))

    report = validate_project(project)

    assert report.ok is False
    assert {issue.code for issue in report.issues} == {"invalid_policy_composition"}


def test_project_policy_empty_allowlists_compose_as_deny_all(tmp_path: Path) -> None:
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "deny_all.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: deny_all
            providers:
              allowed: {}
            observability:
              allowed_backends: []
            """
        ),
        encoding="utf-8",
    )
    (policy_dir / "local.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: local
            extends: [deny_all]
            providers:
              allowed:
                openai: {}
            observability:
              allowed_backends: [langfuse]
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            policies:
              deny_all: policies/deny_all.yaml
              local: policies/local.yaml
            """,
        )
    )

    composed = compose_project_policies(project, ("local",))

    assert composed.payload["providers"]["allowed"] == {}
    assert composed.payload["observability"]["allowed_backends"] == []


def test_project_policy_composition_continues_after_unrelated_missing_policy_file(
    tmp_path: Path,
) -> None:
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "regulated.yaml").write_text(
        "version: '1'\nname: regulated\nextends: [unknown_parent]\n",
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            policies:
              missing: policies/missing.yaml
              regulated: policies/regulated.yaml
            """,
        )
    )

    report = validate_project(project)

    assert report.ok is False
    assert {issue.code for issue in report.issues} == {
        "missing_policy_file",
        "invalid_policy_composition",
    }


def test_project_policy_target_composition_continues_after_unrelated_unknown_policy(
    tmp_path: Path,
) -> None:
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "base.yaml").write_text(
        "version: '1'\nname: base\nruntime:\n  provider_retry:\n    max_attempts: 3\n",
        encoding="utf-8",
    )
    (policy_dir / "unsafe.yaml").write_text(
        "version: '1'\nname: unsafe\nruntime:\n  provider_retry:\n    max_attempts: 5\n",
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            policies:
              base: policies/base.yaml
              unsafe: policies/unsafe.yaml
            validation:
              targets:
                bad_ref:
                  workflows: [workflow]
                  policies: [unknown]
                conflict:
                  workflows: [workflow]
                  policies: [base, unsafe]
            """,
        )
    )

    report = validate_project(project)

    assert report.ok is False
    assert {issue.code for issue in report.issues} == {
        "unknown_target_policy",
        "invalid_target_policy_composition",
    }


def test_project_workflow_specs_load_and_reject_duplicate_workflow_names(
    tmp_path: Path,
) -> None:
    _write_workflow_yaml(tmp_path / "first.yaml", yaml_name="first")
    _write_workflow_yaml(tmp_path / "second.yaml", yaml_name="second")
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: first
                path: first.yaml
              - id: second
                path: second.yaml
            """,
        )
    )

    with pytest.raises(ValueError, match="duplicate YAML workflow name"):
        load_project_workflow_specs(project)

    report = validate_project(project)
    assert report.ok is False
    assert [issue.code for issue in report.issues] == ["duplicate_workflow_name"]


def test_project_environment_profile_resolves_workflow_with_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_env_override_workflow_yaml(tmp_path / "workflow.yaml")
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "\n".join(
            [
                "TEMPORAL_API_KEY=cloud-secret",
                "TEMPORAL_NAMESPACE=file-namespace",
                "TYPEFLUX_OPENAI_MODEL=file-model",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "env.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: cloud
            env_files:
              - path: runtime.env
                required: true
            variables:
              TYPEFLUX_ENVIRONMENT: cloud
              TYPEFLUX_DEPLOYMENT_ID: cloud-dev
              TYPEFLUX_TEMPORAL_REGION: us-east
              TEMPORAL_ADDRESS: cloud.tmprl.cloud:7233
              TEMPORAL_NAMESPACE: profile-namespace
            overrides:
              runtime:
                temporal:
                  address: ${TEMPORAL_ADDRESS}
                  namespace: ${TEMPORAL_NAMESPACE}
                  tls: true
                  api_key: ${TEMPORAL_API_KEY}
                provider:
                  type: openai
                  model: ${TYPEFLUX_OPENAI_MODEL}
                observability:
                  type: langfuse
            workflows:
              workflow:
                overrides:
                  task_queue: workflow-cloud-queue
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            environments:
              cloud: env.yaml
            """,
        )
    )
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "shell-namespace")

    resolved = resolve_project_workflow(project, workflow_id="workflow", environment_id="cloud")
    summary = resolved.summary().model_dump(mode="json")

    assert resolved.spec.task_queue == "workflow-cloud-queue"
    assert resolved.spec.runtime.temporal.address == "cloud.tmprl.cloud:7233"
    assert resolved.spec.runtime.temporal.namespace == "profile-namespace"
    assert resolved.spec.runtime.temporal.tls is True
    assert resolved.spec.runtime.temporal.api_key == "cloud-secret"
    assert resolved.spec.runtime.provider.type == "openai"
    assert resolved.spec.runtime.provider.model == "file-model"
    assert resolved.spec.runtime.observability.type == "langfuse"
    assert summary["temporal"]["api_key_configured"] is True
    assert summary["provider"]["api_key_configured"] is False
    assert "cloud-secret" not in json.dumps(summary)
    metadata = workflow_invocation_metadata(
        workflow_name=resolved.spec.workflow.name,
        workflow_id="project-workflow-test",
        task_queue=resolved.spec.task_queue,
        metadata_contributors=_yaml_metadata_contributors(resolved.spec),
    )
    override_payload = {
        "source": "project_environment",
        "project_name": "demo-project",
        "environment_id": "cloud",
        "environment_name": "cloud",
        "workflow_id": "workflow",
        "override_paths": [
            "runtime.observability.type",
            "runtime.provider.model",
            "runtime.provider.type",
            "runtime.temporal.address",
            "runtime.temporal.api_key",
            "runtime.temporal.namespace",
            "runtime.temporal.tls",
            "task_queue",
        ],
    }
    assert metadata["typeflux"]["yaml_overrides"] == override_payload
    assert (
        metadata["typeflux"]["execution_manifest"]["contributions"]["yaml_overrides"]
        == override_payload
    )
    assert "cloud-secret" not in json.dumps(metadata)
    assert summary["environment"]["profile_variable_names"] == [
        "TEMPORAL_ADDRESS",
        "TEMPORAL_NAMESPACE",
        "TYPEFLUX_DEPLOYMENT_ID",
        "TYPEFLUX_ENVIRONMENT",
        "TYPEFLUX_TEMPORAL_REGION",
    ]


def test_project_summary_treats_empty_provider_strings_as_unconfigured(tmp_path: Path) -> None:
    _write_env_override_workflow_yaml(tmp_path / "workflow.yaml")
    (tmp_path / "env.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: local
            overrides:
              runtime:
                provider:
                  api_key: ""
                  base_url: ""
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            environments:
              local: env.yaml
            """,
        )
    )

    resolved = resolve_project_workflow(project, workflow_id="workflow", environment_id="local")
    summary = resolved.summary().model_dump(mode="json")

    assert resolved.spec.runtime.provider.api_key == ""
    assert resolved.spec.runtime.provider.base_url == ""
    assert summary["provider"]["api_key_configured"] is False
    assert summary["provider"]["base_url_configured"] is False


def _write_base_env_workflow(path: Path, queue_expr: str) -> None:
    path.write_text(
        _dedent(
            f"""
            project: demo_project
            name: workflow
            task_queue: {queue_expr}
            runtime:
              temporal: {{}}
              registry: {{ type: inline, prompts: {{ first: hi }} }}
              provider: {{ type: fake }}
            activities:
              definitions:
                - {{ name: first, input: schemas:InputModel, output: schemas:OutputModel, prompt: first }}
            workflow:
              name: DemoWorkflow
              input: schemas:InputModel
              output: schemas:OutputModel
              steps:
                - {{ id: first, activity: first }}
            """
        ),
        encoding="utf-8",
    )


def _base_env_project(tmp_path: Path, env_yaml: str) -> TypefluxProjectSpec:
    _write_base_env_workflow(tmp_path / "workflow.yaml", "${TF760_QUEUE}")
    (tmp_path / "env.yaml").write_text(_dedent(env_yaml), encoding="utf-8")
    return load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            environments:
              local: env.yaml
            """,
        )
    )


def test_resolve_project_workflow_base_env_is_hermetic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #760: `base_env` is the ONLY interpolation source — the poison in the real process
    # environment must never reach the resolved spec, and a var present only in the
    # injected map resolves. os.environ is NOT mutated (the hermetic guarantee).
    monkeypatch.setenv("TF760_QUEUE", "POISON")
    project = _base_env_project(tmp_path, "version: '1'\nname: local\n")
    environ_before = dict(os.environ)

    resolved = resolve_project_workflow(
        project,
        workflow_id="workflow",
        environment_id="local",
        base_env={"TF760_QUEUE": "hermetic"},
    )

    assert resolved.spec.task_queue == "hermetic"
    # The base is RETAINED on the artifact so downstream closure walks reuse it.
    assert resolved.base_env == {"TF760_QUEUE": "hermetic"}
    # STRICT no-mutation: the ENTIRE process environment (key set + values) is
    # byte-identical after the hermetic resolution — nothing added, removed, or
    # rewritten (catches accidental writes, not just the poison key surviving).
    assert dict(os.environ) == environ_before


def test_resolve_project_workflow_default_reads_process_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Omitting base_env preserves the pre-#760 behavior: interpolation reads os.environ
    # under the environment overlay.
    monkeypatch.setenv("TF760_QUEUE", "from-shell")
    project = _base_env_project(tmp_path, "version: '1'\nname: local\n")

    resolved = resolve_project_workflow(project, workflow_id="workflow", environment_id="local")

    assert resolved.spec.task_queue == "from-shell"


def test_resolve_project_workflow_variables_overlay_wins_over_base_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The environment's `variables:` overlay layers OVER the injected base (overlay wins),
    # matching the overlay-over-os.environ precedence of the default path.
    monkeypatch.setenv("TF760_QUEUE", "POISON")
    project = _base_env_project(
        tmp_path,
        "version: '1'\nname: local\nvariables:\n  TF760_QUEUE: from-overlay\n",
    )

    resolved = resolve_project_workflow(
        project,
        workflow_id="workflow",
        environment_id="local",
        base_env={"TF760_QUEUE": "from-base"},
    )

    assert resolved.spec.task_queue == "from-overlay"


def test_resolve_project_workflow_base_env_missing_key_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A `${VAR}` absent from the injected map, with no default, errors exactly as an unset
    # shell variable would — no silent os.environ fallback even though it is set there.
    monkeypatch.setenv("TF760_QUEUE", "POISON")
    project = _base_env_project(tmp_path, "version: '1'\nname: local\n")

    with pytest.raises(KeyError, match="missing environment variable: TF760_QUEUE"):
        resolve_project_workflow(
            project,
            workflow_id="workflow",
            environment_id="local",
            base_env={},
        )


def test_project_summary_reports_secret_reference_configuration_without_values(
    tmp_path: Path,
) -> None:
    _write_env_override_workflow_yaml(tmp_path / "workflow.yaml")
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "\n".join(
            [
                "TEMPORAL_API_KEY=temporal-secret",
                "OPENAI_API_KEY=openai-secret",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "env.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: local
            env_files:
              - path: runtime.env
                required: true
            overrides:
              runtime:
                temporal:
                  tls: true
                  api_key:
                    value_from:
                      env: TEMPORAL_API_KEY
                provider:
                  api_key:
                    value_from:
                      env: OPENAI_API_KEY
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            environments:
              local: env.yaml
            """,
        )
    )

    resolved = resolve_project_workflow(project, workflow_id="workflow", environment_id="local")
    summary = resolved.summary().model_dump(mode="json")
    with project_environment_context(resolved.application):
        metadata = workflow_invocation_metadata(
            workflow_name=resolved.spec.workflow.name,
            workflow_id="project-secret-ref-test",
            task_queue=resolved.spec.task_queue,
            metadata_contributors=_yaml_metadata_contributors(resolved.spec),
        )

    assert summary["temporal"]["api_key_configured"] is True
    assert summary["provider"]["api_key_configured"] is True
    references = sorted(
        metadata["typeflux"]["secret_references"]["references"],
        key=lambda item: item["runtime_path"],
    )
    assert references == [
        {
            "runtime_path": "runtime.provider.api_key",
            "source_kind": "env",
            "source_name": "OPENAI_API_KEY",
            "configured": True,
        },
        {
            "runtime_path": "runtime.temporal.api_key",
            "source_kind": "env",
            "source_name": "TEMPORAL_API_KEY",
            "configured": True,
        },
    ]
    payload = json.dumps({"summary": summary, "metadata": metadata})
    assert "openai-secret" not in payload
    assert "temporal-secret" not in payload


def test_project_environment_context_restores_env_and_suppresses_implicit_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_env_override_workflow_yaml(tmp_path / "workflow.yaml")
    (tmp_path / ".env").write_text("TEMPORAL_ADDRESS=from-cwd-env:7233\n", encoding="utf-8")
    (tmp_path / "env.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: local
            variables:
              TEMPORAL_ADDRESS: from-profile:7233
            overrides:
              runtime:
                temporal:
                  address: ${TEMPORAL_ADDRESS}
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            environments:
              local: env.yaml
            """,
        )
    )
    resolved = resolve_project_workflow(project, workflow_id="workflow", environment_id="local")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEMPORAL_ADDRESS", "from-shell:7233")
    monkeypatch.delenv("TYPEFLUX_ENV_FILE", raising=False)

    with project_environment_context(resolved.application):
        assert os.environ["TEMPORAL_ADDRESS"] == "from-profile:7233"
        assert os.environ["TYPEFLUX_ENV_FILE"].endswith(".typeflux-project-env-do-not-load")

    assert os.environ["TEMPORAL_ADDRESS"] == "from-shell:7233"
    assert "TYPEFLUX_ENV_FILE" not in os.environ


def test_run_while_env_lock_held_cancellation_waits_for_the_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a caller mid-worker must not unwind the env context first.

    A worker thread cannot be stopped, so when a cancellation (e.g. the
    control-plane tier timeout) lands while the worker is running, the env
    context's teardown must wait for it — otherwise os.environ is restored
    while the orphaned worker still reads the environment, and it observes
    another environment's (or the shell's) values.
    """
    import asyncio
    import threading

    from typeflux.project.environment import (
        async_project_environment_context,
        run_while_env_lock_held,
    )

    _write_env_override_workflow_yaml(tmp_path / "workflow.yaml")
    (tmp_path / "env.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: local
            variables:
              TEMPORAL_ADDRESS: from-profile:7233
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            environments:
              local: env.yaml
            """,
        )
    )
    resolved = resolve_project_workflow(project, workflow_id="workflow", environment_id="local")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEMPORAL_ADDRESS", "from-shell:7233")
    monkeypatch.delenv("TYPEFLUX_ENV_FILE", raising=False)

    started = threading.Event()
    release = threading.Event()
    seen: list[str | None] = []

    def orphaned_read() -> None:
        started.set()
        release.wait(timeout=10)
        seen.append(os.environ.get("TEMPORAL_ADDRESS"))

    async def flow() -> None:
        async with async_project_environment_context(resolved.application):
            await run_while_env_lock_held(orphaned_read)

    async def main() -> None:
        task = asyncio.create_task(flow())
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        # Give an unwind-too-early bug room to restore the env before the
        # worker's read; the fixed path is still blocked waiting it out.
        await asyncio.sleep(0.1)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        while not seen:
            await asyncio.sleep(0.01)

    asyncio.run(main())
    assert seen == ["from-profile:7233"], (
        "orphaned worker outlived its env context and read a foreign environment"
    )
    assert os.environ["TEMPORAL_ADDRESS"] == "from-shell:7233"


def test_project_environment_optional_env_file_is_skipped(tmp_path: Path) -> None:
    _write_env_override_workflow_yaml(tmp_path / "workflow.yaml")
    (tmp_path / "env.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: local
            env_files:
              - path: missing.env
                required: false
            variables:
              TEMPORAL_ADDRESS: local:7233
            overrides:
              runtime:
                temporal:
                  address: ${TEMPORAL_ADDRESS}
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            environments:
              local: env.yaml
            """,
        )
    )

    resolved = resolve_project_workflow(project, workflow_id="workflow", environment_id="local")

    assert resolved.spec.runtime.temporal.address == "local:7233"
    assert resolved.application.env_files[0].loaded is False


def test_project_environment_missing_required_env_file_rejected(tmp_path: Path) -> None:
    _write_env_override_workflow_yaml(tmp_path / "workflow.yaml")
    (tmp_path / "env.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: cloud
            env_files:
              - path: missing.env
                required: true
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            environments:
              cloud: env.yaml
            """,
        )
    )

    report = validate_project(project)
    assert report.ok is False
    assert report.issues[0].code == "invalid_environment_yaml"
    assert "required environment env file" in report.issues[0].message

    with pytest.raises(ProjectEnvironmentError, match="required environment env file"):
        resolve_project_workflow(project, workflow_id="workflow", environment_id="cloud")


def test_project_environment_invalid_override_key_rejected(tmp_path: Path) -> None:
    _write_env_override_workflow_yaml(tmp_path / "workflow.yaml")
    (tmp_path / "env.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: local
            overrides:
              workflow:
                name: OtherWorkflow
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            environments:
              local: env.yaml
            """,
        )
    )

    with pytest.raises(ValueError, match="not an allowed environment override"):
        load_project_environment(project, "local")


def test_project_environment_unknown_workflow_override_rejected(tmp_path: Path) -> None:
    _write_env_override_workflow_yaml(tmp_path / "workflow.yaml")
    (tmp_path / "env.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: local
            workflows:
              missing:
                overrides:
                  task_queue: missing
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            environments:
              local: env.yaml
            """,
        )
    )

    with pytest.raises(ProjectEnvironmentError, match="unknown workflow"):
        load_project_environment(project, "local")


def test_validate_project_reports_invalid_environment_profile(tmp_path: Path) -> None:
    _write_env_override_workflow_yaml(tmp_path / "workflow.yaml")
    (tmp_path / "env.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: local
            workflows:
              missing:
                overrides:
                  task_queue: missing
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            environments:
              local: env.yaml
            """,
        )
    )

    report = validate_project(project)

    assert report.ok is False
    assert report.issues[0].code == "invalid_environment_yaml"
    assert "unknown workflow" in report.issues[0].message


def test_project_manifest_rejects_duplicate_workflow_ids(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate project workflow id"):
        load_project_spec(
            _write_project_yaml(
                tmp_path,
                """
                version: "1"
                name: demo-project
                workflows:
                  - id: duplicate
                    path: first.yaml
                  - id: duplicate
                    path: second.yaml
                """,
            )
        )


def test_project_manifest_rejects_path_and_directory_together(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one of path or directory"):
        load_project_spec(
            _write_project_yaml(
                tmp_path,
                """
                version: "1"
                name: demo-project
                workflows:
                  - id: first
                    path: first.yaml
                    directory: workflows/first
                """,
            )
        )


def test_project_manifest_rejects_workflow_filename_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="workflow_filename must be a file name"):
        load_project_spec(
            _write_project_yaml(
                tmp_path,
                """
                version: "1"
                name: demo-project
                defaults:
                  workflow_filename: ../typeflux.yaml
                workflows:
                  - id: first
                    directory: workflows/first
                """,
            )
        )


def test_validate_project_reports_missing_workflow_file(tmp_path: Path) -> None:
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: missing
                path: missing.yaml
            """,
        )
    )

    report = validate_project(project)

    assert report.ok is False
    assert report.issues[0].code == "missing_workflow_file"
    assert report.issues[0].reference == "missing"


def test_validate_project_reports_invalid_workflow_yaml(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text("name: missing-required-fields\n", encoding="utf-8")
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: bad
                path: bad.yaml
            """,
        )
    )

    report = validate_project(project)

    assert report.ok is False
    assert report.issues[0].code == "invalid_workflow_yaml"
    assert report.issues[0].reference == "bad"


def test_validate_project_reports_missing_environment_and_policy_files(
    tmp_path: Path,
) -> None:
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            environments:
              local: environments/missing.yaml
            policies:
              base: policies/missing.yaml
            """,
        )
    )

    report = validate_project(project)

    assert report.ok is False
    assert {issue.code for issue in report.issues} == {
        "missing_environment_file",
        "missing_policy_file",
    }


def test_validate_project_reports_unknown_validation_target_references(
    tmp_path: Path,
) -> None:
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    _write_reference_files(tmp_path)
    project = load_project_spec(
        _write_project_yaml(
            tmp_path,
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            environments:
              local: environments/local.yaml
            policies:
              base: policies/base.yaml
            validation:
              targets:
                prod:
                  workflows: [missing_workflow]
                  environment: prod
                  policies: [missing_policy]
            """,
        )
    )

    report = validate_project(project)

    assert report.ok is False
    assert {issue.code for issue in report.issues} == {
        "unknown_target_environment",
        "unknown_target_policy",
        "unknown_target_workflow",
    }


def test_project_cli_lists_workflows(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        """,
    )

    exit_code = project_cli.main(["list", str(project_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "workflow" in output
    assert "DemoWorkflow" in output


def test_project_cli_validate_success_and_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        """,
    )

    assert project_cli.main(["validate", str(project_path)]) == 0
    assert "is valid" in capsys.readouterr().out

    assert project_cli.main(["validate", str(project_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["workflows"][0]["id"] == "workflow"


def test_validate_project_bundle_resolves_environment_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_env_override_workflow_yaml(tmp_path / "workflow.yaml")
    (tmp_path / "env.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: local
            variables:
              TEMPORAL_ADDRESS: profile:7233
            overrides:
              runtime:
                temporal:
                  address: ${TEMPORAL_ADDRESS}
              task_queue: profile-queue
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        """,
    )

    report = validate_project_bundle(
        load_project_spec(project_path),
        environment_id="local",
    )

    assert report.ok is True
    resolved = report.resolved_workflows[0]
    assert resolved.workflow_id == "workflow"
    assert resolved.task_queue == "profile-queue"
    assert {check.code: check.status for check in resolved.checks} == {
        "environment_workflow_resolution": "passed",
        "observability_config": "passed",
        "policy_enforcement": "skipped",
        "provider_import_policy": "passed",
        "activity_imports": "passed",
        "risk_tier_binding": "passed",
        "workflow_graph": "passed",
        "execution_manifest": "passed",
    }


def test_project_cli_validate_environment_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_env_override_workflow_yaml(tmp_path / "workflow.yaml")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        """,
    )

    exit_code = project_cli.main(
        [
            "validate",
            str(project_path),
            "--environment",
            "local",
            "--workflow",
            "workflow",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["resolved_workflows"][0]["workflow_id"] == "workflow"
    assert payload["resolved_workflows"][0]["checks"][-1]["code"] == "execution_manifest"


def test_project_cli_validate_selector_errors_are_json_reported(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        """,
    )

    exit_code = project_cli.main(
        [
            "validate",
            str(project_path),
            "--environment",
            "missing",
            "--workflow",
            "unknown",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 3  # validation/drift verdict (#818)
    assert {issue["code"] for issue in payload["issues"]} == {
        "unknown_validation_environment",
        "unknown_validation_workflow",
    }
    assert "resolved_workflows" not in payload


def test_validate_project_bundle_reports_provider_import_policy_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(
        tmp_path / "workflow.yaml",
        yaml_name="workflow",
        provider_class="external.provider:Provider",
    )
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        """,
    )

    report = validate_project_bundle(load_project_spec(project_path), environment_id="local")

    assert report.ok is False
    assert report.resolved_workflows[0].ok is False
    assert "resolved_provider_import_policy_failed" in {issue.code for issue in report.issues}


def test_validate_project_bundle_applies_policy_from_validation_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    (tmp_path / "env.yaml").write_text(
        'version: "1"\nname: local\nvariables:\n  TYPEFLUX_TEMPORAL_REGION: us-east\n',
        encoding="utf-8",
    )
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "future.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: future
            providers:
              allowed:
                fake: {}
            observability:
              allowed_backends: [none]
            review:
              require_review_routes: true
              invalid_user_decision: fail
            runtime:
              temporal:
                allowed_regions: [us-east]
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          future: policies/future.yaml
        validation:
          targets:
              local:
                workflows: [workflow]
                environment: local
                policies: [future]
        """,
    )

    report = validate_project_bundle(load_project_spec(project_path), environment_id="local")

    check_statuses = {check.code: check.status for check in report.resolved_workflows[0].checks}
    assert report.ok is False
    assert check_statuses["policy_selection"] == "passed"
    assert check_statuses["policy_provider"] == "passed"
    assert check_statuses["policy_observability"] == "passed"
    assert check_statuses["policy_temporal"] == "passed"
    assert check_statuses["policy_review"] == "failed"
    assert "resolved_policy_review_failed" in {issue.code for issue in report.issues}


def test_validate_project_bundle_applies_explicit_policy_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "openai-only.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: openai-only
            providers:
              allowed:
                openai: {}
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          openai-only: policies/openai-only.yaml
        """,
    )

    report = validate_project_bundle(
        load_project_spec(project_path),
        environment_id="local",
        policy_ids=("openai-only",),
    )

    assert report.ok is False
    assert "resolved_policy_provider_failed" in {issue.code for issue in report.issues}


def test_validate_project_bundle_admits_omitted_openai_model_against_default_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: an omitted openai model materializes to the default in the resolved
    # spec, so admission enforces the same concrete model the runtime would use. A
    # policy that allow-lists only that default must therefore admit the workflow,
    # not reject it for a None model.
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(
        tmp_path / "workflow.yaml",
        yaml_name="workflow",
        provider_type="openai",
        provider_model=None,
    )
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "openai-default.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: openai-default
            providers:
              allowed:
                openai:
                  models: [gpt-4o-mini]
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          openai-default: policies/openai-default.yaml
        """,
    )

    report = validate_project_bundle(
        load_project_spec(project_path),
        environment_id="local",
        policy_ids=("openai-default",),
    )

    check_statuses = {check.code: check.status for check in report.resolved_workflows[0].checks}
    assert check_statuses["policy_provider"] == "passed"
    assert "resolved_policy_provider_failed" not in {issue.code for issue in report.issues}


def test_validate_project_bundle_admits_omitted_anthropic_model_against_default_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Anthropic mirrors OpenAI's default-model materialization so project policy
    # admission sees the effective provider model, not None.
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(
        tmp_path / "workflow.yaml",
        yaml_name="workflow",
        provider_type="anthropic",
        provider_model=None,
    )
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "anthropic-default.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: anthropic-default
            providers:
              allowed:
                anthropic:
                  models: [claude-sonnet-4-6]
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          anthropic-default: policies/anthropic-default.yaml
        """,
    )

    report = validate_project_bundle(
        load_project_spec(project_path),
        environment_id="local",
        policy_ids=("anthropic-default",),
    )

    check_statuses = {check.code: check.status for check in report.resolved_workflows[0].checks}
    assert check_statuses["policy_provider"] == "passed"
    assert "resolved_policy_provider_failed" not in {issue.code for issue in report.issues}


def test_validate_project_bundle_requires_environment_for_explicit_policy_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "openai-only.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: openai-only
            providers:
              allowed:
                openai: {}
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          openai-only: policies/openai-only.yaml
        """,
    )

    report = validate_project_bundle(
        load_project_spec(project_path),
        policy_ids=("openai-only",),
    )

    assert report.ok is False
    assert report.resolved_workflows == ()
    assert {issue.code for issue in report.issues} == {"validation_environment_required"}


def test_validate_project_bundle_applies_environmentless_validation_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "openai-only.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: openai-only
            providers:
              allowed:
                openai: {}
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          openai-only: policies/openai-only.yaml
        validation:
          targets:
            all-envs:
              workflows: [workflow]
              policies: [openai-only]
        """,
    )

    report = validate_project_bundle(load_project_spec(project_path), environment_id="local")

    assert report.ok is False
    assert "resolved_policy_provider_failed" in {issue.code for issue in report.issues}


def test_validate_project_bundle_treats_empty_policy_allowlists_as_deny_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "empty.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: empty
            providers:
              allowed: {}
            observability:
              allowed_backends: []
            artifacts:
              allowed_sources: []
            imports:
              allow_provider_class: false
              allowed_module_roots: []
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          empty: policies/empty.yaml
        """,
    )

    report = validate_project_bundle(
        load_project_spec(project_path),
        environment_id="local",
        policy_ids=("empty",),
    )

    assert report.ok is False
    issue_codes = {issue.code for issue in report.issues}
    assert "resolved_policy_provider_failed" in issue_codes
    assert "resolved_policy_observability_failed" in issue_codes
    assert "resolved_policy_artifacts_failed" in issue_codes


def test_project_policy_payload_preserves_explicit_empty_allowlists() -> None:
    from typeflux.project.policy import TypefluxProjectPolicySpec

    policy = TypefluxProjectPolicySpec.model_validate(
        {
            "version": "1",
            "name": "empty",
            "providers": {"allowed": {"openai": {"models": []}}},
            "observability": {"allowed_backends": []},
            "runtime": {
                "temporal": {
                    "allowed_addresses": [],
                    "allowed_namespaces": [],
                    "allowed_regions": [],
                }
            },
            "artifacts": {"allowed_sources": [], "allowed_media_types": []},
            "imports": {"allowed_module_roots": []},
        }
    )

    payload = policy.to_payload()

    assert payload["providers"]["allowed"]["openai"]["models"] == []
    assert payload["observability"]["allowed_backends"] == []
    assert payload["runtime"]["temporal"]["allowed_addresses"] == []
    assert payload["runtime"]["temporal"]["allowed_namespaces"] == []
    assert payload["runtime"]["temporal"]["allowed_regions"] == []
    assert payload["artifacts"]["allowed_sources"] == []
    assert payload["artifacts"]["allowed_media_types"] == []
    assert payload["imports"]["allowed_module_roots"] == []


def test_project_policy_payload_omits_absent_allowlists() -> None:
    from typeflux.project.policy import TypefluxProjectPolicySpec

    # Absent allow-lists (no opinion) must not be treated as explicit-empty.
    policy = TypefluxProjectPolicySpec.model_validate(
        {
            "version": "1",
            "name": "sparse",
            "imports": {"allow_provider_class": False},
            "providers": {"allowed": {"openai": {}}},
        }
    )

    payload = policy.to_payload()

    assert "allowed_module_roots" not in payload["imports"]
    assert "models" not in payload["providers"]["allowed"]["openai"]


def test_validate_project_bundle_reports_observability_temporal_and_review_policy_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_env_override_workflow_yaml(tmp_path / "workflow.yaml")
    (tmp_path / "env.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: local
            variables:
              TYPEFLUX_TEMPORAL_REGION: eu-west
            """
        ),
        encoding="utf-8",
    )
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "regulated.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: regulated
            observability:
              required: true
              allowed_backends: [langfuse]
              redaction:
                required: true
                preserve_typeflux_metadata: true
            runtime:
              temporal:
                allowed_regions: [us-east]
                require_tls: true
                require_api_key: true
            review:
              require_review_routes: true
              invalid_user_decision: fail
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          regulated: policies/regulated.yaml
        """,
    )

    report = validate_project_bundle(
        load_project_spec(project_path),
        environment_id="local",
        policy_ids=("regulated",),
    )

    issue_codes = {issue.code for issue in report.issues}
    assert report.ok is False
    assert "resolved_policy_observability_failed" in issue_codes
    assert "resolved_policy_temporal_failed" in issue_codes
    assert "resolved_policy_review_failed" in issue_codes


def test_validate_project_bundle_reports_project_import_policy_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(
        tmp_path / "workflow.yaml",
        yaml_name="workflow",
        provider_class="external.provider:Provider",
        allow_provider_class=True,
        allowed_module_roots=["external"],
    )
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "locked.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: locked
            imports:
              allow_provider_class: false
              allow_absolute_activity_modules: false
              allowed_module_roots:
                - demo_project
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          locked: policies/locked.yaml
        """,
    )

    report = validate_project_bundle(
        load_project_spec(project_path),
        environment_id="local",
        policy_ids=("locked",),
    )

    assert report.ok is False
    assert "resolved_policy_imports_failed" in {issue.code for issue in report.issues}


def test_validate_project_bundle_reports_artifact_policy_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    (tmp_path / "workflow.yaml").write_text(
        _dedent(
            """
            project: demo_project
            name: workflow
            task_queue: demo-task-queue
            runtime:
              temporal:
                address: localhost:7233
              registry:
                type: inline
                prompts:
                  first: first {{value}}
              provider:
                type: fake
              artifacts:
                allowed_sources: [url]
                allowed_media_types: [image/png]
                max_bytes: 4096
            activities:
              definitions:
                - name: first
                  input: schemas:InputModel
                  output: schemas:OutputModel
                  prompt: first
                  artifacts:
                    - name: contract
                      from: input.contract
                      media_types: [application/pdf]
                      max_bytes: 8192
            workflow:
              name: DemoWorkflow
              input: schemas:InputModel
              output: schemas:OutputModel
              steps:
                - id: first
                  activity: first
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "artifacts.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: artifacts
            artifacts:
              allowed_sources: [local_path]
              allowed_media_types: [application/pdf]
              max_bytes: 1024
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          artifacts: policies/artifacts.yaml
        """,
    )

    report = validate_project_bundle(
        load_project_spec(project_path),
        environment_id="local",
        policy_ids=("artifacts",),
    )

    assert report.ok is False
    assert "resolved_policy_artifacts_failed" in {issue.code for issue in report.issues}


def test_validate_project_bundle_reports_provider_retry_and_limit_policy_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    (tmp_path / "workflow.yaml").write_text(
        _dedent(
            """
            project: demo_project
            name: workflow
            task_queue: demo-task-queue
            runtime:
              temporal:
                address: localhost:7233
              registry:
                type: inline
                prompts:
                  first: first {{value}}
              provider:
                type: fake
              provider_retry:
                max_attempts: 3
              provider_limits:
                default:
                  max_concurrent: 5
                  min_interval_seconds: 0.1
            activities:
              definitions:
                - name: first
                  input: schemas:InputModel
                  output: schemas:OutputModel
                  prompt: first
            workflow:
              name: DemoWorkflow
              input: schemas:InputModel
              output: schemas:OutputModel
              steps:
                - id: first
                  activity: first
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "runtime.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: runtime
            runtime:
              provider_retry:
                max_attempts: 1
              provider_limits:
                default:
                  max_concurrent: 2
                  min_interval_seconds: 1.0
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          runtime: policies/runtime.yaml
        """,
    )

    report = validate_project_bundle(
        load_project_spec(project_path),
        environment_id="local",
        policy_ids=("runtime",),
    )

    issue_codes = {issue.code for issue in report.issues}
    assert report.ok is False
    assert "resolved_policy_provider_retry_failed" in issue_codes
    assert "resolved_policy_provider_limits_failed" in issue_codes


def test_project_cli_validate_applies_explicit_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "openai-only.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: openai-only
            providers:
              allowed:
                openai: {}
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          openai-only: policies/openai-only.yaml
        """,
    )

    exit_code = project_cli.main(
        [
            "validate",
            str(project_path),
            "--environment",
            "local",
            "--policy",
            "openai-only",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 3  # validation/drift verdict (#818)
    assert "resolved_policy_provider_failed" in {issue["code"] for issue in payload["issues"]}


def test_project_cli_validate_policy_requires_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "openai-only.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: openai-only
            providers:
              allowed:
                openai: {}
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        policies:
          openai-only: policies/openai-only.yaml
        """,
    )

    exit_code = project_cli.main(
        [
            "validate",
            str(project_path),
            "--policy",
            "openai-only",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 3  # validation/drift verdict (#818)
    assert {issue["code"] for issue in payload["issues"]} == {
        "validation_environment_required",
    }


def test_project_cli_validate_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: missing
            path: missing.yaml
        """,
    )

    exit_code = project_cli.main(["validate", str(project_path)])

    output = capsys.readouterr().out
    assert exit_code == 3  # validation/drift verdict (#818)
    assert "missing_workflow_file" in output


def test_project_cli_lists_environments(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_env_override_workflow_yaml(tmp_path / "workflow.yaml")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        """,
    )

    exit_code = project_cli.main(["environments", str(project_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "local" in output
    assert "ok" in output

    # #814: every subcommand supports --json — the same rows, machine-readable.
    json_exit = project_cli.main(["environments", str(project_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert json_exit == 0
    assert payload["environments"][0]["id"] == "local"
    assert payload["environments"][0]["status"] == "ok"

    list_exit = project_cli.main(["list", str(project_path), "--json"])
    list_payload = json.loads(capsys.readouterr().out)
    assert list_exit == 0
    assert list_payload["ok"] is True
    assert list_payload["workflows"][0]["id"] == "workflow"
    assert list_payload["issue_count"] == 0


def test_project_cli_resolve_json_uses_environment_profile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_env_override_workflow_yaml(tmp_path / "workflow.yaml")
    (tmp_path / "env.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: local
            variables:
              TEMPORAL_ADDRESS: profile:7233
            overrides:
              runtime:
                temporal:
                  address: ${TEMPORAL_ADDRESS}
            workflows:
              workflow:
                overrides:
                  task_queue: profile-queue
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        """,
    )

    exit_code = project_cli.main(
        [
            "resolve",
            str(project_path),
            "--workflow",
            "workflow",
            "--environment",
            "local",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["task_queue"] == "profile-queue"
    assert payload["temporal"]["address"] == "profile:7233"


def test_project_cli_run_preflight_resolves_prompts_and_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_env_override_workflow_yaml(tmp_path / "workflow.yaml")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "fake-only.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: fake-only
            providers:
              allowed:
                fake: {}
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          fake-only: policies/fake-only.yaml
        """,
    )
    calls: dict[str, Any] = {}
    real_build_policy_guard = project_cli.build_project_policy_runtime_guard

    def recording_build_policy_guard(**kwargs):
        calls["enforcement_mode"] = kwargs["enforcement_mode"]
        return real_build_policy_guard(**kwargs)

    monkeypatch.setattr(
        project_cli,
        "build_project_policy_runtime_guard",
        recording_build_policy_guard,
    )

    exit_code = project_cli.main(
        [
            "run",
            str(project_path),
            "--workflow",
            "workflow",
            "--environment",
            "local",
            "--preflight",
            "--policy",
            "fake-only",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert [item["activity_name"] for item in payload["resolved"]] == ["first"]
    assert calls["enforcement_mode"] == "project_run"


def test_project_cli_run_expected_policy_hash_cli_allows_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, expected_hash = _write_project_run_policy_fixture(tmp_path)
    assert expected_hash is not None

    exit_code = project_cli.main(
        [
            "run",
            str(project_path),
            "--workflow",
            "workflow",
            "--environment",
            "local",
            "--preflight",
            "--policy",
            "fake-only",
            "--expect-policy-hash",
            expected_hash,
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True


def test_project_cli_run_expected_policy_hash_env_allows_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, expected_hash = _write_project_run_policy_fixture(tmp_path)
    assert expected_hash is not None
    monkeypatch.setenv("TYPEFLUX_EXPECTED_POLICY_HASH", expected_hash)

    exit_code = project_cli.main(
        [
            "run",
            str(project_path),
            "--workflow",
            "workflow",
            "--environment",
            "local",
            "--preflight",
            "--policy",
            "fake-only",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True


@pytest.mark.asyncio
async def test_workflow_operations_policy_hash_mismatch_fails_before_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typeflux.project.operations import WorkflowOperations
    from typeflux.project.policy_enforcement import ProjectPolicyEnforcementError

    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, _ = _write_project_run_policy_fixture(tmp_path)
    project = load_project_spec(project_path)

    def fail_build(*args, **kwargs):
        raise AssertionError("build_runtime must not run after policy hash mismatch")

    monkeypatch.setattr("typeflux.project.binding.build_runtime", fail_build)

    with pytest.raises(ProjectPolicyEnforcementError, match="does not match expected"):
        await WorkflowOperations.for_project_workflow(
            project,
            workflow_id="workflow",
            environment_id="local",
            policy_ids=("fake-only",),
            expected_policy_hash="0" * 64,
        )


@pytest.mark.asyncio
async def test_workflow_operations_for_project_workflow_builds_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typeflux.project.operations import WorkflowOperations

    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, expected_hash = _write_project_run_policy_fixture(tmp_path)
    assert expected_hash is not None
    project = load_project_spec(project_path)
    captured: dict[str, Any] = {}

    def fake_prepare_runtime_build(
        spec,
        *,
        policy_guard=None,
        subworkflow_records=None,
        child_workflow_classes=(),
        child_activities=None,
        child_registry_specs=(),
    ):
        captured["prepare_policy_guard"] = policy_guard
        # #55 slice 3: the driver prelude resolves sub-workflows and passes them
        # through; this V1 fixture has none, so the resolution is EMPTY.
        captured["subworkflow_records"] = subworkflow_records
        captured["child_workflow_classes"] = child_workflow_classes
        captured["child_registry_specs"] = child_registry_specs
        return "prepared-sentinel"

    async def fake_build_runtime(spec, *, policy_guard=None, prepared=None):
        captured["spec"] = spec
        captured["policy_guard"] = policy_guard
        captured["prepared"] = prepared
        return "runtime-sentinel"

    monkeypatch.setattr(
        "typeflux.project.binding.prepare_runtime_build",
        fake_prepare_runtime_build,
    )
    monkeypatch.setattr(
        "typeflux.project.binding.build_runtime",
        fake_build_runtime,
    )

    ops = await WorkflowOperations.for_project_workflow(
        project,
        workflow_id="workflow",
        environment_id="local",
        policy_ids=("fake-only",),
        expected_policy_hash=expected_hash,
    )

    assert ops.runtime == "runtime-sentinel"
    assert ops.resolved is not None
    assert ops.resolved.workflow_id == "workflow"
    assert captured["prepared"] == "prepared-sentinel"
    assert captured["policy_guard"] is not None
    assert captured["policy_guard"].policy.policy_hash == expected_hash
    assert captured["prepare_policy_guard"] is captured["policy_guard"]
    assert captured["subworkflow_records"] == {}
    assert captured["child_workflow_classes"] == ()
    assert captured["child_registry_specs"] == ()


def test_project_cli_run_expected_policy_hash_mismatch_fails_before_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, _ = _write_project_run_policy_fixture(tmp_path)

    def fail_preflight(**kwargs):
        raise AssertionError("preflight should not run after policy hash mismatch")

    monkeypatch.setattr(project_cli, "preflight_ai_activities", fail_preflight)

    exit_code = project_cli.main(
        [
            "run",
            str(project_path),
            "--workflow",
            "workflow",
            "--environment",
            "local",
            "--preflight",
            "--policy",
            "fake-only",
            "--expect-policy-hash",
            "0" * 64,
        ]
    )

    output = capsys.readouterr()
    assert exit_code == 3  # validation/drift verdict (#818)
    assert output.out == ""
    assert "does not match expected deployment policy hash" in output.err


def test_project_cli_run_expected_policy_hash_conflict_fails_before_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, _ = _write_project_run_policy_fixture(tmp_path, include_policy=False)
    monkeypatch.setenv("TYPEFLUX_EXPECTED_POLICY_HASH", "1" * 64)

    def fail_preflight(**kwargs):
        raise AssertionError("preflight should not run after policy hash source conflict")

    monkeypatch.setattr(project_cli, "preflight_ai_activities", fail_preflight)

    exit_code = project_cli.main(
        [
            "run",
            str(project_path),
            "--workflow",
            "workflow",
            "--environment",
            "local",
            "--preflight",
            "--expect-policy-hash",
            "2" * 64,
        ]
    )

    output = capsys.readouterr()
    assert exit_code == 2  # usage error: bad --expect-policy-hash input (#818)
    assert output.out == ""
    assert "conflicting expected project policy hashes" in output.err


def test_project_cli_run_expected_policy_hash_requires_selected_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, _ = _write_project_run_policy_fixture(tmp_path, include_policy=False)

    def fail_preflight(**kwargs):
        raise AssertionError("preflight should not run without selected policy")

    monkeypatch.setattr(project_cli, "preflight_ai_activities", fail_preflight)

    exit_code = project_cli.main(
        [
            "run",
            str(project_path),
            "--workflow",
            "workflow",
            "--environment",
            "local",
            "--preflight",
            "--expect-policy-hash",
            "0" * 64,
        ]
    )

    output = capsys.readouterr()
    assert exit_code == 3  # validation/drift verdict (#818)
    assert output.out == ""
    assert "no project policy was selected" in output.err


def test_project_cli_expected_policy_hash_rejects_invalid_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TYPEFLUX_EXPECTED_POLICY_HASH", raising=False)
    # Malformed CLI value is rejected fail-closed before any policy resolution —
    # as a USAGE error (exit 2), distinct from a hash-drift verdict (#818).
    with pytest.raises(project_cli._PolicyHashUsageError, match="64-character sha256 hex digest"):
        project_cli._expected_policy_hash("not-a-valid-sha256")
    # Malformed env value is rejected the same way.
    monkeypatch.setenv("TYPEFLUX_EXPECTED_POLICY_HASH", "abc123")
    with pytest.raises(project_cli._PolicyHashUsageError, match="64-character sha256 hex digest"):
        project_cli._expected_policy_hash(None)
    # A valid digest is normalized (trimmed + lower-cased) for comparison.
    monkeypatch.delenv("TYPEFLUX_EXPECTED_POLICY_HASH", raising=False)
    assert project_cli._expected_policy_hash("  " + "A" * 64 + "  ") == "a" * 64


def _submit_args(project_path: Path, input_path: Path, *extra: str) -> list[str]:
    return [
        "submit",
        str(project_path),
        "--workflow",
        "workflow",
        "--environment",
        "local",
        "--policy",
        "fake-only",
        "--input",
        str(input_path),
        "--workflow-id",
        "wf-submit-hash",
        *extra,
    ]


def _migrate_args(project_path: Path, *extra: str) -> list[str]:
    return [
        "migrate",
        str(project_path),
        "--workflow",
        "workflow",
        "--environment",
        "local",
        "--execution-id",
        "case-1",
        "--run-id",
        "run-1",
        *extra,
    ]


class _FakeMigrateOps:
    """A WorkflowOperations stand-in for the migrate CLI (#204)."""

    result: object | None = None
    exc: Exception | None = None

    @classmethod
    async def for_project_workflow(
        cls, project, *, workflow_id, environment_id, policy_ids=(), expected_policy_hash=None
    ):
        return cls()

    async def migrate(
        self, execution_id, *, run_id=None, abandon_gates=False, reason=None, dry_run=False
    ):
        if type(self).exc is not None:
            raise type(self).exc
        return type(self).result

    def shutdown(self) -> None:
        pass


def test_project_cli_migrate_prints_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from typeflux.project.migrate import WorkflowMigrateResult

    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, _ = _write_project_run_policy_fixture(tmp_path, include_policy=False)
    _FakeMigrateOps.exc = None
    _FakeMigrateOps.result = WorkflowMigrateResult(
        execution_id="case-1",
        old_run_id="run-1",
        new_run_id="new-run",
        old_version_key="W.oldkey000000",
        new_version_key="W.newkey000000",
        abandoned_gate_ids=(),
    )
    monkeypatch.setattr("typeflux.project.operations.WorkflowOperations", _FakeMigrateOps)

    exit_code = project_cli.main(_migrate_args(project_path, "--reason", "rollover"))

    output = capsys.readouterr()
    assert exit_code == 0
    assert '"new_run_id": "new-run"' in output.out
    assert '"new_version_key": "W.newkey000000"' in output.out


def test_project_cli_migrate_refusal_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from typeflux.project.migrate import SameVersionMigrateError

    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, _ = _write_project_run_policy_fixture(tmp_path, include_policy=False)
    _FakeMigrateOps.result = None
    _FakeMigrateOps.exc = SameVersionMigrateError(
        "migrate refused: execution 'case-1' already runs the current version 'W.newkey000000'"
    )
    monkeypatch.setattr("typeflux.project.operations.WorkflowOperations", _FakeMigrateOps)

    exit_code = project_cli.main(_migrate_args(project_path))

    output = capsys.readouterr()
    assert exit_code == 1
    assert "already runs the current version" in output.err


def test_project_cli_submit_threads_subject_override_like_the_yaml_edition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#805: `project submit --subject` has the yaml edition's exact semantics — an
    explicit override wins over the spec `subjects:` extraction (yaml/submit.py passes
    `subject_ids=tuple(...) or None` to the shared runtime; the project CLI now does the
    same, so both editions hit the identical `execute_workflow(subject_ids=...)` seam)."""
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, _ = _write_project_run_policy_fixture(tmp_path, include_policy=False)
    input_path = tmp_path / "input.json"
    input_path.write_text('{"value": "hello"}', encoding="utf-8")

    captured: dict[str, Any] = {}

    async def _fake_build(spec, policy_guard, *, project=None, resolved=None):
        class _FakeObs:
            class writer:
                @staticmethod
                def shutdown() -> None:
                    pass

        class _FakeRuntime:
            observability = _FakeObs()

            async def execute_workflow(self, input_value, **kwargs):
                captured.update(kwargs)
                return {"result": "ok"}

        return _FakeRuntime()

    monkeypatch.setattr("typeflux.project.__main__._build_runtime_with_policy_guard", _fake_build)

    exit_code = project_cli.main(
        [
            "submit",
            str(project_path),
            "--workflow",
            "workflow",
            "--environment",
            "local",
            "--input",
            str(input_path),
            "--workflow-id",
            "wf-subject",
            "--subject",
            "subject-0001",
            "--subject",
            "subject-0002",
        ]
    )

    output = capsys.readouterr()
    assert exit_code == 0, output.err
    assert captured["subject_ids"] == ("subject-0001", "subject-0002")

    # Without the flag: None reaches the seam — the spec `subjects:` extraction applies,
    # exactly the yaml edition's fall-through.
    captured.clear()
    exit_code = project_cli.main(
        [
            "submit",
            str(project_path),
            "--workflow",
            "workflow",
            "--environment",
            "local",
            "--input",
            str(input_path),
            "--workflow-id",
            "wf-subject-2",
        ]
    )
    assert exit_code == 0
    assert captured["subject_ids"] is None


class _FakeLifecycleOps:
    """A WorkflowOperations stand-in for the status/review/cancel CLI (#802)."""

    status_result: object | None = None
    review_calls: list = []
    cancel_calls: list = []
    exc: Exception | None = None

    @classmethod
    async def for_project_workflow(
        cls, project, *, workflow_id, environment_id, policy_ids=(), expected_policy_hash=None
    ):
        return cls()

    async def status(self, execution_id, *, run_id=None, trace=False):
        if type(self).exc is not None:
            raise type(self).exc
        return type(self).status_result

    async def submit_review(self, execution_id, command, *, run_id=None):
        if type(self).exc is not None:
            raise type(self).exc
        type(self).review_calls.append((execution_id, command, run_id))

    async def request_cancel(self, execution_id, reason=None, *, run_id=None):
        if type(self).exc is not None:
            raise type(self).exc
        type(self).cancel_calls.append((execution_id, reason, run_id))

    def shutdown(self) -> None:
        pass


def _lifecycle_args(command: str, project_path: Path, *extra: str) -> list[str]:
    return [
        command,
        str(project_path),
        "--workflow",
        "workflow",
        "--environment",
        "local",
        "--execution-id",
        "case-1",
        *extra,
    ]


def test_project_cli_status_prints_the_operation_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#802: a CLI-only operator gets the CP status route's exact shape."""
    from typeflux.project.operations import WorkflowOperationStatus

    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, _ = _write_project_run_policy_fixture(tmp_path, include_policy=False)
    _FakeLifecycleOps.exc = None
    from typeflux.core.contracts import WorkflowLifecycleStatus

    _FakeLifecycleOps.status_result = WorkflowOperationStatus(
        workflow_id="case-1",
        run_id="run-1",
        status=WorkflowLifecycleStatus(state="running", current_step="review"),
        valid_user_decisions={"approve": "next", "reject": "revise"},
    )
    monkeypatch.setattr("typeflux.project.operations.WorkflowOperations", _FakeLifecycleOps)

    exit_code = project_cli.main(_lifecycle_args("status", project_path, "--trace"))

    output = capsys.readouterr()
    assert exit_code == 0
    assert '"state": "running"' in output.out
    assert '"valid_user_decisions"' in output.out


def test_project_cli_review_submits_the_decision_with_gate_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, _ = _write_project_run_policy_fixture(tmp_path, include_policy=False)
    _FakeLifecycleOps.exc = None
    _FakeLifecycleOps.review_calls = []
    monkeypatch.setattr("typeflux.project.operations.WorkflowOperations", _FakeLifecycleOps)

    exit_code = project_cli.main(
        _lifecycle_args(
            "review",
            project_path,
            "--decision",
            "approve",
            "--gate",
            "legal",
            "--reviewer",
            "ops@example",
            "--notes",
            "checked",
        )
    )

    output = capsys.readouterr()
    assert exit_code == 0
    assert '"review": "submitted"' in output.out
    ((execution_id, command, run_id),) = _FakeLifecycleOps.review_calls
    assert execution_id == "case-1"
    assert command.user_decision == "approve"
    assert command.gate == "legal"
    assert command.reviewer == "ops@example"
    assert run_id is None


def test_project_cli_cancel_requests_cancellation_and_refusals_exit_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, _ = _write_project_run_policy_fixture(tmp_path, include_policy=False)
    _FakeLifecycleOps.exc = None
    _FakeLifecycleOps.cancel_calls = []
    monkeypatch.setattr("typeflux.project.operations.WorkflowOperations", _FakeLifecycleOps)

    exit_code = project_cli.main(
        _lifecycle_args("cancel", project_path, "--reason", "duplicate run")
    )
    output = capsys.readouterr()
    assert exit_code == 0
    assert '"cancel": "requested"' in output.out
    assert _FakeLifecycleOps.cancel_calls == [("case-1", "duplicate run", None)]

    _FakeLifecycleOps.exc = RuntimeError("no pollers on the target queue")
    exit_code = project_cli.main(_lifecycle_args("cancel", project_path))
    output = capsys.readouterr()
    assert exit_code == 1
    assert "no pollers" in output.err


def test_project_cli_submit_expected_policy_hash_mismatch_fails_before_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("TYPEFLUX_EXPECTED_POLICY_HASH", raising=False)
    project_path, _ = _write_project_run_policy_fixture(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text('{"value": "start"}', encoding="utf-8")

    exit_code = project_cli.main(
        _submit_args(project_path, input_path, "--expect-policy-hash", "0" * 64)
    )

    output = capsys.readouterr()
    assert exit_code == 3  # validation/drift verdict (#818)
    assert "does not match expected deployment policy hash" in output.err


def test_project_cli_submit_expected_policy_hash_env_is_verified_before_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, expected_hash = _write_project_run_policy_fixture(tmp_path)
    assert expected_hash is not None
    monkeypatch.setenv("TYPEFLUX_EXPECTED_POLICY_HASH", expected_hash)
    input_path = tmp_path / "input.json"
    input_path.write_text('{"value": "start"}', encoding="utf-8")

    async def reached_runtime_build(spec, policy_guard, **kwargs):
        raise RuntimeError("reached-runtime-build")

    monkeypatch.setattr(project_cli, "_build_runtime_with_policy_guard", reached_runtime_build)

    exit_code = project_cli.main(_submit_args(project_path, input_path))

    output = capsys.readouterr()
    # The matching env hash passes verification; the sentinel proves submit
    # proceeded past the policy hash gate rather than skipping it.
    assert exit_code == 1
    assert "reached-runtime-build" in output.err


def test_project_cli_submit_expected_policy_hash_requires_selected_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("TYPEFLUX_EXPECTED_POLICY_HASH", raising=False)
    project_path, _ = _write_project_run_policy_fixture(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text('{"value": "start"}', encoding="utf-8")

    exit_code = project_cli.main(
        [
            "submit",
            str(project_path),
            "--workflow",
            "workflow",
            "--environment",
            "local",
            "--input",
            str(input_path),
            "--workflow-id",
            "wf-submit-hash",
            "--expect-policy-hash",
            "0" * 64,
        ]
    )

    output = capsys.readouterr()
    assert exit_code == 3  # validation/drift verdict (#818)
    assert "no project policy was selected" in output.err


def _validate_bundle_with_policy(
    tmp_path: Path,
    *,
    policy_yaml: str,
    env_variables: str = "",
    **workflow_kwargs: Any,
):
    if not (tmp_path / "demo_project").exists():
        _write_demo_project_package(tmp_path)
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow", **workflow_kwargs)
    (tmp_path / "env.yaml").write_text(
        'version: "1"\nname: local\n' + env_variables,
        encoding="utf-8",
    )
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir(exist_ok=True)
    (policy_dir / "policy.yaml").write_text(_dedent(policy_yaml), encoding="utf-8")
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          policy: policies/policy.yaml
        """,
    )
    return validate_project_bundle(
        load_project_spec(project_path),
        environment_id="local",
        policy_ids=("policy",),
    )


_BASE_URL_POLICY = """
version: "1"
name: policy
providers:
  allowed:
    fake:
      base_urls: ["https://llm-gateway.internal"]
"""


def test_policy_rejects_disallowed_provider_base_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    report = _validate_bundle_with_policy(
        tmp_path,
        policy_yaml=_BASE_URL_POLICY,
        provider_base_url="https://exfiltrate.example.com",
    )

    assert report.ok is False
    issues = {issue.code: issue.message for issue in report.issues}
    assert "resolved_policy_provider_failed" in issues
    assert "base_url" in issues["resolved_policy_provider_failed"]


def test_policy_allows_listed_or_default_provider_base_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    listed = _validate_bundle_with_policy(
        tmp_path,
        policy_yaml=_BASE_URL_POLICY,
        provider_base_url="https://llm-gateway.internal",
    )
    assert listed.ok is True

    default_endpoint = _validate_bundle_with_policy(
        tmp_path,
        policy_yaml=_BASE_URL_POLICY,
    )
    assert default_endpoint.ok is True


_REGISTRY_HOST_POLICY = """
version: "1"
name: policy
runtime:
  registry:
    allowed_hosts: ["https://langfuse.us.internal"]
"""


def test_policy_rejects_disallowed_registry_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    report = _validate_bundle_with_policy(
        tmp_path,
        policy_yaml=_REGISTRY_HOST_POLICY,
        registry_type="langfuse",
        registry_host="https://langfuse.shadow.example.com",
    )

    assert report.ok is False
    issues = {issue.code for issue in report.issues}
    assert "resolved_policy_registry_failed" in issues


def test_policy_registry_hosts_check_uses_env_fallback_and_skips_inline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)

    # The effective host comes from LANGFUSE_HOST when the spec has no host.
    env_allowed = _validate_bundle_with_policy(
        tmp_path,
        policy_yaml=_REGISTRY_HOST_POLICY,
        env_variables="variables:\n  LANGFUSE_HOST: https://langfuse.us.internal\n",
        registry_type="langfuse",
    )
    assert env_allowed.ok is True

    env_disallowed = _validate_bundle_with_policy(
        tmp_path,
        policy_yaml=_REGISTRY_HOST_POLICY,
        env_variables="variables:\n  LANGFUSE_HOST: https://langfuse.eu.internal\n",
        registry_type="langfuse",
    )
    assert env_disallowed.ok is False

    inline = _validate_bundle_with_policy(
        tmp_path,
        policy_yaml=_REGISTRY_HOST_POLICY,
    )
    assert inline.ok is True


_ADDRESS_REGIONS_POLICY = """
version: "1"
name: policy
runtime:
  temporal:
    allowed_regions: [us-east]
    address_regions:
      localhost:7233: {region}
"""


class _FakeDrainClient:
    def __init__(self, workflow_types: list[str]) -> None:
        self._workflow_types = workflow_types
        self.queries: list[str] = []

    def list_workflows(self, query: str):
        self.queries.append(query)

        async def _generate():
            for workflow_type in self._workflow_types:
                yield SimpleNamespace(workflow_type=workflow_type)

        return _generate()


def _patch_drain_client(
    monkeypatch: pytest.MonkeyPatch,
    client: _FakeDrainClient,
) -> None:
    async def fake_connect(spec, *, plugin):
        return client

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)


@pytest.mark.asyncio
async def test_workflow_drain_status_reports_running_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, _ = _write_project_run_policy_fixture(tmp_path)
    project = load_project_spec(project_path)
    client = _FakeDrainClient(["DemoWorkflow.v6", "DemoWorkflow.v6"])
    _patch_drain_client(monkeypatch, client)

    status = await workflow_drain_status(
        project,
        workflow_id="workflow",
        environment_id="local",
    )

    assert status.logical_workflow == "DemoWorkflow"
    assert status.current_workflow_type.startswith("DemoWorkflow.")
    assert status.running == {"DemoWorkflow.v6": 2}
    assert status.total_running == 2
    assert status.drained is False
    # Without a configured search attribute, the prefix query is used.
    assert client.queries == [
        "WorkflowType STARTS_WITH 'DemoWorkflow.' AND ExecutionStatus = 'Running'"
    ]


@pytest.mark.asyncio
async def test_workflow_drain_status_drained_when_only_current_version_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, _ = _write_project_run_policy_fixture(tmp_path)
    project = load_project_spec(project_path)

    empty_client = _FakeDrainClient([])
    _patch_drain_client(monkeypatch, empty_client)
    empty = await workflow_drain_status(project, workflow_id="workflow", environment_id="local")
    assert empty.drained is True
    assert empty.total_running == 0

    current_client = _FakeDrainClient([empty.current_workflow_type])
    _patch_drain_client(monkeypatch, current_client)
    current_only = await workflow_drain_status(
        project, workflow_id="workflow", environment_id="local"
    )
    assert current_only.drained is True
    assert current_only.running == {empty.current_workflow_type: 1}


@pytest.mark.asyncio
async def test_drain_status_ignores_search_attribute_for_gating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Executions started before the attribute was enabled (or via raw
    # client.start_workflow) carry no search attribute; drain gating must not
    # become a false positive because of them, so the type-prefix query is
    # used even when the attribute is configured.
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    (tmp_path / "workflow.yaml").write_text(
        _dedent(
            """
            project: demo_project
            name: workflow
            task_queue: demo-task-queue
            runtime:
              temporal:
                address: localhost:7233
                workflow_search_attribute: TypefluxWorkflow
              registry:
                type: inline
                prompts:
                  first: first {{value}}
              provider:
                type: fake
            activities:
              definitions:
                - name: first
                  input: schemas:InputModel
                  output: schemas:OutputModel
                  prompt: first
            workflow:
              name: DemoWorkflow
              input: schemas:InputModel
              output: schemas:OutputModel
              steps:
                - id: first
                  activity: first
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        """,
    )
    project = load_project_spec(project_path)
    client = _FakeDrainClient(["DemoWorkflow.v6"])
    _patch_drain_client(monkeypatch, client)

    status = await workflow_drain_status(project, workflow_id="workflow", environment_id="local")

    assert status.drained is False
    assert client.queries == [
        "WorkflowType STARTS_WITH 'DemoWorkflow.' AND ExecutionStatus = 'Running'"
    ]


def test_project_cli_drain_status_exit_code_tracks_drained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, _ = _write_project_run_policy_fixture(tmp_path)
    client = _FakeDrainClient(["DemoWorkflow.v6"])
    _patch_drain_client(monkeypatch, client)

    exit_code = project_cli.main(
        [
            "drain-status",
            str(project_path),
            "--workflow",
            "workflow",
            "--environment",
            "local",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["drained"] is False
    assert payload["running"] == {"DemoWorkflow.v6": 1}

    _patch_drain_client(monkeypatch, _FakeDrainClient([]))
    exit_code = project_cli.main(
        [
            "drain-status",
            str(project_path),
            "--workflow",
            "workflow",
            "--environment",
            "local",
        ]
    )
    assert exit_code == 0


def test_project_spec_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    project_path = tmp_path / "typeflux.project.yaml"
    project_path.write_text(
        _dedent(
            """
            version: "1"
            name: demo-project
            name: shadowed-project
            workflows:
              - id: workflow
                path: workflow.yaml
            environments:
              local: env.yaml
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="duplicate key 'name'"):
        load_project_spec(project_path)


def test_environment_profile_and_policy_reject_duplicate_yaml_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    (tmp_path / "env.yaml").write_text(
        'version: "1"\nname: local\nname: shadowed\n',
        encoding="utf-8",
    )
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: policy
            providers:
              allowed:
                fake: {}
            providers:
              allowed:
                openai: {}
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          policy: policies/policy.yaml
        """,
    )
    project = load_project_spec(project_path)

    with pytest.raises(Exception, match="duplicate key 'name'"):
        load_project_environment(project, "local")
    with pytest.raises(Exception, match="duplicate key 'providers'"):
        load_project_policy(project, "policy")
    # Bundle validation reports the same failures as issues instead of raising.
    report = validate_project_bundle(project, environment_id="local")
    assert report.ok is False
    assert any("duplicate key" in issue.message for issue in report.issues)


_REQUIRE_SECRET_REFERENCES_POLICY = """
version: "1"
name: policy
secrets:
  require_secret_references: true
"""

_SECRET_WORKFLOW_YAML = """
project: demo_project
name: workflow
task_queue: demo-task-queue
runtime:
  temporal:
    address: localhost:7233
    tls: true
    api_key: {temporal_api_key}
  registry:
    type: inline
    prompts:
      first: first {{{{value}}}}
  provider:
    type: fake
    api_key: {provider_api_key}
activities:
  definitions:
    - name: first
      input: schemas:InputModel
      output: schemas:OutputModel
      prompt: first
workflow:
  name: DemoWorkflow
  input: schemas:InputModel
  output: schemas:OutputModel
  steps:
    - id: first
      activity: first
"""


def _validate_secret_policy_bundle(
    tmp_path: Path,
    *,
    temporal_api_key: str,
    provider_api_key: str,
):
    if not (tmp_path / "demo_project").exists():
        _write_demo_project_package(tmp_path)
    (tmp_path / "workflow.yaml").write_text(
        _dedent(
            _SECRET_WORKFLOW_YAML.format(
                temporal_api_key=temporal_api_key,
                provider_api_key=provider_api_key,
            )
        ),
        encoding="utf-8",
    )
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir(exist_ok=True)
    (policy_dir / "policy.yaml").write_text(
        _dedent(_REQUIRE_SECRET_REFERENCES_POLICY), encoding="utf-8"
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          policy: policies/policy.yaml
        """,
    )
    return validate_project_bundle(
        load_project_spec(project_path),
        environment_id="local",
        policy_ids=("policy",),
    )


def test_policy_require_secret_references_rejects_literal_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    report = _validate_secret_policy_bundle(
        tmp_path,
        temporal_api_key="literal-temporal-key",
        provider_api_key="sk-literal-provider-key",
    )

    assert report.ok is False
    checks = {
        check.code: check for workflow in report.resolved_workflows for check in workflow.checks
    }
    assert checks["policy_secrets"].status == "failed"
    message = checks["policy_secrets"].message or ""
    assert "runtime.temporal.api_key" in message
    assert "runtime.provider.api_key" in message
    assert "sk-literal-provider-key" not in message


def test_policy_require_secret_references_accepts_typed_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    reference = """
      value_from:
        env: DEMO_SECRET
        required: false"""
    report = _validate_secret_policy_bundle(
        tmp_path,
        temporal_api_key=reference,
        provider_api_key=reference,
    )

    checks = {
        check.code: check for workflow in report.resolved_workflows for check in workflow.checks
    }
    assert checks["policy_secrets"].status == "passed"


_SECRET_CUSTOM_CONFIG_WORKFLOW_YAML = """
project: demo_project
name: workflow
task_queue: demo-task-queue
runtime:
  temporal:
    address: localhost:7233
    tls: true
    api_key:
      value_from:
        env: DEMO_SECRET
        required: false
  registry:
    type: inline
    prompts:
      first: first {{{{value}}}}
  provider:
    type: custom
    class: demo_project.providers:CustomProvider
    config:
      api_key: {config_api_key}
  imports:
    allow_provider_class: true
activities:
  definitions:
    - name: first
      input: schemas:InputModel
      output: schemas:OutputModel
      prompt: first
workflow:
  name: DemoWorkflow
  input: schemas:InputModel
  output: schemas:OutputModel
  steps:
    - id: first
      activity: first
"""


def _validate_custom_config_policy_bundle(tmp_path: Path, *, config_api_key: str):
    if not (tmp_path / "demo_project").exists():
        _write_demo_project_package(tmp_path)
    (tmp_path / "workflow.yaml").write_text(
        _dedent(_SECRET_CUSTOM_CONFIG_WORKFLOW_YAML.format(config_api_key=config_api_key)),
        encoding="utf-8",
    )
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir(exist_ok=True)
    (policy_dir / "policy.yaml").write_text(
        _dedent(_REQUIRE_SECRET_REFERENCES_POLICY), encoding="utf-8"
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          policy: policies/policy.yaml
        """,
    )
    return validate_project_bundle(
        load_project_spec(project_path),
        environment_id="local",
        policy_ids=("policy",),
    )


def test_policy_require_secret_references_rejects_literal_custom_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#792: custom-extension config entries are first-class secret slots — the policy
    gate is driven by the same inventory walk that reports them, so a literal there is
    exactly as forbidden as a literal api_key."""
    monkeypatch.syspath_prepend(str(tmp_path))
    report = _validate_custom_config_policy_bundle(tmp_path, config_api_key="sk-live-hardcoded")

    checks = {
        check.code: check for workflow in report.resolved_workflows for check in workflow.checks
    }
    assert checks["policy_secrets"].status == "failed"
    message = checks["policy_secrets"].message or ""
    assert "runtime.provider.config[api_key]" in message
    assert "sk-live-hardcoded" not in message


def test_policy_require_secret_references_accepts_custom_config_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    report = _validate_custom_config_policy_bundle(
        tmp_path,
        config_api_key="""
        value_from:
          env: DEMO_SECRET
          required: false""",
    )

    checks = {
        check.code: check for workflow in report.resolved_workflows for check in workflow.checks
    }
    assert checks["policy_secrets"].status == "passed"


def test_policy_require_secret_references_rejects_literal_observability_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#793 x #792: the records-driven gate covers the new observability slots — a literal
    langfuse credential fails exactly like a literal api_key."""
    monkeypatch.syspath_prepend(str(tmp_path))
    if not (tmp_path / "demo_project").exists():
        _write_demo_project_package(tmp_path)
    (tmp_path / "workflow.yaml").write_text(
        _dedent(
            """
            project: demo_project
            name: workflow
            task_queue: demo-task-queue
            runtime:
              temporal:
                address: localhost:7233
              registry:
                type: inline
                prompts:
                  first: first {{value}}
              provider:
                type: fake
              observability:
                type: langfuse
                langfuse:
                  public_key: pk-literal
                  secret_key: sk-live-hardcoded
            activities:
              definitions:
                - name: first
                  input: schemas:InputModel
                  output: schemas:OutputModel
                  prompt: first
            workflow:
              name: DemoWorkflow
              input: schemas:InputModel
              output: schemas:OutputModel
              steps:
                - id: first
                  activity: first
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir(exist_ok=True)
    (policy_dir / "policy.yaml").write_text(
        _dedent(_REQUIRE_SECRET_REFERENCES_POLICY), encoding="utf-8"
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          policy: policies/policy.yaml
        """,
    )
    report = validate_project_bundle(
        load_project_spec(project_path), environment_id="local", policy_ids=("policy",)
    )

    checks = {
        check.code: check for workflow in report.resolved_workflows for check in workflow.checks
    }
    message = checks["policy_secrets"].message or ""
    assert checks["policy_secrets"].status == "failed"
    assert "runtime.observability.langfuse.public_key" in message
    assert "runtime.observability.langfuse.secret_key" in message
    assert "sk-live-hardcoded" not in message


def test_policy_address_regions_mapping_replaces_self_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("TYPEFLUX_TEMPORAL_REGION", raising=False)

    # The mapped region satisfies allowed_regions with no env attestation.
    mapped_pass = _validate_bundle_with_policy(
        tmp_path,
        policy_yaml=_ADDRESS_REGIONS_POLICY.format(region="us-east"),
    )
    assert mapped_pass.ok is True

    # The mapped region fails allowed_regions regardless of attestation.
    mapped_fail = _validate_bundle_with_policy(
        tmp_path,
        policy_yaml=_ADDRESS_REGIONS_POLICY.format(region="eu-west"),
    )
    assert mapped_fail.ok is False
    assert any("maps to region" in issue.message for issue in mapped_fail.issues)


def test_policy_address_regions_detects_attestation_conflict_and_unmapped_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))

    conflict = _validate_bundle_with_policy(
        tmp_path,
        policy_yaml=_ADDRESS_REGIONS_POLICY.format(region="us-east"),
        env_variables="variables:\n  TYPEFLUX_TEMPORAL_REGION: eu-west\n",
    )
    assert conflict.ok is False
    assert any("conflicts with the policy mapping" in issue.message for issue in conflict.issues)

    monkeypatch.delenv("TYPEFLUX_TEMPORAL_REGION", raising=False)
    unmapped = _validate_bundle_with_policy(
        tmp_path,
        policy_yaml="""
        version: "1"
        name: policy
        runtime:
          temporal:
            allowed_regions: [us-east]
            address_regions:
              other-host:7233: us-east
        """,
    )
    assert unmapped.ok is False
    assert any("has no region mapping" in issue.message for issue in unmapped.issues)


def test_resolved_workflow_bundle_composes_identity_steps_and_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, expected_hash = _write_project_run_policy_fixture(tmp_path)
    project = load_project_spec(project_path)

    bundle = resolve_workflow_bundle(
        project,
        workflow_id="workflow",
        environment_id="local",
        policy_ids=("fake-only",),
    )

    assert bundle.bundle_version == "1"
    assert bundle.workflow.workflow_name == "DemoWorkflow"
    assert len(bundle.workflow.spec_digest) == 64
    assert bundle.workflow.workflow_type == (f"DemoWorkflow.{bundle.workflow.spec_digest[:12]}")
    assert bundle.workflow.input_schema["name"] == "InputModel"
    assert bundle.workflow.output_schema["name"] == "OutputModel"
    assert bundle.policy is not None
    assert bundle.policy.policy_hash == expected_hash
    assert bundle.validation.ok is True
    # Steps carry the effective bounded Temporal retry/timeout resolution.
    step = bundle.steps[0]
    assert step.kind == "activity"
    assert step.effective_retry.maximum_attempts == 5
    assert step.effective_start_to_close_timeout_seconds == 120.0
    activity = bundle.activities[0]
    assert activity.kind == "ai"
    assert activity.used_by_steps == (step.id,)
    assert activity.input_schema["hash"]
    assert bundle.runtime["provider"]["type"] == "fake"
    assert bundle.runtime["provider"]["api_key_configured"] is False
    assert bundle.deployment_preview is None
    assert bundle.deployment_preview_reference is not None
    assert "project deploy" in bundle.deployment_preview_reference
    assert bundle.components == ()


def test_resolved_workflow_bundle_is_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, _ = _write_project_run_policy_fixture(tmp_path)
    project = load_project_spec(project_path)

    def bundle_json() -> str:
        bundle = resolve_workflow_bundle(
            project,
            workflow_id="workflow",
            environment_id="local",
            policy_ids=("fake-only",),
        )
        return json.dumps(bundle.to_dict(), sort_keys=True)

    assert bundle_json() == bundle_json()


def test_resolved_workflow_bundle_resolves_with_failing_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "openai-only.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: openai-only
            providers:
              allowed:
                openai: {}
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          openai-only: policies/openai-only.yaml
        """,
    )
    project = load_project_spec(project_path)

    bundle = resolve_workflow_bundle(
        project,
        workflow_id="workflow",
        environment_id="local",
        policy_ids=("openai-only",),
    )

    # The fake provider violates the policy; the bundle stays inspectable.
    assert bundle.validation.ok is False
    failed = [check for check in bundle.validation.checks if check.status == "failed"]
    assert any(check.code == "policy_provider" for check in failed)
    assert bundle.workflow.spec_digest

    # A failing deployment preview must not make the bundle unresolvable:
    # the plan generation error is captured in the preview payload instead.
    with_preview = resolve_workflow_bundle(
        project,
        workflow_id="workflow",
        environment_id="local",
        policy_ids=("openai-only",),
        deployment_image="registry.example.com/worker:dev",
    )
    assert with_preview.validation.ok is False
    assert with_preview.deployment_preview is not None
    assert "error" in with_preview.deployment_preview
    assert with_preview.deployment_preview_reference is None
    assert with_preview.workflow.spec_digest


def test_resolved_workflow_bundle_is_secret_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    secret_value = "sk-test-secret-DONOTLEAK"
    (tmp_path / "workflow.yaml").write_text(
        _dedent(
            f"""
            project: demo_project
            name: workflow
            task_queue: demo-task-queue
            runtime:
              temporal:
                address: localhost:7233
                tls: true
                api_key: {secret_value}
              registry:
                type: inline
                prompts:
                  first: first {{{{value}}}}
              provider:
                type: fake
                api_key: {secret_value}
            activities:
              definitions:
                - name: first
                  input: schemas:InputModel
                  output: schemas:OutputModel
                  prompt: first
            workflow:
              name: DemoWorkflow
              input: schemas:InputModel
              output: schemas:OutputModel
              steps:
                - id: first
                  activity: first
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        """,
    )
    project = load_project_spec(project_path)

    bundle = resolve_workflow_bundle(
        project,
        workflow_id="workflow",
        environment_id="local",
    )

    serialized = json.dumps(bundle.to_dict())
    assert secret_value not in serialized
    assert bundle.runtime["provider"]["api_key_configured"] is True
    assert bundle.runtime["temporal"]["api_key_configured"] is True


def test_resolved_workflow_bundle_exposes_lifecycle_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    (tmp_path / "workflow.yaml").write_text(
        _dedent(
            """
            project: demo_project
            name: workflow
            task_queue: demo-task-queue
            runtime:
              temporal:
                address: localhost:7233
              registry:
                type: inline
                prompts:
                  first: first {{value}}
                  second: second {{value}}
              provider:
                type: fake
            activities:
              definitions:
                - name: first
                  input: schemas:InputModel
                  output: schemas:OutputModel
                  prompt: first
                - name: second
                  input: schemas:OutputModel
                  output: schemas:OutputModel
                  prompt: second
            workflow:
              name: DemoWorkflow
              input: schemas:InputModel
              output: schemas:OutputModel
              lifecycle:
                enabled: true
                review:
                  after_step: first
                  invalid_user_decision: fail
                  user_decisions:
                    approve:
                      route: second
              steps:
                - id: first
                  activity: first
                - id: second
                  activity: second
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        """,
    )
    project = load_project_spec(project_path)

    bundle = resolve_workflow_bundle(
        project,
        workflow_id="workflow",
        environment_id="local",
    )

    assert bundle.lifecycle is not None
    assert bundle.lifecycle.review is not None
    assert bundle.lifecycle.review.after_step == "first"
    assert bundle.lifecycle.review.invalid_user_decision == "fail"
    assert bundle.lifecycle.review.user_decisions == {"approve": "second"}


def test_resolved_workflow_bundle_deployment_preview_is_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, _ = _write_project_run_policy_fixture(tmp_path)
    project = load_project_spec(project_path)

    bundle = resolve_workflow_bundle(
        project,
        workflow_id="workflow",
        environment_id="local",
        policy_ids=("fake-only",),
        deployment_image="registry.example.com/worker:dev",
    )

    preview = bundle.deployment_preview
    assert preview is not None
    assert bundle.deployment_preview_reference is None
    assert preview["image"] == "registry.example.com/worker:dev"
    worker = preview["workers"][0]
    assert worker["workflow_id"] == "workflow"
    # Config values stay out of the bundle; only key names are listed.
    assert all(isinstance(key, str) for key in worker["config_map_keys"])
    assert "config_map" not in worker


def test_project_cli_bundle_prints_deterministic_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path, expected_hash = _write_project_run_policy_fixture(tmp_path)

    exit_code = project_cli.main(
        [
            "bundle",
            str(project_path),
            "--workflow",
            "workflow",
            "--environment",
            "local",
            "--policy",
            "fake-only",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["bundle_version"] == "1"
    assert payload["policy"]["policy_hash"] == expected_hash
    assert payload["validation"]["ok"] is True
    assert payload["workflow"]["workflow_type"].startswith("DemoWorkflow.")


def test_project_cli_run_applies_target_policy_before_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "openai-only.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: openai-only
            providers:
              allowed:
                openai: {}
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          openai-only: policies/openai-only.yaml
        validation:
          targets:
            all-envs:
              workflows: [workflow]
              policies: [openai-only]
        """,
    )

    def fail_preflight(**kwargs):
        raise AssertionError("preflight should not run after policy admission fails")

    monkeypatch.setattr(project_cli, "preflight_ai_activities", fail_preflight)

    exit_code = project_cli.main(
        [
            "run",
            str(project_path),
            "--workflow",
            "workflow",
            "--environment",
            "local",
            "--preflight",
        ]
    )

    output = capsys.readouterr()
    assert exit_code == 3  # validation/drift verdict (#818)
    assert output.out == ""
    assert "policy_provider" in output.err


def test_project_cli_resolve_json_blocks_invalid_project_validation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_workflow_yaml(tmp_path / "first.yaml", yaml_name="first")
    _write_workflow_yaml(tmp_path / "second.yaml", yaml_name="second")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: first
            path: first.yaml
          - id: second
            path: second.yaml
        environments:
          local: env.yaml
        """,
    )

    exit_code = project_cli.main(
        [
            "resolve",
            str(project_path),
            "--workflow",
            "first",
            "--environment",
            "local",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 3  # validation verdict (#818)
    assert payload["ok"] is False
    assert payload["issues"][0]["code"] == "duplicate_workflow_name"


def test_project_cli_submit_uses_resolved_environment_and_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_env_override_workflow_yaml(tmp_path / "workflow.yaml")
    (tmp_path / "env.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: local
            variables:
              TEMPORAL_ADDRESS: profile:7233
            overrides:
              runtime:
                temporal:
                  address: ${TEMPORAL_ADDRESS}
            workflows:
              workflow:
                overrides:
                  task_queue: profile-queue
            """
        ),
        encoding="utf-8",
    )
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "fake-only.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: fake-only
            providers:
              allowed:
                fake: {}
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          fake-only: policies/fake-only.yaml
        """,
    )
    input_path = tmp_path / "input.json"
    input_path.write_text('{"value": "hello"}', encoding="utf-8")
    calls: dict[str, Any] = {}

    class InputModel(BaseModel):
        value: str

    class FakeWriter:
        def shutdown(self) -> None:
            calls["shutdown"] = True

    class FakeRuntime:
        observability = type("Observability", (), {"writer": FakeWriter()})()

        async def execute_workflow(self, input_value, **kwargs):
            calls["input"] = input_value
            calls["kwargs"] = kwargs
            calls["env_temporal_address"] = os.environ["TEMPORAL_ADDRESS"]
            return {"submitted": True, "value": input_value.value}

    async def fake_build_runtime(spec, **kwargs):
        calls["spec"] = spec
        calls["policy_guard"] = kwargs["policy_guard"]
        calls["build_env_temporal_address"] = os.environ["TEMPORAL_ADDRESS"]
        return FakeRuntime()

    monkeypatch.setattr(project_cli, "import_type_ref", lambda project, ref: InputModel)
    monkeypatch.setattr(project_cli, "build_runtime", fake_build_runtime)

    exit_code = project_cli.main(
        [
            "submit",
            str(project_path),
            "--workflow",
            "workflow",
            "--environment",
            "local",
            "--input",
            str(input_path),
            "--workflow-id",
            "project-submit-test",
            "--task-queue",
            "cli-queue",
            "--policy",
            "fake-only",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload == {"submitted": True, "value": "hello"}
    assert calls["spec"].task_queue == "profile-queue"
    assert calls["policy_guard"].enforcement_mode == "project_submit"
    assert calls["build_env_temporal_address"] == "profile:7233"
    assert calls["env_temporal_address"] == "profile:7233"
    assert calls["kwargs"]["id"] == "project-submit-test"
    assert calls["kwargs"]["task_queue"] == "cli-queue"
    assert calls["shutdown"] is True


def test_project_cli_submit_applies_explicit_policy_before_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "openai-only.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: openai-only
            providers:
              allowed:
                openai: {}
            """
        ),
        encoding="utf-8",
    )
    input_path = tmp_path / "input.json"
    input_path.write_text('{"value": "hello"}', encoding="utf-8")
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          openai-only: policies/openai-only.yaml
        """,
    )

    class InputModel(BaseModel):
        value: str

    async def fail_build_runtime(spec):
        raise AssertionError("runtime should not be built after policy admission fails")

    monkeypatch.setattr(project_cli, "import_type_ref", lambda project, ref: InputModel)
    monkeypatch.setattr(project_cli, "build_runtime", fail_build_runtime)

    exit_code = project_cli.main(
        [
            "submit",
            str(project_path),
            "--workflow",
            "workflow",
            "--environment",
            "local",
            "--input",
            str(input_path),
            "--workflow-id",
            "project-submit-test",
            "--policy",
            "openai-only",
        ]
    )

    output = capsys.readouterr()
    assert exit_code == 3  # validation/drift verdict (#818)
    assert output.out == ""
    assert "policy_provider" in output.err


def test_project_cli_submit_blocks_invalid_project_before_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_workflow_yaml(tmp_path / "first.yaml", yaml_name="first")
    _write_workflow_yaml(tmp_path / "second.yaml", yaml_name="second")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    input_path = tmp_path / "input.json"
    input_path.write_text('{"value": "hello"}', encoding="utf-8")
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: first
            path: first.yaml
          - id: second
            path: second.yaml
        environments:
          local: env.yaml
        """,
    )

    async def fail_build_runtime(spec):
        raise AssertionError("runtime should not be built for invalid projects")

    monkeypatch.setattr(project_cli, "build_runtime", fail_build_runtime)

    exit_code = project_cli.main(
        [
            "submit",
            str(project_path),
            "--workflow",
            "first",
            "--environment",
            "local",
            "--input",
            str(input_path),
            "--workflow-id",
            "project-submit-test",
        ]
    )

    output = capsys.readouterr()
    assert exit_code == 3  # validation verdict (#818)
    assert "duplicate_workflow_name" in output.err
    assert output.out == ""


def test_project_cli_validate_invalid_manifest_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: first
            path: first.yaml
            directory: workflows/first
        """,
    )

    exit_code = project_cli.main(["validate", str(project_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 3  # validation verdict (#818)
    assert payload["ok"] is False
    assert payload["issues"][0]["code"] == "invalid_project_manifest"


def test_checked_in_examples_project_manifest_validates() -> None:
    project = load_project_spec("examples/typeflux.project.yaml")

    report = validate_project(project)

    assert report.ok is True
    assert {workflow.id for workflow in report.workflows} == {
        "tutorial_quickstart",
        "contract_risk_review",
        "contract_risk_review_anthropic",
        "financial_claims_marketing_review",
        "insurance_claim_review",
        "lifecycle_review",
        "regulated_claims_adjudication",
        "regulated_disclosure_review",
        "support_triage_langfuse",
        "support_triage_langfuse_anthropic",
        "claim_triage",
        "escalation_review",
        "claims_review",
        "claims_review_pure",
    }


def _write_reference_files(tmp_path: Path) -> None:
    env_dir = tmp_path / "environments"
    env_dir.mkdir()
    (env_dir / "local.yaml").write_text("name: local\n", encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "base.yaml").write_text("name: base\n", encoding="utf-8")


def _write_project_run_policy_fixture(
    tmp_path: Path,
    *,
    include_policy: bool = True,
) -> tuple[Path, str | None]:
    _write_demo_project_package(tmp_path)
    _write_env_override_workflow_yaml(tmp_path / "workflow.yaml")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    project_yaml = _dedent(
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        """
    )
    expected_hash = None
    if include_policy:
        policy_dir = tmp_path / "policies"
        policy_dir.mkdir()
        (policy_dir / "fake-only.yaml").write_text(
            _dedent(
                """
                version: "1"
                name: fake-only
                providers:
                  allowed:
                    fake: {}
                """
            ),
            encoding="utf-8",
        )
        project_yaml += "policies:\n  fake-only: policies/fake-only.yaml\n"
    project_path = _write_project_yaml(tmp_path, project_yaml)
    if include_policy:
        expected_hash = compose_project_policies(
            load_project_spec(project_path),
            ("fake-only",),
        ).policy_hash
    return project_path, expected_hash


def _write_project_yaml(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "typeflux.project.yaml"
    path.write_text(_dedent(content), encoding="utf-8")
    return path


def _write_workflow_yaml(
    path: Path,
    *,
    yaml_name: str,
    workflow_name: str = "DemoWorkflow",
    provider_type: str = "fake",
    provider_model: str | None = None,
    provider_class: str | None = None,
    provider_base_url: str | None = None,
    registry_type: str = "inline",
    registry_host: str | None = None,
    allow_provider_class: bool = False,
    allowed_module_roots: list[str] | None = None,
    risk_tier: str | None = None,
) -> None:
    # A custom provider class is the `type: custom` extension path (#189).
    if provider_class:
        provider_type = "custom"
    provider_model_line = f"\n                model: {provider_model}" if provider_model else ""
    provider_base_url_line = (
        f"\n                base_url: {provider_base_url}" if provider_base_url else ""
    )
    provider_class_line = f"\n                class: {provider_class}" if provider_class else ""
    risk_tier_line = f"\n              risk_tier: {risk_tier}" if risk_tier else ""
    import_lines = ""
    if allow_provider_class or allowed_module_roots:
        roots = allowed_module_roots or []
        roots_lines = "".join(f"\n                  - {root}" for root in roots)
        import_lines = (
            "\n              imports:"
            f"\n                allow_provider_class: {str(allow_provider_class).lower()}"
        )
        if roots:
            import_lines += f"\n                allowed_module_roots:{roots_lines}"
    if registry_type == "inline":
        registry_block = (
            "                type: inline\n"
            "                prompts:\n"
            "                  first: first {{value}}"
        )
    else:
        host_line = f"\n                host: {registry_host}" if registry_host else ""
        registry_block = f"                type: {registry_type}{host_line}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _dedent(
            f"""
            project: demo_project
            name: {yaml_name}
            task_queue: demo-task-queue
            runtime:
              temporal:
                address: localhost:7233
              registry:
{registry_block}
              provider:
                type: {provider_type}{provider_model_line}{provider_base_url_line}{provider_class_line}{import_lines}
            activities:
              definitions:
                - name: first
                  input: schemas:InputModel
                  output: schemas:OutputModel
                  prompt: first
            workflow:
              name: {workflow_name}{risk_tier_line}
              input: schemas:InputModel
              output: schemas:OutputModel
              steps:
                - id: first
                  activity: first
            """
        ),
        encoding="utf-8",
    )


def _write_demo_project_package(tmp_path: Path) -> None:
    package_dir = tmp_path / "demo_project"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "schemas.py").write_text(
        _dedent(
            """
            from pydantic import BaseModel


            class InputModel(BaseModel):
                value: str


            class OutputModel(BaseModel):
                value: str
            """
        ),
        encoding="utf-8",
    )


def _write_env_override_workflow_yaml(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _dedent(
            """
            project: demo_project
            name: env_override
            task_queue: ${BASE_TASK_QUEUE:-base-queue}
            runtime:
              temporal:
                address: ${TEMPORAL_ADDRESS:-base:7233}
                namespace: ${TEMPORAL_NAMESPACE:-default}
                tls: ${TEMPORAL_TLS:-false}
                api_key: ${TEMPORAL_API_KEY:-}
              registry:
                type: inline
                prompts:
                  first: first {{value}}
              provider:
                type: fake
              observability:
                type: none
            activities:
              definitions:
                - name: first
                  input: schemas:InputModel
                  output: schemas:OutputModel
                  prompt: first
            workflow:
              name: DemoWorkflow
              input: schemas:InputModel
              output: schemas:OutputModel
              steps:
                - id: first
                  activity: first
            """
        ),
        encoding="utf-8",
    )


def _dedent(content: str) -> str:
    lines = content.strip("\n").splitlines()
    indentation = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
    return "\n".join(line[indentation:] for line in lines) + "\n"


def test_validate_fails_closed_on_unbound_elevated_risk_tier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #788 quadrant 1: an elevated declared tier with NO policy binding fails validation.
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow", risk_tier="human_gated")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        """,
    )
    report = validate_project_bundle(load_project_spec(project_path), environment_id="local")
    assert report.ok is False
    statuses = {check.code: check.status for check in report.resolved_workflows[0].checks}
    assert statuses["risk_tier_binding"] == "failed"
    assert any(
        "UNENFORCED" in issue.message for issue in report.issues if "risk_tier" in issue.message
    )


def test_validate_fails_closed_on_bound_policy_without_risk_tiers_dimension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #788 quadrant 2: bound policy WITHOUT a risk_tiers dimension is still inert -> failed.
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow", risk_tier="policy_gated")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "no-tiers.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: no-tiers
            providers:
              allowed:
                fake: {}
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        policies:
          no-tiers: policies/no-tiers.yaml
        validation:
          targets:
            all-envs:
              workflows: [workflow]
              policies: [no-tiers]
        """,
    )
    report = validate_project_bundle(load_project_spec(project_path), environment_id="local")
    statuses = {check.code: check.status for check in report.resolved_workflows[0].checks}
    assert statuses["risk_tier_binding"] == "failed"


def test_validate_passes_bound_dimensioned_and_safe_risk_tiers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #788 quadrants 3+4: bound + risk_tiers dimension passes; safe/undeclared passes unbound.
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow", risk_tier="policy_gated")
    _write_workflow_yaml(tmp_path / "plain.yaml", yaml_name="plain", workflow_name="PlainWorkflow")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "tiered.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: tiered
            providers:
              allowed:
                fake: {}
            risk_tiers:
              policy_gated:
                require_redaction: true
            """
        ),
        encoding="utf-8",
    )
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
          - id: plain
            path: plain.yaml
        environments:
          local: env.yaml
        policies:
          tiered: policies/tiered.yaml
        validation:
          targets:
            all-envs:
              workflows: [workflow]
              policies: [tiered]
        """,
    )
    report = validate_project_bundle(load_project_spec(project_path), environment_id="local")
    by_workflow = {
        validation.workflow_id: {check.code: check.status for check in validation.checks}
        for validation in report.resolved_workflows
    }
    assert by_workflow["workflow"]["risk_tier_binding"] == "passed"
    assert by_workflow["plain"]["risk_tier_binding"] == "passed"  # undeclared = safe


def test_runtime_guard_build_fails_closed_on_unbound_elevated_risk_tier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #788: run/submit/migrate/CP-start share this builder — unbound elevated tier refuses.
    from typeflux.project.environment import resolve_project_workflow
    from typeflux.project.policy_enforcement import (
        ProjectPolicyEnforcementError,
        build_project_policy_runtime_guard,
    )

    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow", risk_tier="prohibited")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        """,
    )
    project = load_project_spec(project_path)
    resolved = resolve_project_workflow(project, workflow_id="workflow", environment_id="local")
    with pytest.raises(ProjectPolicyEnforcementError, match="UNENFORCED"):
        build_project_policy_runtime_guard(project=project, resolved=resolved)


def test_validate_rejects_unsupported_provider_params_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #789 (audit B2): the openai+top_k spec used to pass `validate --environment`
    # (CI's own offline gate) and fail only at worker preflight. It now fails at spec
    # LOAD, surfacing as an invalid-workflow issue in environment-aware validation.
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(tmp_path / "workflow.yaml", yaml_name="workflow", provider_type="openai")
    text = (tmp_path / "workflow.yaml").read_text(encoding="utf-8")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "type: openai":
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = line + f"\n{indent}params:\n{indent}  top_k: 40"
            break
    (tmp_path / "workflow.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        """,
    )
    report = validate_project_bundle(load_project_spec(project_path), environment_id="local")
    assert report.ok is False
    assert any("top_k" in (issue.message or "") for issue in report.issues)


# --- #796/#797 conditional validate checks -------------------------------------------


_TLS_INVARIANT_WORKFLOW_YAML = """
project: demo_project
name: workflow
task_queue: demo-task-queue
runtime:
  temporal:
    address: localhost:7233
    api_key:
      value_from:
        env: TLS_CHECK_TEMPORAL_KEY
  registry:
    type: inline
    prompts:
      first: first {{value}}
  provider:
    type: fake
activities:
  definitions:
    - name: first
      input: schemas:InputModel
      output: schemas:OutputModel
      prompt: first
workflow:
  name: DemoWorkflow
  input: schemas:InputModel
  output: schemas:OutputModel
  steps:
    - id: first
      activity: first
"""


def _validate_single_workflow_bundle(tmp_path: Path, workflow_yaml: str):
    if not (tmp_path / "demo_project").exists():
        _write_demo_project_package(tmp_path)
    (tmp_path / "workflow.yaml").write_text(_dedent(workflow_yaml), encoding="utf-8")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    project_path = _write_project_yaml(
        tmp_path,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        """,
    )
    return validate_project_bundle(load_project_spec(project_path), environment_id="local")


def test_validate_tls_invariant_fails_when_reference_resolves_without_tls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#796: the api-key-requires-TLS invariant fires at validate for a value_from
    reference exactly like the literal form fires at load — authoring shape must not
    change WHEN a security invariant is enforced."""
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TLS_CHECK_TEMPORAL_KEY", "tmprl-live-key")
    report = _validate_single_workflow_bundle(tmp_path, _TLS_INVARIANT_WORKFLOW_YAML)

    checks = {check.code: check for check in report.resolved_workflows[0].checks}
    assert checks["temporal_tls_invariant"].status == "failed"
    assert "tmprl-live-key" not in (checks["temporal_tls_invariant"].message or "")
    assert report.ok is False


def test_validate_tls_invariant_defers_explicitly_when_source_unresolvable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("TLS_CHECK_TEMPORAL_KEY", raising=False)
    report = _validate_single_workflow_bundle(tmp_path, _TLS_INVARIANT_WORKFLOW_YAML)

    checks = {check.code: check for check in report.resolved_workflows[0].checks}
    assert checks["temporal_tls_invariant"].status == "skipped"
    assert "enforced at client connect" in (checks["temporal_tls_invariant"].message or "")
    assert report.ok is True


def test_validate_tls_invariant_absent_when_tls_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TLS_CHECK_TEMPORAL_KEY", "tmprl-live-key")
    report = _validate_single_workflow_bundle(
        tmp_path,
        _TLS_INVARIANT_WORKFLOW_YAML.replace(
            "    address: localhost:7233\n", "    address: localhost:7233\n    tls: true\n"
        ),
    )

    codes = {check.code for check in report.resolved_workflows[0].checks}
    assert "temporal_tls_invariant" not in codes


def test_validate_payload_codec_presence_notice_names_deferred_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#797: wherever required: false defers a codec presence check, validate states what
    is NOT deferred — the runtime fail-closed behavior."""
    monkeypatch.syspath_prepend(str(tmp_path))
    codec_yaml = """
project: demo_project
name: workflow
task_queue: demo-task-queue
runtime:
  temporal:
    address: localhost:7233
    payload_codec:
      type: aes
      current: k1
      keys:
        - id: k1
          value_from:
            env: CODEC_KEY_ONE
            required: false
        - id: k2
          value_from:
            env: CODEC_KEY_TWO
  registry:
    type: inline
    prompts:
      first: first {{value}}
  provider:
    type: fake
activities:
  definitions:
    - name: first
      input: schemas:InputModel
      output: schemas:OutputModel
      prompt: first
workflow:
  name: DemoWorkflow
  input: schemas:InputModel
  output: schemas:OutputModel
  steps:
    - id: first
      activity: first
"""
    report = _validate_single_workflow_bundle(tmp_path, codec_yaml)

    checks = {check.code: check for check in report.resolved_workflows[0].checks}
    notice = checks["payload_codec_presence"]
    assert notice.status == "passed"
    assert notice.details["deferred_keys"] == ["k1"]
    assert "fail-closes at runtime" in notice.details["runtime_behavior"]
