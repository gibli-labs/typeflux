"""The binding-driver seam (#618 slice 3): selection + fail-closed profiles."""

from __future__ import annotations

import pytest

from tests.test_controlplane_api import _setup
from typeflux.project import (
    BINDING_PROFILE_FOR_RUNTIME,
    PolicyGuardUnavailableError,
    PythonVersionedTypeDriver,
    UnsupportedBindingProfileError,
    WorkflowOperations,
    load_project_spec,
)


def test_runtime_to_profile_mapping_matches_the_binding_contract() -> None:
    assert BINDING_PROFILE_FOR_RUNTIME == {
        "python": "python-versioned-type",
        "typescript": "ts-plan-argument",
    }


@pytest.mark.asyncio
async def test_ts_plan_argument_profile_builds_the_plan_less_driver(tmp_path, monkeypatch) -> None:
    from typeflux.project.binding_ts import TsPlanArgumentDriver

    project = load_project_spec(_setup(tmp_path, monkeypatch))
    ops = await WorkflowOperations.for_project_workflow(
        project,
        workflow_id="workflow",
        environment_id="local",
        binding_profile="ts-plan-argument",
    )
    assert isinstance(ops.driver, TsPlanArgumentDriver)
    assert ops.driver.target.workflow_name == "ControlPlaneDemoWorkflow"
    # No in-process runtime on this profile — the facade fails closed.
    with pytest.raises(AttributeError, match="no\\s*.*in-process runtime"):
        _ = ops.runtime


@pytest.mark.asyncio
async def test_ts_plan_argument_profile_rejects_policy_guards(tmp_path, monkeypatch) -> None:
    project = load_project_spec(_setup(tmp_path, monkeypatch))
    with pytest.raises(PolicyGuardUnavailableError, match="policy enforcement requires"):
        await WorkflowOperations.for_project_workflow(
            project,
            workflow_id="workflow",
            environment_id="local",
            policy_ids=("base",),
            binding_profile="ts-plan-argument",
        )


@pytest.mark.asyncio
async def test_unknown_profile_fails_closed(tmp_path, monkeypatch) -> None:
    project = load_project_spec(_setup(tmp_path, monkeypatch))
    with pytest.raises(UnsupportedBindingProfileError, match="unknown binding profile"):
        await WorkflowOperations.for_project_workflow(
            project,
            workflow_id="workflow",
            environment_id="local",
            binding_profile="rust-native",
        )


def test_facade_defaults_to_the_python_driver_and_exposes_its_runtime() -> None:
    class _Runtime:  # the facade only stores it
        pass

    runtime = _Runtime()
    ops = WorkflowOperations(runtime=runtime)  # type: ignore[arg-type]
    assert isinstance(ops.driver, PythonVersionedTypeDriver)
    assert ops.runtime is runtime

    with pytest.raises(ValueError, match="driver or an in-process runtime"):
        WorkflowOperations()


# --- #642: plan-argument start + policy through the runtime's resolver ---------------


class _FakePlanResolver:
    """A contract resolver double: scripted resolve_plan / bundle / validation."""

    runtime = "typescript"

    def __init__(
        self,
        *,
        plan_overrides: dict | None = None,
        policy_hash: str | None = "hash-1",
        failed_checks: tuple | None = None,
    ) -> None:
        self.plan_payload = {
            "plan": {"workflow": "ControlPlaneDemoWorkflow", "steps": [{"id": "s"}]},
            "task_queue": "resolved-queue",
            "spec_digest": "digest-1",
            "workflow_name": "ControlPlaneDemoWorkflow",
            "version_label": None,
            "search_attribute": None,
            **(plan_overrides or {}),
        }
        self._policy_hash = policy_hash
        self._failed_checks = failed_checks or ()

    def resolve_plan(self, manifest_path, *, workflow_id, environment_id):
        from typeflux.controlplane.resolver import ResolvedPlan

        self.resolve_plan_calls = getattr(self, "resolve_plan_calls", 0) + 1
        return ResolvedPlan.model_validate(self.plan_payload)

    def resolve_bundle(
        self, manifest_path, *, workflow_id, environment_id, policy_ids=(), deployment_image=None
    ):
        from types import SimpleNamespace

        self.resolve_bundle_calls = getattr(self, "resolve_bundle_calls", 0) + 1
        policy = (
            None if self._policy_hash is None else SimpleNamespace(policy_hash=self._policy_hash)
        )
        return SimpleNamespace(
            policy=policy,
            workflow=SimpleNamespace(
                input_schema={
                    "name": "ClaimInput",
                    "hash": "h1",
                    "json_schema": {"type": "object"},
                }
            ),
        )

    def validate_project(
        self, manifest_path, *, environment_id=None, workflow_ids=(), policy_ids=()
    ):
        from types import SimpleNamespace

        return SimpleNamespace(
            issues=(),
            resolved_workflows=(SimpleNamespace(checks=tuple(self._failed_checks)),),
        )


class _FakeStartClient:
    """Captures start_workflow calls; optional scripted visibility list."""

    def __init__(self, list_memos: list[dict] | None = None) -> None:
        self.starts: list[dict] = []
        self._list_memos = list_memos

    async def start_workflow(self, workflow_type, *, args, id, task_queue, memo, **kwargs):
        self.starts.append(
            {
                "workflow_type": workflow_type,
                "args": args,
                "id": id,
                "task_queue": task_queue,
                "memo": memo,
                **kwargs,
            }
        )

        class _Handle:
            first_execution_run_id = "run-1"

        return _Handle()

    def list_workflows(self, query):
        memos = self._list_memos

        class _Iter:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if not memos:
                    raise StopAsyncIteration
                memo = memos.pop(0)

                class _Execution:
                    pass

                execution = _Execution()
                execution.memo = memo
                return execution

        return _Iter()


@pytest.mark.asyncio
async def test_ts_start_dispatches_plan_and_input_with_the_identity_memo(
    tmp_path, monkeypatch
) -> None:
    project = load_project_spec(_setup(tmp_path, monkeypatch))
    resolver = _FakePlanResolver()
    ops = await WorkflowOperations.for_project_workflow(
        project,
        workflow_id="workflow",
        environment_id="local",
        binding_profile="ts-plan-argument",
        resolver=resolver,
    )
    client = _FakeStartClient()
    ops.driver._client = client  # bypass the live connect

    receipt = await ops.start({"value": "in"}, workflow_id="exec-1")

    (start,) = client.starts
    assert start["workflow_type"] == "typefluxYamlWorkflow"
    # The binding contract: exactly [plan, input], plan first, dispatched verbatim.
    assert start["args"] == [resolver.plan_payload["plan"], {"value": "in"}]
    assert start["id"] == "exec-1"
    assert start["task_queue"] == "resolved-queue"
    assert start["memo"] == {
        "typeflux_project": ops.driver.target.project_name,
        "typeflux_workflow": "ControlPlaneDemoWorkflow",
        "typeflux_spec_digest": "digest-1",
    }
    assert receipt.workflow_type == "typefluxYamlWorkflow"
    assert receipt.spec_digest == "digest-1"
    assert receipt.run_id == "run-1"
    assert receipt.task_queue == "resolved-queue"


@pytest.mark.asyncio
async def test_ts_start_empty_task_queue_falls_back_to_the_resolved_one(
    tmp_path, monkeypatch
) -> None:
    # Python `task_queue or spec` truthiness: "" falls through (the pinned parity rule).
    project = load_project_spec(_setup(tmp_path, monkeypatch))
    ops = await WorkflowOperations.for_project_workflow(
        project,
        workflow_id="workflow",
        environment_id="local",
        binding_profile="ts-plan-argument",
        resolver=_FakePlanResolver(),
    )
    client = _FakeStartClient()
    ops.driver._client = client
    await ops.start({}, workflow_id="e", task_queue="")
    assert client.starts[0]["task_queue"] == "resolved-queue"


@pytest.mark.asyncio
async def test_ts_start_without_a_resolver_keeps_failing_closed(tmp_path, monkeypatch) -> None:
    project = load_project_spec(_setup(tmp_path, monkeypatch))
    ops = await WorkflowOperations.for_project_workflow(
        project,
        workflow_id="workflow",
        environment_id="local",
        binding_profile="ts-plan-argument",
    )
    from typeflux.project.binding_ts import TsBindingConfigError

    with pytest.raises(TsBindingConfigError, match="subprocess resolver"):
        await ops.start({}, workflow_id="e")


@pytest.mark.asyncio
async def test_ts_pin_refuses_resolver_identity_drift(tmp_path, monkeypatch) -> None:
    # Drift surfaces at PIN time (the plan is resolved + checked at driver
    # construction, #642) — a mismatched identity never even builds a driver.
    project = load_project_spec(_setup(tmp_path, monkeypatch))
    from typeflux.project.binding_ts import TsBindingConfigError

    with pytest.raises(TsBindingConfigError, match="identity drift"):
        await WorkflowOperations.for_project_workflow(
            project,
            workflow_id="workflow",
            environment_id="local",
            binding_profile="ts-plan-argument",
            resolver=_FakePlanResolver(plan_overrides={"workflow_name": "SomeOtherWorkflow"}),
        )


@pytest.mark.asyncio
async def test_ts_start_enforces_a_frozen_version_from_the_memo(tmp_path, monkeypatch) -> None:
    # A prior execution froze v1 to another digest: the start must refuse
    # (message parity with the TS edition's enforceFrozenWorkflowVersion).
    from pathlib import Path

    import yaml as _yaml

    manifest = _setup(tmp_path, monkeypatch)
    # The workflow YAML itself declares the frozen label, so the pure-YAML
    # target and the resolver AGREE at pin time (no identity drift).
    manifest_dir = Path(manifest).parent
    workflow_file = next(
        candidate
        for candidate in sorted(manifest_dir.rglob("*.yaml"))
        if isinstance(doc := _yaml.safe_load(candidate.read_text(encoding="utf-8")), dict)
        and "workflow" in doc
    )
    doc = _yaml.safe_load(workflow_file.read_text(encoding="utf-8"))
    doc["workflow"]["version"] = "v1"
    workflow_file.write_text(_yaml.safe_dump(doc), encoding="utf-8")

    project = load_project_spec(manifest)
    resolver = _FakePlanResolver(plan_overrides={"version_label": "v1"})
    ops = await WorkflowOperations.for_project_workflow(
        project,
        workflow_id="workflow",
        environment_id="local",
        binding_profile="ts-plan-argument",
        resolver=resolver,
    )
    ops.driver._client = _FakeStartClient(
        list_memos=[
            {
                "typeflux_workflow": "ControlPlaneDemoWorkflow",
                "typeflux_project": ops.driver.target.project_name,
                "typeflux_workflow_version": "v1",
                "typeflux_spec_digest": "OTHER-digest",
            }
        ]
    )
    from typeflux.project.binding_ts import TsBindingConfigError

    with pytest.raises(TsBindingConfigError, match="is frozen to spec digest"):
        await ops.start({}, workflow_id="e")


@pytest.mark.asyncio
async def test_ts_policy_selection_is_honored_through_the_resolver(tmp_path, monkeypatch) -> None:
    project = load_project_spec(_setup(tmp_path, monkeypatch))
    # Matching hash + no failed checks → the driver builds (admission passed).
    ops = await WorkflowOperations.for_project_workflow(
        project,
        workflow_id="workflow",
        environment_id="local",
        policy_ids=("base",),
        expected_policy_hash="hash-1",
        binding_profile="ts-plan-argument",
        resolver=_FakePlanResolver(policy_hash="hash-1"),
    )
    from typeflux.project.binding_ts import TsPlanArgumentDriver

    assert isinstance(ops.driver, TsPlanArgumentDriver)


@pytest.mark.asyncio
async def test_ts_policy_pin_reuses_the_verified_bundle_one_round_trip(
    tmp_path, monkeypatch
) -> None:
    # #673 finder: the schema pin must REUSE the policy path's verified bundle —
    # exactly one resolve_bundle per pin, and the schema comes from the bundle
    # resolved under the admitted policy selection.
    project = load_project_spec(_setup(tmp_path, monkeypatch))
    resolver = _FakePlanResolver(policy_hash="hash-1")
    ops = await WorkflowOperations.for_project_workflow(
        project,
        workflow_id="workflow",
        environment_id="local",
        policy_ids=("base",),
        expected_policy_hash="hash-1",
        binding_profile="ts-plan-argument",
        resolver=resolver,
    )
    assert resolver.resolve_bundle_calls == 1
    assert ops.driver._input_json_schema == {"type": "object"}


@pytest.mark.asyncio
async def test_ts_policy_hash_mismatch_refuses_with_python_message_parity(
    tmp_path, monkeypatch
) -> None:
    from typeflux.project.policy_enforcement import ProjectPolicyEnforcementError

    project = load_project_spec(_setup(tmp_path, monkeypatch))
    with pytest.raises(
        ProjectPolicyEnforcementError,
        match="does not match expected deployment policy hash",
    ):
        await WorkflowOperations.for_project_workflow(
            project,
            workflow_id="workflow",
            environment_id="local",
            policy_ids=("base",),
            expected_policy_hash="hash-OTHER",
            binding_profile="ts-plan-argument",
            resolver=_FakePlanResolver(policy_hash="hash-1"),
        )


@pytest.mark.asyncio
async def test_ts_policy_failed_checks_refuse_admission(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from typeflux.project.policy_enforcement import ProjectPolicyEnforcementError

    project = load_project_spec(_setup(tmp_path, monkeypatch))
    failed = (
        SimpleNamespace(code="policy_provider", status="failed", message="model not allowed"),
    )
    with pytest.raises(
        ProjectPolicyEnforcementError,
        match="project policy enforcement failed: policy_provider: model not allowed",
    ):
        await WorkflowOperations.for_project_workflow(
            project,
            workflow_id="workflow",
            environment_id="local",
            policy_ids=("base",),
            binding_profile="ts-plan-argument",
            resolver=_FakePlanResolver(failed_checks=failed),
        )


def _project_with_target(tmp_path, monkeypatch):
    """A ts-operable project whose validation target SELECTS a policy for the workflow."""
    manifest = _setup(tmp_path, monkeypatch)
    from pathlib import Path

    import yaml as _yaml

    manifest_path = Path(manifest)
    data = _yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    (manifest_path.parent / "policies").mkdir(exist_ok=True)
    (manifest_path.parent / "policies" / "base.yaml").write_text(
        "version: '1'\nname: base\n", encoding="utf-8"
    )
    data["policies"] = {"base": "policies/base.yaml"}
    data["validation"] = {
        "targets": {
            "local": {"workflows": ["workflow"], "environment": "local", "policies": ["base"]}
        }
    }
    manifest_path.write_text(_yaml.safe_dump(data), encoding="utf-8")
    return load_project_spec(manifest_path)


@pytest.mark.asyncio
async def test_ts_target_derived_policies_are_enforced_by_default(tmp_path, monkeypatch) -> None:
    # NO explicit policy_ids: the target's selection must still be enforced —
    # without a resolver that is a refusal, never a silently ungoverned op (codex P1).
    project = _project_with_target(tmp_path, monkeypatch)
    with pytest.raises(PolicyGuardUnavailableError, match="selects project policies \\(base\\)"):
        await WorkflowOperations.for_project_workflow(
            project,
            workflow_id="workflow",
            environment_id="local",
            binding_profile="ts-plan-argument",
        )


@pytest.mark.asyncio
async def test_ts_target_derived_policies_verify_through_the_resolver(
    tmp_path, monkeypatch
) -> None:
    project = _project_with_target(tmp_path, monkeypatch)
    resolver = _FakePlanResolver()  # composes fine, no failed checks
    ops = await WorkflowOperations.for_project_workflow(
        project,
        workflow_id="workflow",
        environment_id="local",
        binding_profile="ts-plan-argument",
        resolver=resolver,
    )
    from typeflux.project.binding_ts import TsPlanArgumentDriver

    assert isinstance(ops.driver, TsPlanArgumentDriver)


@pytest.mark.asyncio
async def test_ts_policy_admission_refuses_when_validation_cannot_prove_it(
    tmp_path, monkeypatch
) -> None:
    # A top-level policy issue (unknown id / missing source) or a report with
    # NO resolved workflow means admission is unprovable — refuse (codex P1).
    from types import SimpleNamespace

    from typeflux.project.policy_enforcement import ProjectPolicyEnforcementError

    project = load_project_spec(_setup(tmp_path, monkeypatch))

    class _IssueResolver(_FakePlanResolver):
        def validate_project(
            self, manifest_path, *, environment_id=None, workflow_ids=(), policy_ids=()
        ):
            return SimpleNamespace(
                issues=(
                    SimpleNamespace(
                        code="unknown_validation_policy", message="unknown policy: nope"
                    ),
                ),
                resolved_workflows=(),
            )

    with pytest.raises(ProjectPolicyEnforcementError, match="unknown_validation_policy"):
        await WorkflowOperations.for_project_workflow(
            project,
            workflow_id="workflow",
            environment_id="local",
            policy_ids=("nope",),
            binding_profile="ts-plan-argument",
            resolver=_IssueResolver(),
        )

    class _BailedResolver(_FakePlanResolver):
        def validate_project(
            self, manifest_path, *, environment_id=None, workflow_ids=(), policy_ids=()
        ):
            return SimpleNamespace(
                issues=(SimpleNamespace(code="duplicate_workflow_name", message="dup"),),
                resolved_workflows=(),
            )

    with pytest.raises(ProjectPolicyEnforcementError, match="no resolved workflow to admit"):
        await WorkflowOperations.for_project_workflow(
            project,
            workflow_id="workflow",
            environment_id="local",
            policy_ids=("base",),
            binding_profile="ts-plan-argument",
            resolver=_BailedResolver(),
        )


@pytest.mark.asyncio
async def test_ts_start_dispatches_the_plan_PINNED_at_construction(tmp_path, monkeypatch) -> None:
    # The plan is resolved ONCE at pin (with admission); starts never re-resolve —
    # a YAML edit after the pin cannot dispatch under a stale admission (codex P1).
    project = load_project_spec(_setup(tmp_path, monkeypatch))
    resolver = _FakePlanResolver()
    ops = await WorkflowOperations.for_project_workflow(
        project,
        workflow_id="workflow",
        environment_id="local",
        binding_profile="ts-plan-argument",
        resolver=resolver,
    )
    client = _FakeStartClient()
    ops.driver._client = client
    await ops.start({}, workflow_id="e1")
    await ops.start({}, workflow_id="e2")
    assert resolver.resolve_plan_calls == 1
    assert len(client.starts) == 2


@pytest.mark.asyncio
async def test_ts_driver_composes_a_runtime_profile_into_the_connection(
    tmp_path, monkeypatch
) -> None:
    # The selected runtime profile's runtime.temporal layers between the
    # workflow YAML and the environment overrides (#672, the #568 precedence).
    from pathlib import Path

    import yaml as _yaml

    manifest = _setup(tmp_path, monkeypatch)
    manifest_path = Path(manifest)
    data = _yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data["workflows"][0]["profiles"] = {"runtime": "prod-cluster"}
    data["profiles"] = {"runtime": {"prod-cluster": "profiles/prod.yaml"}}
    (manifest_path.parent / "profiles").mkdir(exist_ok=True)
    (manifest_path.parent / "profiles" / "prod.yaml").write_text(
        "name: prod-cluster\nkind: runtime\nruntime:\n  temporal:\n"
        "    address: prod.cluster.example:7233\n    namespace: prod-ns\n",
        encoding="utf-8",
    )
    manifest_path.write_text(_yaml.safe_dump(data), encoding="utf-8")
    project = load_project_spec(manifest_path)

    ops = await WorkflowOperations.for_project_workflow(
        project,
        workflow_id="workflow",
        environment_id="local",
        binding_profile="ts-plan-argument",
    )
    assert ops.driver.target.address == "prod.cluster.example:7233"
    assert ops.driver.target.namespace == "prod-ns"


@pytest.mark.asyncio
async def test_ts_driver_env_profile_selection_replaces_the_workflow_one(
    tmp_path, monkeypatch
) -> None:
    # Environment-level selection (the environment's PER-WORKFLOW block)
    # REPLACES the workflow-level selection per kind (profiles.py rule).
    from pathlib import Path

    import yaml as _yaml

    manifest = _setup(tmp_path, monkeypatch)
    manifest_path = Path(manifest)
    data = _yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data["workflows"][0]["profiles"] = {"runtime": "wf-cluster"}
    data["profiles"] = {
        "runtime": {"wf-cluster": "profiles/wf.yaml", "env-cluster": "profiles/env.yaml"}
    }
    profiles_dir = manifest_path.parent / "profiles"
    profiles_dir.mkdir(exist_ok=True)
    (profiles_dir / "wf.yaml").write_text(
        "name: wf-cluster\nkind: runtime\nruntime:\n  temporal:\n    namespace: wf-ns\n",
        encoding="utf-8",
    )
    (profiles_dir / "env.yaml").write_text(
        "name: env-cluster\nkind: runtime\nruntime:\n  temporal:\n    namespace: env-ns\n",
        encoding="utf-8",
    )
    # The environment doc gains a per-workflow profiles block selecting env-cluster.
    env_rel = data["environments"]["local"]
    env_path = manifest_path.parent / env_rel
    env_doc = _yaml.safe_load(env_path.read_text(encoding="utf-8"))
    env_doc.setdefault("workflows", {})["workflow"] = {"profiles": {"runtime": "env-cluster"}}
    env_path.write_text(_yaml.safe_dump(env_doc), encoding="utf-8")
    manifest_path.write_text(_yaml.safe_dump(data), encoding="utf-8")
    project = load_project_spec(manifest_path)

    ops = await WorkflowOperations.for_project_workflow(
        project,
        workflow_id="workflow",
        environment_id="local",
        binding_profile="ts-plan-argument",
    )
    assert ops.driver.target.namespace == "env-ns"


@pytest.mark.asyncio
async def test_ts_driver_refuses_an_undeclared_or_wrong_kind_profile(tmp_path, monkeypatch) -> None:
    from pathlib import Path

    import yaml as _yaml

    from typeflux.project.binding_ts import TsBindingConfigError, TsBindingTarget

    manifest = _setup(tmp_path, monkeypatch)
    manifest_path = Path(manifest)
    data = _yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data["workflows"][0]["profiles"] = {"runtime": "ghost"}
    manifest_path.write_text(_yaml.safe_dump(data), encoding="utf-8")
    # Undeclared id: manifest-declaration-first, fail closed.
    with pytest.raises(TsBindingConfigError, match="unknown project runtime profile: ghost"):
        TsBindingTarget.from_project_yaml(
            manifest_path, workflow_id="workflow", environment_id="local"
        )

    # Declared but the file says a DIFFERENT kind: fail closed.
    data["profiles"] = {"runtime": {"ghost": "profiles/ghost.yaml"}}
    (manifest_path.parent / "profiles").mkdir(exist_ok=True)
    (manifest_path.parent / "profiles" / "ghost.yaml").write_text(
        "name: ghost\nkind: provider\nruntime:\n  provider: { type: openai }\n",
        encoding="utf-8",
    )
    manifest_path.write_text(_yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(TsBindingConfigError, match="declares kind 'provider'"):
        TsBindingTarget.from_project_yaml(
            manifest_path, workflow_id="workflow", environment_id="local"
        )


@pytest.mark.asyncio
async def test_ts_driver_profile_selection_edge_cases_fail_closed(tmp_path, monkeypatch) -> None:
    # Finder findings on #672: a typo'd kind, a non-string id, a kind-less
    # profile, and a malformed temporal subtree all refuse — never a silent
    # fallback to the unprofiled cluster and never a bare TypeError.
    from pathlib import Path

    import yaml as _yaml

    from typeflux.project.binding_ts import TsBindingConfigError, TsBindingTarget

    manifest = _setup(tmp_path, monkeypatch)
    manifest_path = Path(manifest)
    base = _yaml.safe_load(manifest_path.read_text(encoding="utf-8"))

    def _with(workflow_profiles, declared=None, profile_yaml=None):
        data = dict(base)
        data["workflows"] = [dict(base["workflows"][0])]
        data["workflows"][0]["profiles"] = workflow_profiles
        if declared is not None:
            data["profiles"] = declared
        manifest_path.write_text(_yaml.safe_dump(data), encoding="utf-8")
        if profile_yaml is not None:
            (manifest_path.parent / "profiles").mkdir(exist_ok=True)
            (manifest_path.parent / "profiles" / "p.yaml").write_text(
                profile_yaml, encoding="utf-8"
            )

    # Unknown kind (typo) — canonical validate_profile_selection parity.
    _with({"runtme": "cloud"})
    with pytest.raises(TsBindingConfigError, match="unknown profile kind 'runtme'"):
        TsBindingTarget.from_project_yaml(
            manifest_path, workflow_id="workflow", environment_id="local"
        )

    # Non-string selection value — 422-class, never a bare TypeError.
    _with({"runtime": {"nested": "oops"}})
    with pytest.raises(TsBindingConfigError, match="must be a profile id string"):
        TsBindingTarget.from_project_yaml(
            manifest_path, workflow_id="workflow", environment_id="local"
        )

    # Kind-less profile file — kind is REQUIRED (canonical mandatory Literal).
    _with(
        {"runtime": "p"},
        declared={"runtime": {"p": "profiles/p.yaml"}},
        profile_yaml="name: p\nruntime:\n  temporal:\n    namespace: x\n",
    )
    with pytest.raises(TsBindingConfigError, match="declares kind None"):
        TsBindingTarget.from_project_yaml(
            manifest_path, workflow_id="workflow", environment_id="local"
        )

    # Present-but-malformed temporal subtree — fail closed, never drop.
    _with(
        {"runtime": "p"},
        declared={"runtime": {"p": "profiles/p.yaml"}},
        profile_yaml="name: p\nkind: runtime\nruntime:\n  temporal: oops\n",
    )
    with pytest.raises(TsBindingConfigError, match="malformed `runtime.temporal`"):
        TsBindingTarget.from_project_yaml(
            manifest_path, workflow_id="workflow", environment_id="local"
        )

    # An ABSENT temporal subtree is LEGAL (an observability-only runtime profile).
    _with(
        {"runtime": "p"},
        declared={"runtime": {"p": "profiles/p.yaml"}},
        profile_yaml="name: p\nkind: runtime\nruntime:\n  observability:\n    type: none\n",
    )
    target = TsBindingTarget.from_project_yaml(
        manifest_path, workflow_id="workflow", environment_id="local"
    )
    assert target.address  # the workflow YAML's connection stands


@pytest.mark.asyncio
async def test_ts_codec_key_resolves_from_environment_variables_not_os_environ(
    tmp_path, monkeypatch
) -> None:
    # #188 FIX 2: a codec key whose value_from.env is supplied ONLY by the selected
    # environment's `variables:` map (NOT the process env) must still resolve — the CP's
    # codec-aware client resolves history without the operator exporting the key in its
    # shell. Before the fix, build_payload_codec read os.environ directly and this failed.
    from pathlib import Path

    import yaml as _yaml

    from typeflux.project.binding_ts import TsBindingTarget

    manifest = _setup(tmp_path, monkeypatch)
    manifest_path = Path(manifest)

    # The key value lives ONLY in the environment variables map — prove os.environ is empty.
    key_value = ("01234567" + "89abcdef") * 2  # exactly 32 bytes (AES-256)
    monkeypatch.delenv("TF_CODEC_KEY", raising=False)

    workflow_path = manifest_path.parent / "workflow.yaml"
    workflow_doc = _yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    workflow_doc["runtime"]["temporal"]["payload_codec"] = {
        "type": "aes",
        "current": "k1",
        "keys": [{"id": "k1", "value_from": {"env": "TF_CODEC_KEY"}}],
    }
    workflow_path.write_text(_yaml.safe_dump(workflow_doc), encoding="utf-8")

    env_path = manifest_path.parent / "environments" / "local.yaml"
    env_doc = _yaml.safe_load(env_path.read_text(encoding="utf-8"))
    env_doc["variables"] = {"TF_CODEC_KEY": key_value}
    env_path.write_text(_yaml.safe_dump(env_doc), encoding="utf-8")

    target = TsBindingTarget.from_project_yaml(
        manifest_path, workflow_id="workflow", environment_id="local"
    )
    assert target.payload_codec is not None  # resolved from `variables`, not os.environ


# --- #671: memo-identity execution listing on the ts driver --------------------------


class _FakeVisibilityClient:
    """Scripted list_workflows rows shaped like temporalio WorkflowExecution."""

    def __init__(self, rows: list) -> None:
        self._rows = rows
        self.queries: list[str] = []
        self.yielded = 0

    def list_workflows(self, query):
        self.queries.append(query)
        rows = iter(self._rows)
        outer = self

        class _Iter:
            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    row = next(rows)
                except StopIteration:
                    raise StopAsyncIteration from None
                outer.yielded += 1
                return row

        return _Iter()


def _execution_row(
    execution_id: str,
    memo: dict,
    *,
    status: str = "RUNNING",
    workflow_type: str = "typefluxYamlWorkflow",
):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    return SimpleNamespace(
        id=execution_id,
        run_id=f"run-{execution_id}",
        workflow_type=workflow_type,
        status=SimpleNamespace(name=status),
        start_time=datetime(2026, 7, 13, tzinfo=UTC),
        close_time=None,
        memo=memo,
    )


def _swap_rows(ops, rows) -> _FakeVisibilityClient:
    client = _FakeVisibilityClient(rows)
    ops.driver._client = client
    return client


async def _ts_ops_with_visibility(tmp_path, monkeypatch, rows, *, plan_overrides=None):
    project = load_project_spec(_setup(tmp_path, monkeypatch))
    ops = await WorkflowOperations.for_project_workflow(
        project,
        workflow_id="workflow",
        environment_id="local",
        binding_profile="ts-plan-argument",
        resolver=_FakePlanResolver(plan_overrides=plan_overrides),
    )
    client = _FakeVisibilityClient(rows)
    ops.driver._client = client  # bypass the live connect
    return ops, client


@pytest.mark.asyncio
async def test_ts_list_executions_filters_by_memo_identity(tmp_path, monkeypatch) -> None:
    # The profile's listing: generic-type scan, client-side memo filter on
    # workflow+project, current_version = memo digest vs the PINNED digest.
    ops, client = await _ts_ops_with_visibility(tmp_path, monkeypatch, [])
    project_name = ops.driver.target.project_name
    mine = {"typeflux_workflow": "ControlPlaneDemoWorkflow", "typeflux_project": project_name}
    client = _swap_rows(
        ops,
        [
            _execution_row("current", {**mine, "typeflux_spec_digest": "digest-1"}),
            _execution_row(
                "stale", {**mine, "typeflux_spec_digest": "old-digest"}, status="COMPLETED"
            ),
            _execution_row(
                "foreign-project",
                {**mine, "typeflux_project": "someone-else", "typeflux_spec_digest": "digest-1"},
            ),
            _execution_row("memo-less", {}),
        ],
    )

    listing = await ops.driver.list_executions()

    assert client.queries == ["WorkflowType = 'typefluxYamlWorkflow'"]
    assert listing.logical_workflow == "ControlPlaneDemoWorkflow"
    assert listing.current_workflow_type == "typefluxYamlWorkflow"
    assert [record.execution_id for record in listing.executions] == ["current", "stale"]
    by_id = {record.execution_id: record for record in listing.executions}
    assert by_id["current"].current_version is True
    assert by_id["stale"].current_version is False
    assert by_id["stale"].status == "COMPLETED"
    assert by_id["current"].run_id == "run-current"
    assert by_id["current"].start_time == "2026-07-13T00:00:00+00:00"
    assert by_id["current"].close_time is None


@pytest.mark.asyncio
async def test_ts_list_executions_narrows_by_the_search_attribute(tmp_path, monkeypatch) -> None:
    ops, client = await _ts_ops_with_visibility(
        tmp_path,
        monkeypatch,
        [],
        plan_overrides={"search_attribute": "TypefluxLogicalName"},
    )

    await ops.driver.list_executions()

    assert client.queries == [
        "WorkflowType = 'typefluxYamlWorkflow' AND TypefluxLogicalName = 'ControlPlaneDemoWorkflow'"
    ]


@pytest.mark.asyncio
async def test_ts_list_executions_clamps_the_limit(tmp_path, monkeypatch) -> None:
    ops, client = await _ts_ops_with_visibility(tmp_path, monkeypatch, [])
    memo = {
        "typeflux_workflow": "ControlPlaneDemoWorkflow",
        "typeflux_project": ops.driver.target.project_name,
        "typeflux_spec_digest": "digest-1",
    }
    client = _swap_rows(ops, [_execution_row(f"e{i}", dict(memo)) for i in range(3)])

    listing = await ops.driver.list_executions(limit=0)  # clamps to 1
    assert len(listing.executions) == 1
    # The iteration stopped at the clamped limit, not after draining the rows.
    assert client.yielded == 1


@pytest.mark.asyncio
async def test_ts_list_executions_bounds_the_scan(tmp_path, monkeypatch) -> None:
    from typeflux.project.binding_ts import EXECUTIONS_SCAN_LIMIT

    ops, client = await _ts_ops_with_visibility(tmp_path, monkeypatch, [])
    # A matching row hides BEYOND the scan bound: best-effort means it is
    # never reached, and the listing comes back empty instead of unbounded.
    rows = [_execution_row(f"noise{i}", {}) for i in range(EXECUTIONS_SCAN_LIMIT)]
    rows.append(
        _execution_row(
            "beyond",
            {
                "typeflux_workflow": "ControlPlaneDemoWorkflow",
                "typeflux_project": ops.driver.target.project_name,
                "typeflux_spec_digest": "digest-1",
            },
        )
    )
    client = _swap_rows(ops, rows)

    listing = await ops.driver.list_executions()

    assert listing.executions == ()
    assert client.yielded == EXECUTIONS_SCAN_LIMIT


@pytest.mark.asyncio
async def test_ts_list_executions_requires_the_pinned_plan(tmp_path, monkeypatch) -> None:
    # Resolver-less driver: current_version is digest-vs-digest, so listing
    # fails closed exactly like start (never a guessed/hollow listing).
    from typeflux.project.binding_ts import TsBindingConfigError

    project = load_project_spec(_setup(tmp_path, monkeypatch))
    ops = await WorkflowOperations.for_project_workflow(
        project,
        workflow_id="workflow",
        environment_id="local",
        binding_profile="ts-plan-argument",
    )
    with pytest.raises(TsBindingConfigError, match="requires the resolved plan"):
        await ops.driver.list_executions()


@pytest.mark.asyncio
async def test_ts_drain_status_groups_running_by_memo_version_identity(
    tmp_path, monkeypatch
) -> None:
    # #686: the ts profile's drain view — RUNNING-only generic-type scan,
    # memo identity filter, grouped by {logical}.{label|digest12} so the keys
    # render exactly like the python binding's versioned type names.
    ops, client = await _ts_ops_with_visibility(tmp_path, monkeypatch, [])
    project_name = ops.driver.target.project_name
    mine = {"typeflux_workflow": "ControlPlaneDemoWorkflow", "typeflux_project": project_name}
    client = _swap_rows(
        ops,
        [
            _execution_row("current-1", {**mine, "typeflux_spec_digest": "digest-1"}),
            _execution_row("current-2", {**mine, "typeflux_spec_digest": "digest-1"}),
            # An old LABELED version still running → its own key.
            _execution_row(
                "old-labeled",
                {
                    **mine,
                    "typeflux_spec_digest": "0123456789abcdef",
                    "typeflux_workflow_version": "v1",
                },
            ),
            # Foreign identity under the same generic type: excluded entirely.
            _execution_row(
                "foreign", {**mine, "typeflux_project": "someone-else", "typeflux_spec_digest": "x"}
            ),
            # Identity match but NO digest/label memo → fail-safe `unknown` key.
            _execution_row("memo-hollow", dict(mine)),
        ],
    )

    drain = await ops.driver.drain_status()

    # Running-only server-side (Python `_drain_query`'s status filter).
    assert client.queries == [
        "WorkflowType = 'typefluxYamlWorkflow' AND ExecutionStatus = 'Running'"
    ]
    assert drain.logical_workflow == "ControlPlaneDemoWorkflow"
    assert drain.current_workflow_type == "ControlPlaneDemoWorkflow.digest-1"
    assert drain.running == {
        "ControlPlaneDemoWorkflow.digest-1": 2,
        "ControlPlaneDemoWorkflow.(unidentified memo)": 1,
        "ControlPlaneDemoWorkflow.v1": 1,
    }
    assert drain.total_running == 4
    assert drain.drained is False


def test_version_identity_key_matches_registered_workflow_type() -> None:
    # The drain keys render exactly like Python's `registered_workflow_type`
    # (label wins, else digest[:12]); a memo missing both reads `unknown` —
    # fail-safe, it can never equal a current key.
    from typeflux.project.binding_ts import _version_identity_key

    assert _version_identity_key("W", "v2", "abcdef") == "W.v2"
    assert _version_identity_key("W", None, "0123456789abcdef") == "W.0123456789ab"
    assert _version_identity_key("W", None, None) == "W.(unidentified memo)"
    assert _version_identity_key("W", None, "") == "W.(unidentified memo)"


@pytest.mark.asyncio
async def test_ts_drain_status_drained_when_only_the_current_version_runs(
    tmp_path, monkeypatch
) -> None:
    # Only-current (and none-running) both read drained (Python `all(...)`
    # semantics, vacuous truth included).
    ops, _ = await _ts_ops_with_visibility(tmp_path, monkeypatch, [])
    project_name = ops.driver.target.project_name
    _swap_rows(
        ops,
        [
            _execution_row(
                "current",
                {
                    "typeflux_workflow": "ControlPlaneDemoWorkflow",
                    "typeflux_project": project_name,
                    "typeflux_spec_digest": "digest-1",
                },
            )
        ],
    )
    drain = await ops.driver.drain_status()
    assert drain.current_workflow_type == "ControlPlaneDemoWorkflow.digest-1"
    assert drain.running == {"ControlPlaneDemoWorkflow.digest-1": 1}
    assert drain.drained is True

    _swap_rows(ops, [])
    empty = await ops.driver.drain_status()
    assert empty.running == {}
    assert empty.total_running == 0
    assert empty.drained is True


@pytest.mark.asyncio
async def test_ts_drain_status_requires_the_pinned_plan(tmp_path, monkeypatch) -> None:
    # Resolver-less driver: the current version identity is resolution output —
    # fail closed like list_executions, never a guessed/hollow drain view.
    from typeflux.project.binding_ts import TsBindingConfigError

    project = load_project_spec(_setup(tmp_path, monkeypatch))
    ops = await WorkflowOperations.for_project_workflow(
        project,
        workflow_id="workflow",
        environment_id="local",
        binding_profile="ts-plan-argument",
    )
    with pytest.raises(TsBindingConfigError, match="requires the resolved plan"):
        await ops.driver.drain_status()


@pytest.mark.asyncio
async def test_ts_task_queue_workers_describe_and_degradation(tmp_path, monkeypatch) -> None:
    # #686: workers go through the driver's own connection — the resolved plan's
    # queue by default, an explicit override wins, an EMPTY override falls
    # back (Python `or` truthiness), and a describe failure degrades IN-BAND.
    from types import SimpleNamespace

    ops, _ = await _ts_ops_with_visibility(tmp_path, monkeypatch, [])

    class _FakeService:
        def __init__(self) -> None:
            self.requests = []

        async def describe_task_queue(self, request):
            self.requests.append(request)
            return SimpleNamespace(pollers=[object(), object()])

    service = _FakeService()
    ops.driver._client = SimpleNamespace(workflow_service=service)

    workers = await ops.driver.task_queue_workers()
    assert workers.task_queue == "resolved-queue"  # the PINNED plan's queue
    assert workers.reachable is True
    assert workers.workers_polling == 2
    assert workers.detail is None
    assert service.requests[-1].task_queue.name == "resolved-queue"

    overridden = await ops.driver.task_queue_workers(task_queue="panel-queue")
    assert overridden.task_queue == "panel-queue"
    assert service.requests[-1].task_queue.name == "panel-queue"

    empty = await ops.driver.task_queue_workers(task_queue="")
    assert empty.task_queue == "resolved-queue"

    class _FailingService:
        async def describe_task_queue(self, request):
            raise RuntimeError("DescribeTaskQueue: namespace not found")

    ops.driver._client = SimpleNamespace(workflow_service=_FailingService())
    degraded = await ops.driver.task_queue_workers()
    assert degraded.reachable is False
    assert degraded.workers_polling == 0
    assert degraded.detail is not None
    assert degraded.detail.startswith("could not reach Temporal: ")


@pytest.mark.asyncio
async def test_ts_task_queue_workers_require_a_queue_source(tmp_path, monkeypatch) -> None:
    # Resolver-less driver with no explicit queue: the task queue is resolution
    # output — fail closed; an EXPLICIT queue still works plan-lessly.
    from types import SimpleNamespace

    from typeflux.project.binding_ts import TsBindingConfigError

    project = load_project_spec(_setup(tmp_path, monkeypatch))
    ops = await WorkflowOperations.for_project_workflow(
        project,
        workflow_id="workflow",
        environment_id="local",
        binding_profile="ts-plan-argument",
    )
    with pytest.raises(TsBindingConfigError, match="requires the resolved plan"):
        await ops.driver.task_queue_workers()

    class _FakeService:
        async def describe_task_queue(self, request):
            return SimpleNamespace(pollers=[])

    ops.driver._client = SimpleNamespace(workflow_service=_FakeService())
    explicit = await ops.driver.task_queue_workers(task_queue="explicit-queue")
    assert explicit.task_queue == "explicit-queue"
    assert explicit.reachable is True
    assert explicit.workers_polling == 0


# --- #204: migrate on the python-versioned-type driver -------------------------------


@pytest.mark.asyncio
async def test_python_driver_migrate_terminates_and_resubmits(monkeypatch) -> None:
    from types import SimpleNamespace

    from pydantic import BaseModel
    from temporalio.api.enums.v1 import EventType

    from typeflux.project.binding import PythonVersionedTypeDriver

    class _In(BaseModel):
        value: str

    class _WorkflowClass:
        __typeflux_workflow_type__ = "W.newkey000000"
        __typeflux_project__ = "proj"

        @staticmethod
        def run(input_value):  # annotations assigned below
            raise NotImplementedError

    _WorkflowClass.run.__annotations__ = {"input_value": _In, "return": _In}

    calls: dict = {"terminates": [], "starts": []}

    class _Handle:
        async def describe(self):
            return SimpleNamespace(
                workflow_type="W.oldkey000000",
                run_id="old-run",
                memo={"typeflux_project": "proj", "typeflux_workflow": "W"},
            )

        async def query(self, name, *, result_type=None):
            return {"state": "running", "waiting_gates": []}

        async def terminate(self, reason):
            calls["terminates"].append(reason)

        async def fetch_history_events(self, *, page_size=None):
            yield SimpleNamespace(
                event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED,
                workflow_execution_started_event_attributes=SimpleNamespace(
                    input=SimpleNamespace(payloads=[{"value": "carried"}])
                ),
            )

    class _Service:
        async def describe_task_queue(self, request):
            return SimpleNamespace(pollers=[object()])

    class _Client:
        data_converter = _FakeDataConverter()
        workflow_service = _Service()

        def get_workflow_handle(self, workflow_id, run_id=None):
            return _Handle()

    async def _start_workflow(input_value, *, id, task_queue=None, memo=None, **kwargs):
        calls["starts"].append({"input": input_value, "id": id, "memo": memo})
        return SimpleNamespace(run_id="new-run")

    runtime = SimpleNamespace(
        client=_Client(),
        workflow_class=_WorkflowClass,
        spec=SimpleNamespace(
            task_queue="q",
            workflow=SimpleNamespace(name="W"),
            runtime=SimpleNamespace(temporal=SimpleNamespace(namespace="ns")),
        ),
        start_workflow=_start_workflow,
    )
    driver = PythonVersionedTypeDriver(runtime=runtime)  # type: ignore[arg-type]

    result = await driver.migrate("exec-1", reason="rollover")

    assert calls["terminates"] == ["typeflux migrate to W.newkey000000: rollover"]
    (start,) = calls["starts"]
    assert isinstance(start["input"], _In) and start["input"].value == "carried"
    assert start["memo"] == {
        "typeflux_migrated_from_version": "W.oldkey000000",
        "typeflux_migrated_from": "old-run",
    }
    assert result.old_run_id == "old-run"
    assert result.new_run_id == "new-run"
    assert result.old_version_key == "W.oldkey000000"
    assert result.new_version_key == "W.newkey000000"
    assert result.abandoned_gate_ids == ()


@pytest.mark.asyncio
async def test_python_driver_migrate_dry_run_previews_without_mutating(monkeypatch) -> None:
    # #791: identical preflights, one early return — nothing terminated or started.
    from types import SimpleNamespace

    from pydantic import BaseModel
    from temporalio.api.enums.v1 import EventType

    from typeflux.project.binding import PythonVersionedTypeDriver

    class _In(BaseModel):
        value: str

    class _WorkflowClass:
        __typeflux_workflow_type__ = "W.newkey000000"
        __typeflux_project__ = "proj"

        @staticmethod
        def run(input_value):  # annotations assigned below
            raise NotImplementedError

    _WorkflowClass.run.__annotations__ = {"input_value": _In, "return": _In}

    calls: dict = {"terminates": [], "starts": []}

    class _Handle:
        async def describe(self):
            return SimpleNamespace(
                workflow_type="W.oldkey000000",
                run_id="old-run",
                memo={"typeflux_project": "proj", "typeflux_workflow": "W"},
            )

        async def query(self, name, *, result_type=None):
            return {"state": "running", "waiting_gates": []}

        async def terminate(self, reason):
            calls["terminates"].append(reason)

        async def fetch_history_events(self, *, page_size=None):
            yield SimpleNamespace(
                event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED,
                workflow_execution_started_event_attributes=SimpleNamespace(
                    input=SimpleNamespace(payloads=[{"value": "carried"}])
                ),
            )

    class _Service:
        async def describe_task_queue(self, request):
            return SimpleNamespace(pollers=[object()])

    class _Client:
        data_converter = _FakeDataConverter()
        workflow_service = _Service()

        def get_workflow_handle(self, workflow_id, run_id=None):
            return _Handle()

    async def _start_workflow(input_value, *, id, task_queue=None, memo=None, **kwargs):
        calls["starts"].append({"input": input_value, "id": id, "memo": memo})
        return SimpleNamespace(run_id="new-run")

    runtime = SimpleNamespace(
        client=_Client(),
        workflow_class=_WorkflowClass,
        spec=SimpleNamespace(
            task_queue="q",
            workflow=SimpleNamespace(name="W"),
            runtime=SimpleNamespace(temporal=SimpleNamespace(namespace="ns")),
        ),
        start_workflow=_start_workflow,
    )
    driver = PythonVersionedTypeDriver(runtime=runtime)  # type: ignore[arg-type]

    result = await driver.migrate("exec-1", reason="rollover", dry_run=True)

    assert calls["terminates"] == []
    assert calls["starts"] == []
    assert result.dry_run is True
    assert result.old_run_id == "old-run"
    assert result.new_run_id is None
    assert result.old_version_key == "W.oldkey000000"
    assert result.new_version_key == "W.newkey000000"
    assert result.abandoned_gate_ids == ()


@pytest.mark.asyncio
async def test_python_driver_migrate_refuses_same_version(monkeypatch) -> None:
    from types import SimpleNamespace

    from typeflux.project.binding import PythonVersionedTypeDriver
    from typeflux.project.migrate import SameVersionMigrateError

    class _WorkflowClass:
        __typeflux_workflow_type__ = "W.samekey00000"
        __typeflux_project__ = "proj"

        @staticmethod
        def run(input_value):
            raise NotImplementedError

    class _Handle:
        async def describe(self):
            return SimpleNamespace(
                workflow_type="W.samekey00000",
                run_id="r",
                memo={"typeflux_project": "proj", "typeflux_workflow": "W"},
            )

    class _Client:
        def get_workflow_handle(self, workflow_id, run_id=None):
            return _Handle()

    runtime = SimpleNamespace(
        client=_Client(),
        workflow_class=_WorkflowClass,
        spec=SimpleNamespace(
            task_queue="q",
            workflow=SimpleNamespace(name="W"),
            runtime=SimpleNamespace(temporal=SimpleNamespace(namespace="ns")),
        ),
    )
    driver = PythonVersionedTypeDriver(runtime=runtime)  # type: ignore[arg-type]
    with pytest.raises(SameVersionMigrateError, match="already runs the current version"):
        await driver.migrate("exec-1")


def _python_migrate_driver(calls: dict, *, terminate_exc=None, start_exc=None):
    """A PythonVersionedTypeDriver over compact fakes for the post-terminate paths."""
    from types import SimpleNamespace

    from pydantic import BaseModel
    from temporalio.api.enums.v1 import EventType

    from typeflux.project.binding import PythonVersionedTypeDriver

    class _In(BaseModel):
        value: str

    class _WorkflowClass:
        __typeflux_workflow_type__ = "W.newkey000000"
        __typeflux_project__ = "proj"

        @staticmethod
        def run(input_value):  # annotations assigned below
            raise NotImplementedError

    _WorkflowClass.run.__annotations__ = {"input_value": _In, "return": _In}

    class _Handle:
        async def describe(self):
            return SimpleNamespace(
                workflow_type="W.oldkey000000",
                run_id="old-run",
                memo={"typeflux_project": "proj", "typeflux_workflow": "W"},
            )

        async def query(self, name, *, result_type=None):
            return {"state": "running", "waiting_gates": []}

        async def terminate(self, reason):
            if terminate_exc is not None:
                raise terminate_exc
            calls.setdefault("terminates", []).append(reason)

        async def fetch_history_events(self, *, page_size=None):
            yield SimpleNamespace(
                event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED,
                workflow_execution_started_event_attributes=SimpleNamespace(
                    input=SimpleNamespace(payloads=[{"value": "carried"}])
                ),
            )

    class _Service:
        async def describe_task_queue(self, request):
            return SimpleNamespace(pollers=[object()])

    class _Client:
        data_converter = _FakeDataConverter()
        workflow_service = _Service()

        def get_workflow_handle(self, workflow_id, run_id=None):
            return _Handle()

    async def _start_workflow(input_value, *, id, task_queue=None, memo=None, **kwargs):
        if start_exc is not None:
            raise start_exc
        calls.setdefault("starts", []).append({"input": input_value, "id": id, "memo": memo})
        return SimpleNamespace(run_id="new-run")

    runtime = SimpleNamespace(
        client=_Client(),
        workflow_class=_WorkflowClass,
        spec=SimpleNamespace(
            task_queue="q",
            workflow=SimpleNamespace(name="W"),
            runtime=SimpleNamespace(temporal=SimpleNamespace(namespace="ns")),
        ),
        start_workflow=_start_workflow,
    )
    return PythonVersionedTypeDriver(runtime=runtime)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_python_driver_migrate_partial_failure_is_distinguished() -> None:
    # The start leg fails AFTER the terminate: the error must be the DISTINGUISHED
    # partial shape naming the terminated run — never the refusal 422 shape.
    from typeflux.project.migrate import MigratePartialError

    calls: dict = {}
    driver = _python_migrate_driver(calls, start_exc=RuntimeError("connect reset"))
    with pytest.raises(MigratePartialError) as excinfo:
        await driver.migrate("exec-1")
    message = str(excinfo.value)
    assert "old run old-run of execution 'exec-1' was already terminated" in message
    assert "the replacement start failed: connect reset" in message
    assert "resubmit via a normal start" in message
    assert len(calls["terminates"]) == 1  # the terminate DID happen (partial state)


@pytest.mark.asyncio
async def test_python_driver_migrate_classifies_a_terminate_race_as_conflict() -> None:
    # Terminate against an already-closed execution (the SDK's NOT_FOUND shape):
    # a 409 MigrateExecutionClosedError, never an opaque 500 — and no start.
    from temporalio.service import RPCError, RPCStatusCode

    from typeflux.project.migrate import MigrateExecutionClosedError

    calls: dict = {}
    driver = _python_migrate_driver(
        calls,
        terminate_exc=RPCError(
            "workflow execution already completed", RPCStatusCode.NOT_FOUND, b""
        ),
    )
    with pytest.raises(MigrateExecutionClosedError, match="already closed") as excinfo:
        await driver.migrate("exec-1")
    assert "completed or been migrated concurrently" in str(excinfo.value)
    assert "starts" not in calls  # nothing was started


# --- #204: migrate (terminate-and-resubmit) on the ts driver -------------------------


class _FakeMigrateHandle:
    """A cross-version handle: describe/query/terminate/history, all recorded."""

    def __init__(
        self, calls: dict, *, workflow_type, run_id, memo, status, start_args, terminate_exc=None
    ):
        self._calls = calls
        self._workflow_type = workflow_type
        self._run_id = run_id
        self._memo = memo
        self._status = status
        self._start_args = start_args
        self._terminate_exc = terminate_exc

    async def describe(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            workflow_type=self._workflow_type, run_id=self._run_id, memo=self._memo
        )

    async def query(self, name, *, result_type=None):
        self._calls.setdefault("queries", []).append((name, self._run_id))
        return self._status

    async def terminate(self, reason):
        if self._terminate_exc is not None:
            raise self._terminate_exc
        self._calls.setdefault("terminates", []).append({"reason": reason, "run_id": self._run_id})

    async def fetch_history_events(self, *, page_size=None):
        from types import SimpleNamespace

        from temporalio.api.enums.v1 import EventType

        yield SimpleNamespace(
            event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED,
            workflow_execution_started_event_attributes=SimpleNamespace(
                input=SimpleNamespace(payloads=list(self._start_args))
            ),
        )


class _FakeDataConverter:
    async def decode(self, payloads):
        # The fake start args are already Python values; return them verbatim so
        # read_start_event_input picks index 1 (the ts [plan, input] layout).
        return list(payloads)


class _FakeMigrateClient:
    """Client seam for migrate: handle factory + poller service + start capture."""

    def __init__(
        self,
        *,
        workflow_type,
        run_id,
        memo,
        status,
        start_args,
        pollers,
        terminate_exc=None,
        start_exc=None,
        list_memos=None,
    ):
        self.calls: dict = {"getHandle": [], "starts": []}
        self.data_converter = _FakeDataConverter()
        self._handle = _FakeMigrateHandle(
            self.calls,
            workflow_type=workflow_type,
            run_id=run_id,
            memo=memo,
            status=status,
            start_args=start_args,
            terminate_exc=terminate_exc,
        )
        self._pollers = pollers
        self._start_exc = start_exc
        self._list_memos = list_memos
        outer = self

        class _Service:
            async def describe_task_queue(self, request):
                from types import SimpleNamespace

                return SimpleNamespace(pollers=[object()] * outer._pollers)

        self.workflow_service = _Service()
        if list_memos is not None:
            # Optional visibility support: the frozen-version preflight uses it
            # (absent => the check degrades warn-and-skip, like the TS edition).
            def list_workflows(query):
                memos = list(list_memos)

                class _Iter:
                    def __aiter__(self):
                        return self

                    async def __anext__(self):
                        if not memos:
                            raise StopAsyncIteration
                        memo_row = memos.pop(0)

                        class _Execution:
                            pass

                        execution = _Execution()
                        execution.memo = memo_row
                        return execution

                return _Iter()

            self.list_workflows = list_workflows

    def get_workflow_handle(self, workflow_id, run_id=None):
        self.calls["getHandle"].append({"workflow_id": workflow_id, "run_id": run_id})
        return self._handle

    async def start_workflow(self, workflow_type, *, args, id, task_queue, memo, **kwargs):
        if self._start_exc is not None:
            raise self._start_exc
        self.calls["starts"].append(
            {"args": args, "id": id, "task_queue": task_queue, "memo": memo}
        )

        class _Handle:
            first_execution_run_id = "new-run"

        return _Handle()


async def _ts_migrate_ops(
    tmp_path,
    monkeypatch,
    *,
    old_digest,
    status=None,
    pollers=1,
    terminate_exc=None,
    start_exc=None,
    list_memos=None,
):
    project = load_project_spec(_setup(tmp_path, monkeypatch))
    ops = await WorkflowOperations.for_project_workflow(
        project,
        workflow_id="workflow",
        environment_id="local",
        binding_profile="ts-plan-argument",
        resolver=_FakePlanResolver(),  # version_label None, digest "digest-1", queue resolved-queue
    )
    project_name = ops.driver.target.project_name
    memo = {
        "typeflux_project": project_name,
        "typeflux_workflow": "ControlPlaneDemoWorkflow",
        "typeflux_spec_digest": old_digest,
    }
    client = _FakeMigrateClient(
        workflow_type="typefluxYamlWorkflow",
        run_id="old-run",
        memo=memo,
        status=status if status is not None else {"state": "running", "waiting_gates": []},
        start_args=["PLAN", {"value": "carried"}],
        pollers=pollers,
        terminate_exc=terminate_exc,
        start_exc=start_exc,
        list_memos=list_memos,
    )
    ops.driver._client = client
    return ops, client


@pytest.mark.asyncio
async def test_ts_migrate_terminates_and_resubmits_with_provenance(tmp_path, monkeypatch) -> None:
    ops, client = await _ts_migrate_ops(tmp_path, monkeypatch, old_digest="0" * 64)
    result = await ops.driver.migrate("exec-1", reason="budget cut")

    # Terminated with the canonical reason (+ note) on the re-pinned old run.
    (terminate,) = client.calls["terminates"]
    assert (
        terminate["reason"] == "typeflux migrate to ControlPlaneDemoWorkflow.digest-1: budget cut"
    )
    # The new run re-embeds the CURRENT version's plan (arg 0, from the resolver) and carries the
    # OLD input (arg 1, decoded from the old start event) — migrate onto a new graph, same input.
    (start,) = client.calls["starts"]
    assert (
        start["args"][0] == ops.driver._pinned_plan.plan
    )  # current version's plan, not the old one
    assert start["args"][1] == {"value": "carried"}  # input carried over from the old run
    assert start["memo"]["typeflux_migrated_from"] == "old-run"
    assert (
        start["memo"]["typeflux_migrated_from_version"] == "ControlPlaneDemoWorkflow.000000000000"
    )
    assert start["memo"]["typeflux_workflow"] == "ControlPlaneDemoWorkflow"
    assert result.old_run_id == "old-run"
    assert result.new_run_id == "new-run"
    assert result.old_version_key == "ControlPlaneDemoWorkflow.000000000000"
    assert result.new_version_key == "ControlPlaneDemoWorkflow.digest-1"
    assert result.abandoned_gate_ids == ()


@pytest.mark.asyncio
async def test_ts_migrate_refuses_same_version(tmp_path, monkeypatch) -> None:
    from typeflux.project.migrate import SameVersionMigrateError

    ops, client = await _ts_migrate_ops(tmp_path, monkeypatch, old_digest="digest-1")
    with pytest.raises(SameVersionMigrateError, match="already runs the current version"):
        await ops.driver.migrate("exec-1")
    assert "terminates" not in client.calls  # refused before terminating
    assert client.calls["starts"] == []


@pytest.mark.asyncio
async def test_ts_migrate_refuses_when_no_workers_serve_the_target_queue(
    tmp_path, monkeypatch
) -> None:
    from typeflux.project.migrate import NoServingWorkersError

    ops, client = await _ts_migrate_ops(tmp_path, monkeypatch, old_digest="0" * 64, pollers=0)
    with pytest.raises(NoServingWorkersError, match="no workers are polling the target task queue"):
        await ops.driver.migrate("exec-1")
    # Fail-closed BEFORE terminating: the old run stays alive.
    assert "terminates" not in client.calls or client.calls["terminates"] == []
    assert client.calls["starts"] == []


@pytest.mark.asyncio
async def test_ts_migrate_gate_refusal_then_abandon_path(tmp_path, monkeypatch) -> None:
    from typeflux.project.migrate import WaitingGateMigrateError

    gated = {
        "state": "waiting_for_review",
        "waiting_gates": [{"gate_id": "g1", "after_step": "s1", "valid_user_decisions": {}}],
    }
    ops, client = await _ts_migrate_ops(tmp_path, monkeypatch, old_digest="0" * 64, status=gated)
    with pytest.raises(WaitingGateMigrateError, match="waiting at review gate"):
        await ops.driver.migrate("exec-1")
    assert "terminates" not in client.calls  # refused before terminating

    # Same driver/client, now acknowledging the gate loss: it terminates and resubmits.
    result = await ops.driver.migrate("exec-1", abandon_gates=True)
    assert result.abandoned_gate_ids == ("g1",)
    assert len(client.calls["terminates"]) == 1


@pytest.mark.asyncio
async def test_ts_migrate_refuses_a_foreign_project_execution(tmp_path, monkeypatch) -> None:
    from typeflux.core.errors import LifecycleBindingError

    ops, client = await _ts_migrate_ops(tmp_path, monkeypatch, old_digest="0" * 64)
    # Override the described memo to a different project.
    ops.driver._client._handle._memo = {**client._handle._memo, "typeflux_project": "someone-else"}
    with pytest.raises(LifecycleBindingError, match="belongs to project 'someone-else'"):
        await ops.driver.migrate("exec-1")
    assert "terminates" not in client.calls or client.calls["terminates"] == []


@pytest.mark.asyncio
async def test_ts_migrate_validates_the_carried_input_before_terminating(
    tmp_path, monkeypatch
) -> None:
    # PREFLIGHT-BEFORE-TERMINATE (#204 review): a carried input that no longer
    # validates against the CURRENT version's input schema refuses the migration
    # while the old run is still alive — the terminate is never reached.
    from typeflux.project.migrate import MigratedInputError

    ops, client = await _ts_migrate_ops(tmp_path, monkeypatch, old_digest="0" * 64)
    ops.driver._input_json_schema = {
        "type": "object",
        "properties": {"value": {"type": "number"}},
        "required": ["value"],
    }
    with pytest.raises(MigratedInputError, match="not valid for the current version"):
        await ops.driver.migrate("exec-1")
    assert "terminates" not in client.calls
    assert client.calls["starts"] == []


@pytest.mark.asyncio
async def test_ts_migrate_requires_the_input_schema_before_terminating(
    tmp_path, monkeypatch
) -> None:
    # A schema-less driver cannot prove the carried input fits the current
    # version — fail closed BEFORE the terminate (the start path's posture).
    from typeflux.project.binding_ts import TsBindingConfigError

    ops, client = await _ts_migrate_ops(tmp_path, monkeypatch, old_digest="0" * 64)
    ops.driver._input_json_schema = None
    with pytest.raises(TsBindingConfigError, match="requires the workflow's input schema"):
        await ops.driver.migrate("exec-1")
    assert "terminates" not in client.calls


@pytest.mark.asyncio
async def test_ts_migrate_frozen_version_preflight_refuses_before_terminating(
    tmp_path, monkeypatch
) -> None:
    # The frozen workflow.version gate runs as a read-only PREFLIGHT: a reused
    # label whose digest changed refuses with the old run still alive.
    from typeflux.project.binding_ts import TsBindingConfigError

    frozen_memo: dict = {
        "typeflux_workflow": "ControlPlaneDemoWorkflow",
        "typeflux_workflow_version": "v1",
        "typeflux_spec_digest": "SOME-OTHER-digest",
    }
    ops, client = await _ts_migrate_ops(
        tmp_path, monkeypatch, old_digest="0" * 64, list_memos=[frozen_memo]
    )
    # The frozen memo must carry THIS project's identity for the scan to match.
    frozen_memo["typeflux_project"] = ops.driver.target.project_name
    # Pin a frozen label onto the resolved plan (the memo above froze v1 to a
    # different digest, so the preflight must refuse).
    ops.driver._pinned_plan = ops.driver._pinned_plan.model_copy(update={"version_label": "v1"})
    with pytest.raises(TsBindingConfigError, match="is frozen to spec digest"):
        await ops.driver.migrate("exec-1")
    assert "terminates" not in client.calls
    assert client.calls["starts"] == []


@pytest.mark.asyncio
async def test_ts_migrate_partial_failure_is_distinguished(tmp_path, monkeypatch) -> None:
    # The start leg fails AFTER the terminate: the DISTINGUISHED partial shape,
    # naming the terminated run — never the refusal 422 shape.
    from typeflux.project.migrate import MigratePartialError

    ops, client = await _ts_migrate_ops(
        tmp_path, monkeypatch, old_digest="0" * 64, start_exc=RuntimeError("connect reset")
    )
    with pytest.raises(MigratePartialError) as excinfo:
        await ops.driver.migrate("exec-1")
    message = str(excinfo.value)
    assert "old run old-run of execution 'exec-1' was already terminated" in message
    assert "the replacement start failed" in message
    assert "resubmit via a normal start" in message
    assert len(client.calls["terminates"]) == 1


@pytest.mark.asyncio
async def test_ts_migrate_classifies_a_terminate_race_as_conflict(tmp_path, monkeypatch) -> None:
    # Terminate against an already-closed execution: 409 conflict, no start.
    from temporalio.service import RPCError, RPCStatusCode

    from typeflux.project.migrate import MigrateExecutionClosedError

    ops, client = await _ts_migrate_ops(
        tmp_path,
        monkeypatch,
        old_digest="0" * 64,
        terminate_exc=RPCError(
            "workflow execution already completed", RPCStatusCode.NOT_FOUND, b""
        ),
    )
    with pytest.raises(MigrateExecutionClosedError, match="already closed"):
        await ops.driver.migrate("exec-1")
    assert client.calls["starts"] == []


# --- #685: structured TLS (custom CA / mTLS) through the ts driver -------------------


def _write_workflow_tls(tmp_path, tls) -> None:
    """Rewrite the _setup workflow YAML's runtime.temporal.tls block in place."""
    from pathlib import Path

    import yaml as _yaml

    wf_path = Path(tmp_path) / "workflow.yaml"
    doc = _yaml.safe_load(wf_path.read_text(encoding="utf-8"))
    doc["runtime"]["temporal"]["tls"] = tls
    wf_path.write_text(_yaml.safe_dump(doc), encoding="utf-8")


@pytest.mark.asyncio
async def test_ts_target_builds_a_real_tls_config_from_the_structured_block(
    tmp_path, monkeypatch
) -> None:
    # The structured block maps through the CANONICAL yaml/tls.py builder:
    # file slots carry the exact file bytes, inline value_from slots resolve,
    # domain flows through — and _connect passes the built config VERBATIM
    # (never weakened to a bare tls=True).
    from temporalio.client import TLSConfig

    from typeflux.project.binding_ts import TsBindingTarget, TsPlanArgumentDriver

    manifest = _setup(tmp_path, monkeypatch)
    (tmp_path / "ca.pem").write_bytes(b"root-ca-bytes")
    (tmp_path / "client.pem").write_bytes(b"client-cert-bytes")
    monkeypatch.setenv("CLIENT_KEY_PEM", "client-key-material")
    _write_workflow_tls(
        tmp_path,
        {
            "server_root_ca_cert_file": str(tmp_path / "ca.pem"),
            "domain": "temporal.internal",
            "client_cert_file": str(tmp_path / "client.pem"),
            "client_private_key": {"value_from": {"env": "CLIENT_KEY_PEM"}},
        },
    )
    target = TsBindingTarget.from_project_yaml(
        manifest, workflow_id="workflow", environment_id="local"
    )
    assert isinstance(target.tls, TLSConfig)
    assert target.tls.server_root_ca_cert == b"root-ca-bytes"
    assert target.tls.domain == "temporal.internal"
    assert target.tls.client_cert == b"client-cert-bytes"
    assert target.tls.client_private_key == b"client-key-material"

    captured: dict = {}

    async def fake_connect(address, **kwargs):
        captured["address"] = address
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("temporalio.client.Client.connect", fake_connect)
    driver = TsPlanArgumentDriver(target)
    await driver._connect()
    assert captured["tls"] is target.tls
    assert captured["namespace"] == target.namespace


@pytest.mark.asyncio
async def test_ts_target_tls_paths_interpolate_env_and_expand_home(tmp_path, monkeypatch) -> None:
    # ${VAR} substitution mirrors the driver's per-field interpolation of
    # address/namespace; `~` expands like the driver's secret-file rule.
    from temporalio.client import TLSConfig

    from typeflux.project.binding_ts import TsBindingTarget

    manifest = _setup(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    (home / "ca.pem").write_bytes(b"home-ca")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TLS_CA_FILE", "~/ca.pem")
    _write_workflow_tls(tmp_path, {"server_root_ca_cert_file": "${TLS_CA_FILE}"})
    target = TsBindingTarget.from_project_yaml(
        manifest, workflow_id="workflow", environment_id="local"
    )
    assert isinstance(target.tls, TLSConfig)
    assert target.tls.server_root_ca_cert == b"home-ca"
    assert target.tls.client_cert is None


@pytest.mark.asyncio
async def test_ts_target_structured_tls_failure_modes_fail_closed(tmp_path, monkeypatch) -> None:
    from typeflux.project.binding_ts import TsBindingConfigError, TsBindingTarget

    manifest = _setup(tmp_path, monkeypatch)

    # A missing cert file is a loud config error, never a silent trust downgrade.
    _write_workflow_tls(tmp_path, {"server_root_ca_cert_file": str(tmp_path / "absent-ca.pem")})
    with pytest.raises(TsBindingConfigError, match="server_root_ca_cert_file"):
        TsBindingTarget.from_project_yaml(manifest, workflow_id="workflow", environment_id="local")

    # Canonical spec validation: unknown keys refuse (typo'd `ca_file` must not
    # silently become a bare TLS-on connection).
    _write_workflow_tls(tmp_path, {"ca_file": str(tmp_path / "ca.pem")})
    with pytest.raises(TsBindingConfigError, match="runtime.temporal.tls block is invalid"):
        TsBindingTarget.from_project_yaml(manifest, workflow_id="workflow", environment_id="local")

    # A client cert without its private key refuses (canonical pairing rule).
    (tmp_path / "client.pem").write_bytes(b"client-cert-bytes")
    _write_workflow_tls(tmp_path, {"client_cert_file": str(tmp_path / "client.pem")})
    with pytest.raises(TsBindingConfigError, match="must be\\s+configured together"):
        TsBindingTarget.from_project_yaml(manifest, workflow_id="workflow", environment_id="local")

    # Neither a boolean nor a mapping: fail closed with the shape error.
    _write_workflow_tls(tmp_path, "yes-please")
    with pytest.raises(TsBindingConfigError, match="boolean or a structured TLS mapping"):
        TsBindingTarget.from_project_yaml(manifest, workflow_id="workflow", environment_id="local")

    # A required-but-unset inline secret refuses (resolve_optional_secret_bytes).
    monkeypatch.delenv("MISSING_TLS_PEM", raising=False)
    (tmp_path / "client.key").write_bytes(b"client-key-bytes")
    _write_workflow_tls(
        tmp_path,
        {
            "client_cert": {"value_from": {"env": "MISSING_TLS_PEM"}},
            "client_private_key_file": str(tmp_path / "client.key"),
        },
    )
    with pytest.raises(TsBindingConfigError, match="missing required secret"):
        TsBindingTarget.from_project_yaml(manifest, workflow_id="workflow", environment_id="local")


@pytest.mark.asyncio
async def test_ts_drain_safety_review_round(tmp_path, monkeypatch) -> None:
    # (codex + finder) Three safety properties: the scan never narrows by the
    # search attribute; a truncated scan fails CLOSED; a literal version label
    # "unknown" cannot collide with unidentified memos.
    from typeflux.project.binding_ts import (
        EXECUTIONS_SCAN_LIMIT,
        UNIDENTIFIED_VERSION_SUFFIX,
        _version_identity_key,
    )

    # Collision proofing + truthiness at the key level.
    assert _version_identity_key("W", None, None) == f"W.{UNIDENTIFIED_VERSION_SUFFIX}"
    assert _version_identity_key("W", "", "") == f"W.{UNIDENTIFIED_VERSION_SUFFIX}"
    assert _version_identity_key("W", "unknown", None) == "W.unknown"
    assert f"W.{UNIDENTIFIED_VERSION_SUFFIX}" != "W.unknown"

    ops, client = await _ts_ops_with_visibility(
        tmp_path,
        monkeypatch,
        [],
        plan_overrides={"search_attribute": "TypefluxLogicalName"},
    )
    project_name = ops.driver.target.project_name
    memo = {
        "typeflux_workflow": "ControlPlaneDemoWorkflow",
        "typeflux_project": project_name,
        "typeflux_spec_digest": "digest-1",
    }
    # More CURRENT rows than the scan bound: everything seen is current, but
    # the truncated view must never claim drained.
    client = _swap_rows(
        ops, [_execution_row(f"e{i}", dict(memo)) for i in range(EXECUTIONS_SCAN_LIMIT + 1)]
    )
    drain = await ops.driver.drain_status()
    assert drain.drained is False
    # And the attribute never narrowed the safety scan.
    assert "TypefluxLogicalName" not in client.queries[0]
    assert drain.query == "WorkflowType = 'typefluxYamlWorkflow' AND ExecutionStatus = 'Running'"
