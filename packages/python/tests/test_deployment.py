from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from typeflux.project import (
    ProjectDeploymentError,
    build_project_deployment_plan,
    load_project_spec,
    render_project_deployment_plan,
)
from typeflux.project import __main__ as project_cli

_DIGEST_IMAGE = f"ghcr.io/example/typeflux-worker@sha256:{'a' * 64}"


def test_project_deployment_plan_carries_policy_identity_and_worker_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project = load_project_spec(_write_deployment_project(tmp_path))

    plan = build_project_deployment_plan(
        project,
        environment_id="cloud",
        workflow_ids=("claims",),
        image=_DIGEST_IMAGE,
        project_path_in_image="/app/typeflux.project.yaml",
    )

    assert plan.project_name == "claims-platform"
    assert plan.image == _DIGEST_IMAGE
    assert plan.image_digest_pinned is True
    assert len(plan.workers) == 1
    worker = plan.workers[0]
    assert worker.name == "typeflux-claims-platform-cloud-claims"
    assert worker.task_queue == "claims-cloud-typeflux"
    assert worker.policy.selected_policy_ids == ("regulated",)
    assert worker.policy.applied_policy_ids == ("base", "regulated")
    assert len(worker.policy.policy_hash) == 64
    run = (
        "python -m typeflux.project run /app/typeflux.project.yaml "
        "--workflow claims --environment cloud --policy regulated "
        f"--expect-policy-hash {worker.policy.policy_hash}"
    )
    assert worker.command == (
        "sh",
        "-c",
        "rm -f /tmp/typeflux-preflight-ok && "
        f"{run} --preflight && touch /tmp/typeflux-preflight-ok && exec {run}",
    )
    assert worker.config_map["TYPEFLUX_EXPECTED_POLICY_HASH"] == worker.policy.policy_hash


def test_project_deployment_plan_classifies_secret_refs_and_config_map_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project = load_project_spec(_write_deployment_project(tmp_path))

    plan = build_project_deployment_plan(
        project,
        environment_id="cloud",
        workflow_ids=("claims",),
        image=_DIGEST_IMAGE,
    )

    worker = plan.workers[0]
    assert worker.config_map["TEMPORAL_ADDRESS"] == "cloud.tmprl:7233"
    assert worker.config_map["TEMPORAL_NAMESPACE"] == "claims-prod"
    assert worker.config_map["TEMPORAL_TASK_QUEUE"] == "claims-cloud-typeflux"
    assert worker.config_map["TEMPORAL_TLS"] == "true"
    assert worker.config_map["LANGFUSE_HOST"] == "https://cloud.langfuse.example"
    assert worker.config_map["TYPEFLUX_ENVIRONMENT"] == "temporal-cloud"
    assert worker.config_map["TYPEFLUX_OPENAI_MODEL"] == "gpt-4o-mini"
    assert "LANGFUSE_SECRET_KEY" not in worker.config_map
    assert "OPENAI_API_KEY" not in worker.config_map
    assert "TEMPORAL_API_KEY" not in worker.config_map

    secret_refs = {(ref.runtime_path, ref.env_name, ref.secret_key) for ref in worker.secret_env}
    assert {
        ("environment.variables.LANGFUSE_PUBLIC_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_PUBLIC_KEY"),
        ("environment.variables.LANGFUSE_SECRET_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_SECRET_KEY"),
        ("runtime.provider.api_key", "OPENAI_API_KEY", "OPENAI_API_KEY"),
        ("runtime.temporal.api_key", "TEMPORAL_API_KEY", "TEMPORAL_API_KEY"),
    } <= secret_refs
    assert worker.secret_files[0].runtime_path == "runtime.temporal.tls.server_root_ca_cert"
    assert worker.secret_files[0].mount_path == "/etc/typeflux/temporal/ca.pem"
    assert worker.secret_files[0].secret_key == "runtime.temporal.tls.server_root_ca_cert"


_CODEC_YAML = (
    "                  payload_codec:\n"
    "                    type: aes\n"
    "                    current: k1\n"
    "                    keys:\n"
    "                      - id: k1\n"
    "                        value_from:\n"
    "                          env: TF_CODEC_KEY"
)


def test_project_deployment_plan_scaffolds_payload_codec_key_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project = load_project_spec(_write_deployment_project(tmp_path, codec_yaml=_CODEC_YAML))

    plan = build_project_deployment_plan(
        project,
        environment_id="cloud",
        workflow_ids=("claims",),
        image=_DIGEST_IMAGE,
    )

    worker = plan.workers[0]
    # The generated deployment must scaffold the codec key so a codec-enabled worker's
    # build_payload_codec resolves TF_CODEC_KEY at startup — same env-var name and slot path
    # secret_reference_records / build_payload_codec use (#188).
    codec_refs = {(ref.runtime_path, ref.env_name, ref.secret_key) for ref in worker.secret_env}
    assert (
        "runtime.temporal.payload_codec.keys[k1].value_from",
        "TF_CODEC_KEY",
        "TF_CODEC_KEY",
    ) in codec_refs


def test_project_deployment_plan_emits_anthropic_model_env_for_anthropic_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project = load_project_spec(
        _write_deployment_project(
            tmp_path,
            provider_type="anthropic",
            provider_model="claude-sonnet-4-6",
        )
    )

    plan = build_project_deployment_plan(
        project,
        environment_id="cloud",
        workflow_ids=("claims",),
        image=_DIGEST_IMAGE,
    )

    worker = plan.workers[0]
    assert worker.config_map["TYPEFLUX_ANTHROPIC_MODEL"] == "claude-sonnet-4-6"
    assert "TYPEFLUX_OPENAI_MODEL" not in worker.config_map
    assert "ANTHROPIC_API_KEY" not in worker.config_map
    assert "ANTHROPIC_API_KEY" in {ref.env_name for ref in worker.secret_env}


def test_project_deployment_plan_emits_no_model_env_for_fake_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The fake provider has no model env interpolation convention, so the
    # ConfigMap must not carry a model entry under either real provider's key.
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project = load_project_spec(
        _write_deployment_project(
            tmp_path,
            provider_type="fake",
            provider_model="fake-model",
        )
    )

    plan = build_project_deployment_plan(
        project,
        environment_id="cloud",
        workflow_ids=("claims",),
        image=_DIGEST_IMAGE,
    )

    worker = plan.workers[0]
    assert "TYPEFLUX_OPENAI_MODEL" not in worker.config_map
    assert "TYPEFLUX_ANTHROPIC_MODEL" not in worker.config_map


def test_project_deployment_render_writes_secret_free_kubernetes_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project = load_project_spec(_write_deployment_project(tmp_path))
    plan = build_project_deployment_plan(
        project,
        environment_id="cloud",
        workflow_ids=("claims",),
        image=_DIGEST_IMAGE,
        project_path_in_image="/app/typeflux.project.yaml",
    )

    result = render_project_deployment_plan(plan, tmp_path / "rendered")

    assert [Path(item.path).name for item in result.files] == [
        "deployment-plan.json",
        "kubernetes.yaml",
        "secret.scaffold.yaml",
        "secrets.env.example",
    ]
    rendered_plan = json.loads((tmp_path / "rendered" / "deployment-plan.json").read_text())
    assert rendered_plan["workers"][0]["workflow_id"] == "claims"

    kubernetes_text = (tmp_path / "rendered" / "kubernetes.yaml").read_text()
    assert "should-not-be-configmap" not in kubernetes_text
    assert "pk-placeholder" not in kubernetes_text
    assert "sk-placeholder" not in kubernetes_text
    docs = list(yaml.safe_load_all(kubernetes_text))
    # The applyable manifest carries no Secret, so `kubectl apply` cannot clobber
    # populated secrets with blank values.
    assert [doc["kind"] for doc in docs] == ["ConfigMap", "Deployment"]
    assert all(doc["kind"] != "Secret" for doc in docs)

    config_map, deployment = docs
    assert config_map["metadata"]["name"] == "typeflux-claims-platform-cloud-claims-config"
    assert config_map["data"]["TEMPORAL_ADDRESS"] == "cloud.tmprl:7233"
    assert config_map["data"]["TYPEFLUX_EXPECTED_POLICY_HASH"] == plan.workers[0].policy.policy_hash
    assert "TYPEFLUX_K8S_POD_NAME" not in config_map["data"]
    assert "OPENAI_API_KEY" not in config_map["data"]

    scaffold_text = (tmp_path / "rendered" / "secret.scaffold.yaml").read_text()
    assert "Do NOT `kubectl apply`" in scaffold_text
    assert "sk-placeholder" not in scaffold_text
    (secret,) = list(yaml.safe_load_all(scaffold_text))
    assert secret["kind"] == "Secret"
    assert secret["metadata"]["name"] == "typeflux-claims-platform-cloud-claims-secrets"
    assert secret["stringData"]["OPENAI_API_KEY"] == ""
    assert secret["stringData"]["TEMPORAL_API_KEY"] == ""
    assert secret["stringData"]["LANGFUSE_PUBLIC_KEY"] == ""
    assert secret["stringData"]["LANGFUSE_SECRET_KEY"] == ""
    assert secret["stringData"]["runtime.temporal.tls.server_root_ca_cert"] == ""

    pod_spec = deployment["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    assert container["command"] == list(plan.workers[0].command)
    assert container["command"][0:2] == ["sh", "-c"]
    assert container["command"][2].startswith("rm -f /tmp/typeflux-preflight-ok && ")
    assert "--preflight && touch /tmp/typeflux-preflight-ok && exec " in container["command"][2]
    marker_check = ["sh", "-c", "test -f /tmp/typeflux-preflight-ok"]
    assert container["startupProbe"]["exec"]["command"] == marker_check
    assert container["readinessProbe"]["exec"]["command"] == marker_check
    assert container["livenessProbe"]["exec"]["command"] == marker_check
    assert container["envFrom"] == [
        {"configMapRef": {"name": "typeflux-claims-platform-cloud-claims-config"}}
    ]
    env_by_name = {item["name"]: item for item in container["env"]}
    assert env_by_name["TYPEFLUX_RUNTIME_PLATFORM"]["value"] == "kubernetes"
    assert env_by_name["TYPEFLUX_K8S_DEPLOYMENT_NAME"]["value"] == (
        "typeflux-claims-platform-cloud-claims"
    )
    assert env_by_name["TYPEFLUX_K8S_WORKER_NAME"]["value"] == (
        "typeflux-claims-platform-cloud-claims"
    )
    assert env_by_name["TYPEFLUX_CONTAINER_IMAGE"]["value"] == _DIGEST_IMAGE
    assert env_by_name["TYPEFLUX_K8S_NAMESPACE"]["valueFrom"]["fieldRef"] == {
        "fieldPath": "metadata.namespace"
    }
    assert env_by_name["TYPEFLUX_K8S_POD_NAME"]["valueFrom"]["fieldRef"] == {
        "fieldPath": "metadata.name"
    }
    assert env_by_name["TYPEFLUX_K8S_POD_UID"]["valueFrom"]["fieldRef"] == {
        "fieldPath": "metadata.uid"
    }
    assert env_by_name["TYPEFLUX_K8S_NODE_NAME"]["valueFrom"]["fieldRef"] == {
        "fieldPath": "spec.nodeName"
    }
    assert env_by_name["TYPEFLUX_K8S_SERVICE_ACCOUNT"]["valueFrom"]["fieldRef"] == {
        "fieldPath": "spec.serviceAccountName"
    }
    env_refs = {
        item["name"]: item["valueFrom"]["secretKeyRef"]
        for item in container["env"]
        if "secretKeyRef" in item.get("valueFrom", {})
    }
    assert env_refs["OPENAI_API_KEY"] == {
        "name": "typeflux-claims-platform-cloud-claims-secrets",
        "key": "OPENAI_API_KEY",
        "optional": False,
    }
    assert {
        "name": "typeflux-secret-files",
        "mountPath": "/etc/typeflux/temporal/ca.pem",
        "subPath": "runtime.temporal.tls.server_root_ca_cert",
        "readOnly": True,
    } in container["volumeMounts"]
    assert pod_spec["volumes"][1]["secret"]["secretName"] == (
        "typeflux-claims-platform-cloud-claims-secrets"
    )

    secrets_example = (tmp_path / "rendered" / "secrets.env.example").read_text()
    assert "kubectl --from-env-file" in secrets_example
    assert "OPENAI_API_KEY=" in secrets_example
    assert "LANGFUSE_PUBLIC_KEY=" in secrets_example
    assert "pk-placeholder" not in secrets_example

    second = render_project_deployment_plan(plan, tmp_path / "rendered")
    assert [item.sha256 for item in second.files] == [item.sha256 for item in result.files]


def test_project_deployment_plan_rejects_mutable_image_without_escape_hatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project = load_project_spec(_write_deployment_project(tmp_path))

    with pytest.raises(ProjectDeploymentError, match="pinned by digest"):
        build_project_deployment_plan(
            project,
            environment_id="cloud",
            workflow_ids=("claims",),
            image="ghcr.io/example/typeflux-worker:latest",
        )

    plan = build_project_deployment_plan(
        project,
        environment_id="cloud",
        workflow_ids=("claims",),
        image="ghcr.io/example/typeflux-worker:latest",
        allow_mutable_image=True,
    )

    assert plan.image_digest_pinned is False


def test_project_deployment_plan_rejects_shared_task_queue_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project = load_project_spec(_write_deployment_project(tmp_path, include_duplicate=True))

    with pytest.raises(ProjectDeploymentError, match="shared task queue"):
        build_project_deployment_plan(
            project,
            environment_id="cloud",
            workflow_ids=("claims", "claims_duplicate"),
            image=_DIGEST_IMAGE,
        )

    plan = build_project_deployment_plan(
        project,
        environment_id="cloud",
        workflow_ids=("claims", "claims_duplicate"),
        image=_DIGEST_IMAGE,
        allow_shared_task_queue=True,
    )

    assert [worker.workflow_id for worker in plan.workers] == ["claims", "claims_duplicate"]


def test_project_deployment_plan_requires_selected_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project = load_project_spec(_write_deployment_project(tmp_path, include_validation=False))

    with pytest.raises(ProjectDeploymentError, match="requires at least one selected policy"):
        build_project_deployment_plan(
            project,
            environment_id="cloud",
            workflow_ids=("claims",),
            image=_DIGEST_IMAGE,
        )


def test_project_deployment_plan_rejects_unclassified_environment_variables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project = load_project_spec(
        _write_deployment_project(tmp_path, env_file_extra="CUSTOM_RUNTIME=value\n")
    )

    with pytest.raises(ProjectDeploymentError, match="cannot classify environment variable"):
        build_project_deployment_plan(
            project,
            environment_id="cloud",
            workflow_ids=("claims",),
            image=_DIGEST_IMAGE,
        )


def test_project_deployment_plan_does_not_route_key_substring_to_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A non-secret operational variable that merely contains a secret-like token
    # (API_KEY) must not be mis-classified as a Secret reference. With the
    # end-anchored heuristic it falls through to the unclassified hard error
    # rather than silently becoming a secretKeyRef.
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project = load_project_spec(
        _write_deployment_project(tmp_path, env_file_extra="API_KEY_ROTATION_DAYS=30\n")
    )

    with pytest.raises(ProjectDeploymentError, match="cannot classify environment variable"):
        build_project_deployment_plan(
            project,
            environment_id="cloud",
            workflow_ids=("claims",),
            image=_DIGEST_IMAGE,
        )

    # The operator can declare it as a non-secret ConfigMap value explicitly.
    plan = build_project_deployment_plan(
        project,
        environment_id="cloud",
        workflow_ids=("claims",),
        image=_DIGEST_IMAGE,
        config_env_names=("API_KEY_ROTATION_DAYS",),
    )
    worker = plan.workers[0]
    assert worker.config_map["API_KEY_ROTATION_DAYS"] == "30"
    assert "API_KEY_ROTATION_DAYS" not in {ref.env_name for ref in worker.secret_env}


def test_project_deployment_plan_config_env_promotes_custom_variable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # --config-env unblocks custom non-secret operational variables that are not
    # in the built-in safe allowlist, routing them to the ConfigMap (not Secrets).
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project = load_project_spec(
        _write_deployment_project(tmp_path, env_file_extra="CUSTOM_RUNTIME=value\n")
    )

    plan = build_project_deployment_plan(
        project,
        environment_id="cloud",
        workflow_ids=("claims",),
        image=_DIGEST_IMAGE,
        config_env_names=("CUSTOM_RUNTIME",),
    )
    worker = plan.workers[0]
    assert worker.config_map["CUSTOM_RUNTIME"] == "value"
    assert "CUSTOM_RUNTIME" not in {ref.env_name for ref in worker.secret_env}


def test_project_deployment_plan_rejects_policy_admission_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project = load_project_spec(_write_deployment_project(tmp_path, temporal_tls=False))

    with pytest.raises(ProjectDeploymentError, match="project policy admission failed"):
        build_project_deployment_plan(
            project,
            environment_id="cloud",
            workflow_ids=("claims",),
            image=_DIGEST_IMAGE,
        )


def test_project_deployment_plan_rejects_unsafe_file_secret_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project = load_project_spec(_write_deployment_project(tmp_path, ca_mount_path="ca.pem"))

    with pytest.raises(ProjectDeploymentError, match="absolute mount path"):
        build_project_deployment_plan(
            project,
            environment_id="cloud",
            workflow_ids=("claims",),
            image=_DIGEST_IMAGE,
        )


def test_project_cli_deploy_json_emits_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project_path = _write_deployment_project(tmp_path)

    exit_code = project_cli.main(
        [
            "deploy",
            str(project_path),
            "--environment",
            "cloud",
            "--workflow",
            "claims",
            "--image",
            _DIGEST_IMAGE,
            "--json",
        ]
    )

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert exit_code == 0
    assert payload["project_name"] == "claims-platform"
    assert payload["workers"][0]["workflow_id"] == "claims"
    assert payload["workers"][0]["policy"]["selected_policy_ids"] == ["regulated"]
    assert output.err == ""


def test_project_cli_deploy_records_portable_manifest_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # #757 item 1: the recorded project_manifest_path is machine-independent by default.
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project_path = _write_deployment_project(tmp_path)
    base = [
        "deploy",
        str(project_path),
        "--environment",
        "cloud",
        "--workflow",
        "claims",
        "--image",
        _DIGEST_IMAGE,
    ]

    # Default: the manifest's basename (relative to its own directory) — no operator abs path.
    assert project_cli.main([*base, "--output", str(tmp_path / "out"), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["project_manifest_path"] == "typeflux.project.yaml"
    rendered = (tmp_path / "out" / "kubernetes.yaml").read_text(encoding="utf-8")
    assert "typeflux.io/project-manifest-path: typeflux.project.yaml" in rendered
    # The operator's absolute MANIFEST path is no longer baked into the committed artifacts.
    assert str(project_path) not in rendered

    # Explicit override recorded verbatim.
    assert (
        project_cli.main(
            [*base, "--project-manifest-path", "config/typeflux.project.yaml", "--json"]
        )
        == 0
    )
    assert (
        json.loads(capsys.readouterr().out)["project_manifest_path"]
        == "config/typeflux.project.yaml"
    )

    # --absolute-manifest-path restores the operator's resolved absolute path.
    assert project_cli.main([*base, "--absolute-manifest-path", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["project_manifest_path"] == str(project_path)


def test_placeholder_image_digest_rejected_at_build_write_and_promote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #757 item 5: the all-zeros placeholder digest is refused at build, plan-write, and promote.
    from typeflux.project.deployments import verify_deployment_plan, write_deployment_plan

    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project = load_project_spec(_write_deployment_project(tmp_path))
    placeholder = f"ghcr.io/example/typeflux-worker@sha256:{'0' * 64}"

    with pytest.raises(ProjectDeploymentError, match="all-zeros placeholder digest"):
        build_project_deployment_plan(
            project, environment_id="cloud", workflow_ids=("claims",), image=placeholder
        )
    # Opt in → builds.
    built = build_project_deployment_plan(
        project,
        environment_id="cloud",
        workflow_ids=("claims",),
        image=placeholder,
        allow_placeholder_image=True,
    )
    assert built.image == placeholder

    out_dir = tmp_path / "deployments"
    with pytest.raises(ProjectDeploymentError, match="all-zeros placeholder"):
        write_deployment_plan(
            project,
            workflow_id="claims",
            environment_id="cloud",
            image=placeholder,
            policy_ids=("regulated",),
            out_dir=out_dir,
        )
    _, plan = write_deployment_plan(
        project,
        workflow_id="claims",
        environment_id="cloud",
        image=placeholder,
        policy_ids=("regulated",),
        out_dir=out_dir,
        allow_placeholder_image=True,
    )
    # A placeholder plan must NOT promote by default (would only die at pod scheduling). The
    # refusal is its OWN synthetic check code (#757 review): the literal offending image rides
    # plan_value, the explanation rides current_value — never prose in a literal diff slot.
    blocked = verify_deployment_plan(project, plan)
    assert not blocked.ok
    image_mismatch = next(m for m in blocked.mismatches if m.path == "deployment.image_placeholder")
    assert image_mismatch.plan_value == placeholder
    assert "all-zeros placeholder digest" in str(image_mismatch.current_value)
    assert "--allow-placeholder-image" in str(image_mismatch.current_value)
    # The literal diff paths stay same-typed literals — no prose rides deployment.image.
    assert not any(m.path == "deployment.image" for m in blocked.mismatches)
    # Opt in → promotes clean (spec/policy unchanged).
    assert verify_deployment_plan(project, plan, allow_placeholder_image=True).ok


def test_observability_backend_credentials_modeled_in_secret_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #757 item 3: a spec declaring a tracing backend gets the backend's credential NAMES scaffolded.
    monkeypatch.syspath_prepend(str(tmp_path))
    project = load_project_spec(
        _write_observability_project(tmp_path, backend="langfuse", require_observability=True)
    )
    plan = build_project_deployment_plan(
        project,
        environment_id="prod",
        workflow_ids=("review",),
        policy_ids=("base",),
        image=_DIGEST_IMAGE,
    )
    worker = plan.workers[0]
    obs = [ref for ref in worker.secret_env if ref.env_name.startswith("LANGFUSE_")]
    assert sorted(ref.env_name for ref in obs) == ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"]
    # Policy requires observability (#756) → the creds are a hard secretKeyRef.
    assert all(ref.required for ref in obs)
    assert all(ref.runtime_path.startswith("runtime.observability.langfuse.") for ref in obs)

    # Not policy-required → the same creds are optional.
    optional_project = load_project_spec(
        _write_observability_project(
            tmp_path / "opt", backend="langfuse", require_observability=False
        )
    )
    optional_plan = build_project_deployment_plan(
        optional_project,
        environment_id="prod",
        workflow_ids=("review",),
        policy_ids=("base",),
        image=_DIGEST_IMAGE,
    )
    optional_obs = [
        ref for ref in optional_plan.workers[0].secret_env if ref.env_name.startswith("LANGFUSE_")
    ]
    assert len(optional_obs) == 2
    assert all(ref.required is False for ref in optional_obs)

    # langsmith → LANGSMITH_API_KEY.
    smith_project = load_project_spec(
        _write_observability_project(
            tmp_path / "smith", backend="langsmith", require_observability=False
        )
    )
    smith_plan = build_project_deployment_plan(
        smith_project,
        environment_id="prod",
        workflow_ids=("review",),
        policy_ids=("base",),
        image=_DIGEST_IMAGE,
    )
    assert [
        ref.env_name
        for ref in smith_plan.workers[0].secret_env
        if ref.env_name == "LANGSMITH_API_KEY"
    ] == ["LANGSMITH_API_KEY"]


def test_closure_observability_divergence_rejected_at_plan_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #757 review: the finder's exact composition — parent langfuse + child langsmith, each spec
    # individually policy-compliant. Before this gate the plan built/wrote/promoted cleanly with
    # only the parent's creds scaffolded, then crash-looped at pod boot. Now it fails at
    # AUTHORING time with the same composition error the worker boot raises.
    from typeflux.project.deployments import write_deployment_plan
    from typeflux.yaml.runtime import ObservabilityCompositionError

    monkeypatch.syspath_prepend(str(tmp_path))
    project = load_project_spec(
        _write_observability_closure_project(
            tmp_path, parent_backend="langfuse", child_backend="langsmith"
        )
    )
    with pytest.raises(ObservabilityCompositionError, match="observability composition"):
        build_project_deployment_plan(
            project,
            environment_id="prod",
            workflow_ids=("parent",),
            policy_ids=("base",),
            image=_DIGEST_IMAGE,
        )
    # The plan file is the APPROVAL artifact: direct write callers hit the same gate.
    with pytest.raises(ObservabilityCompositionError, match="observability composition"):
        write_deployment_plan(
            project,
            workflow_id="parent",
            environment_id="prod",
            image=_DIGEST_IMAGE,
            policy_ids=("base",),
            out_dir=tmp_path / "deployments",
        )

    # A consistent closure (child none → inherits) builds fine, scaffolding exactly the
    # parent's verified single effective backend's creds.
    consistent = load_project_spec(
        _write_observability_closure_project(
            tmp_path / "ok", parent_backend="langfuse", child_backend="none"
        )
    )
    plan = build_project_deployment_plan(
        consistent,
        environment_id="prod",
        workflow_ids=("parent",),
        policy_ids=("base",),
        image=_DIGEST_IMAGE,
    )
    worker = plan.workers[0]
    langfuse = [ref for ref in worker.secret_env if ref.env_name.startswith("LANGFUSE_")]
    assert sorted(ref.env_name for ref in langfuse) == [
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    ]
    assert not any(ref.env_name == "LANGSMITH_API_KEY" for ref in worker.secret_env)


def _write_observability_closure_project(
    tmp_path: Path, *, parent_backend: str, child_backend: str
) -> Path:
    """A parent (declaring ``parent_backend``) referencing a child (declaring ``child_backend``)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    _write_demo_project_package(tmp_path)

    def workflow_yaml(name: str, backend: str, steps: str) -> str:
        return _dedent(
            f"""
            project: demo_project
            name: {name}
            task_queue: {name}-typeflux
            runtime:
              temporal:
                address: localhost:7233
              registry:
                type: inline
                prompts:
                  first: first {{{{value}}}}
              provider:
                type: fake
              observability:
                type: {backend}
            activities:
              definitions:
                - name: first
                  input: schemas:InputModel
                  output: schemas:OutputModel
                  prompt: first
            workflow:
              name: {name.capitalize()}Workflow
              input: schemas:InputModel
              output: schemas:OutputModel
              steps:
{steps}
            """
        )

    # Steps are interpolated BEFORE _dedent runs, so they carry the template's full indent
    # (top-level keys at 12 spaces; step items at 16). The parent's only step is the child
    # call, so the InputModel → child → OutputModel type chain composes.
    parent_steps = "                - id: assess_child\n                  workflow: child"
    child_steps = "                - id: first\n                  activity: first"
    (tmp_path / "parent.yaml").write_text(
        workflow_yaml("parent", parent_backend, parent_steps), encoding="utf-8"
    )
    (tmp_path / "child.yaml").write_text(
        workflow_yaml("child", child_backend, child_steps), encoding="utf-8"
    )
    env_dir = tmp_path / "environments"
    env_dir.mkdir()
    (env_dir / "prod.yaml").write_text('version: "1"\nname: prod\n', encoding="utf-8")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "base.yaml").write_text(
        'version: "1"\nname: base\nimports:\n  allowed_module_roots: [demo_project]\n',
        encoding="utf-8",
    )
    return _write_project_yaml(
        tmp_path,
        (
            'version: "1"\n'
            "name: closure-obs\n"
            "workflows:\n"
            "  - id: parent\n"
            "    path: parent.yaml\n"
            "  - id: child\n"
            "    path: child.yaml\n"
            "environments:\n"
            "  prod: environments/prod.yaml\n"
            "policies:\n"
            "  base: policies/base.yaml\n"
            "validation:\n"
            "  targets:\n"
            "    prod-parent:\n"
            "      workflows: [parent]\n"
            "      environment: prod\n"
            "      policies: [base]\n"
        ),
    )


def _write_observability_project(
    tmp_path: Path, *, backend: str, require_observability: bool
) -> Path:
    """A minimal project whose workflow declares a tracing backend but NOT its credentials as env vars."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    _write_demo_project_package(tmp_path)
    _write_workflow_yaml(
        tmp_path / "workflows" / "review" / "typeflux.yaml",
        yaml_name="review",
        workflow_name="ReviewWorkflow",
        task_queue="review-typeflux",
    )
    env_dir = tmp_path / "environments"
    env_dir.mkdir()
    (env_dir / "prod.yaml").write_text(
        _dedent(
            f"""
            version: "1"
            name: prod
            variables:
              TYPEFLUX_ENVIRONMENT: prod
            overrides:
              runtime:
                observability:
                  type: {backend}
            """
        ),
        encoding="utf-8",
    )
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    policy_lines = ['version: "1"', "name: base"]
    if require_observability:
        policy_lines += ["observability:", "  required: true"]
    policy_lines += ["imports:", "  allowed_module_roots: [demo_project]"]
    (policy_dir / "base.yaml").write_text("\n".join(policy_lines) + "\n", encoding="utf-8")
    return _write_project_yaml(
        tmp_path,
        (
            'version: "1"\n'
            "name: obs-platform\n"
            "workflows:\n"
            "  - id: review\n"
            "    directory: workflows/review\n"
            "environments:\n"
            "  prod: environments/prod.yaml\n"
            "policies:\n"
            "  base: policies/base.yaml\n"
        ),
    )


def test_project_cli_deploy_output_writes_rendered_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project_path = _write_deployment_project(tmp_path)
    output_dir = tmp_path / "deploy-output"

    exit_code = project_cli.main(
        [
            "deploy",
            str(project_path),
            "--environment",
            "cloud",
            "--workflow",
            "claims",
            "--image",
            _DIGEST_IMAGE,
            "--output",
            str(output_dir),
        ]
    )

    output = capsys.readouterr()
    assert exit_code == 0
    assert "Wrote deployment artifacts" in output.out
    assert (output_dir / "deployment-plan.json").exists()
    assert (output_dir / "kubernetes.yaml").exists()
    assert (output_dir / "secrets.env.example").exists()
    assert output.err == ""


def _write_deployment_project(
    tmp_path: Path,
    *,
    include_duplicate: bool = False,
    include_validation: bool = True,
    temporal_tls: bool = True,
    ca_mount_path: str = "/etc/typeflux/temporal/ca.pem",
    env_file_extra: str = "",
    provider_type: str = "openai",
    provider_model: str = "gpt-4o-mini",
    codec_yaml: str = "",
) -> Path:
    _write_workflow_yaml(
        tmp_path / "workflows" / "claims" / "typeflux.yaml",
        yaml_name="claims",
        workflow_name="ClaimsWorkflow",
        task_queue="claims-typeflux",
    )
    workflows = "  - id: claims\n    directory: workflows/claims\n"
    validation_workflows = ["claims"]
    if include_duplicate:
        _write_workflow_yaml(
            tmp_path / "workflows" / "claims_duplicate" / "typeflux.yaml",
            yaml_name="claims_duplicate",
            workflow_name="ClaimsDuplicateWorkflow",
            task_queue="claims-typeflux",
        )
        workflows += "  - id: claims_duplicate\n    directory: workflows/claims_duplicate\n"
        validation_workflows.append("claims_duplicate")

    if provider_type == "fake":
        provider_override = f"""provider:
                  type: fake
                  model: {provider_model}"""
    else:
        provider_secret_env = f"{provider_type.upper()}_API_KEY"
        provider_override = f"""provider:
                  type: {provider_type}
                  model: {provider_model}
                  api_key:
                    value_from:
                      env: {provider_secret_env}"""

    env_dir = tmp_path / "environments"
    env_dir.mkdir()
    (env_dir / "cloud.env").write_text(
        (
            "LANGFUSE_HOST=https://cloud.langfuse.example\n"
            "LANGFUSE_PUBLIC_KEY=pk-placeholder\n"
            "LANGFUSE_SECRET_KEY=sk-placeholder\n"
            f"{env_file_extra}"
        ),
        encoding="utf-8",
    )
    (env_dir / "cloud.yaml").write_text(
        _dedent(
            f"""
            version: "1"
            name: cloud
            env_files:
              - path: cloud.env
            variables:
              TYPEFLUX_ENVIRONMENT: temporal-cloud
              TYPEFLUX_DEPLOYMENT_ID: claims-prod
              TYPEFLUX_TEMPORAL_REGION: us-east
              OPENAI_API_KEY: should-not-be-configmap
            overrides:
              task_queue: claims-cloud-typeflux
              runtime:
                temporal:
                  address: cloud.tmprl:7233
                  namespace: claims-prod
                  tls:
                    server_root_ca_cert:
                      value_from:
                        file: {ca_mount_path}
                  api_key:
                    value_from:
                      env: TEMPORAL_API_KEY
{codec_yaml}
                observability:
                  type: langfuse
                {provider_override}
            """
            if temporal_tls
            else """
            version: "1"
            name: cloud
            variables:
              TYPEFLUX_TEMPORAL_REGION: us-east
            overrides:
              task_queue: claims-cloud-typeflux
              runtime:
                temporal:
                  address: cloud.tmprl:7233
                  namespace: claims-prod
                  tls: false
                  api_key: null
                observability:
                  type: langfuse
                provider:
                  type: openai
                  model: gpt-4o-mini
                  api_key:
                    value_from:
                      env: OPENAI_API_KEY
            """
        ),
        encoding="utf-8",
    )

    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "base.yaml").write_text(
        _dedent(
            f"""
            version: "1"
            name: base
            providers:
              allowed:
                {provider_type}:
                  models: [{provider_model}]
            observability:
              required: true
              allowed_backends: [langfuse]
            imports:
              allowed_module_roots: [demo_project]
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
            runtime:
              temporal:
                require_tls: true
                require_api_key: true
                allowed_regions: [us-east]
            """
        ),
        encoding="utf-8",
    )

    validation = ""
    if include_validation:
        workflows_yaml = ", ".join(validation_workflows)
        validation = (
            "validation:\n"
            "  targets:\n"
            "    cloud:\n"
            f"      workflows: [{workflows_yaml}]\n"
            "      environment: cloud\n"
            "      policies: [regulated]\n"
        )

    return _write_project_yaml(
        tmp_path,
        (
            'version: "1"\n'
            "name: claims-platform\n"
            "workflows:\n"
            f"{workflows}"
            "environments:\n"
            "  cloud: environments/cloud.yaml\n"
            "policies:\n"
            "  base: policies/base.yaml\n"
            "  regulated: policies/regulated.yaml\n"
            f"{validation}"
        ),
    )


def _write_workflow_yaml(
    path: Path,
    *,
    yaml_name: str,
    workflow_name: str,
    task_queue: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _dedent(
            f"""
            project: demo_project
            name: {yaml_name}
            task_queue: {task_queue}
            runtime:
              temporal:
                address: localhost:7233
              registry:
                type: inline
                prompts:
                  first: first {{{{value}}}}
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
              name: {workflow_name}
              input: schemas:InputModel
              output: schemas:OutputModel
              steps:
                - id: first
                  activity: first
            """
        ),
        encoding="utf-8",
    )


def _write_project_yaml(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "typeflux.project.yaml"
    path.write_text(_dedent(content), encoding="utf-8")
    return path


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


def _dedent(content: str) -> str:
    lines = content.strip("\n").splitlines()
    indentation = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
    return "\n".join(line[indentation:] for line in lines) + "\n"


# --- Deployment plan files + approval gate (#253) -----------------------


def _setup_plan_project(tmp_path):
    """Build a checked-out project that resolves with code provenance + policy."""
    import subprocess

    from typeflux.project import load_project_spec

    package = tmp_path / "demo_proj"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "schemas.py").write_text(
        "from pydantic import BaseModel\n"
        "class In(BaseModel):\n    value: str\n"
        "class Out(BaseModel):\n    value: str\n",
        encoding="utf-8",
    )
    (tmp_path / "workflow.yaml").write_text(
        "project: demo_proj\n"
        "name: demo\n"
        "task_queue: demo-queue\n"
        "runtime:\n"
        "  temporal:\n    address: localhost:7233\n"
        "  registry:\n    type: inline\n    prompts:\n      assess: assess {{value}}\n"
        "  provider:\n    type: fake\n    model: fake-model\n"
        "  observability:\n    type: none\n"
        "activities:\n  definitions:\n"
        "    - name: assess\n      input: schemas:In\n      output: schemas:Out\n      prompt: assess\n"
        "workflow:\n  name: DemoWorkflow\n  input: schemas:In\n  output: schemas:Out\n  steps:\n    - id: assess\n      activity: assess\n",
        encoding="utf-8",
    )
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "base.yaml").write_text(
        'version: "1"\nname: base\nproviders:\n  allowed:\n    fake:\n      models: [fake-model]\n',
        encoding="utf-8",
    )
    (tmp_path / "environments").mkdir()
    (tmp_path / "environments" / "local.yaml").write_text(
        'version: "1"\nname: local\n',
        encoding="utf-8",
    )
    manifest = tmp_path / "typeflux.project.yaml"
    manifest.write_text(
        'version: "1"\n'
        "name: demo-project\n"
        "workflows:\n  - id: workflow\n    path: workflow.yaml\n"
        "environments:\n  local: environments/local.yaml\n"
        "policies:\n  base: policies/base.yaml\n"
        "validation:\n  targets:\n    default:\n      workflows: [workflow]\n      environment: local\n      policies: [base]\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=tmp_path,
        check=True,
    )
    return load_project_spec(manifest)


def test_deployment_plan_round_trip_and_stable_hash(tmp_path, monkeypatch):
    import sys as _sys

    monkeypatch.syspath_prepend(str(tmp_path))
    if "demo_proj" in _sys.modules:
        del _sys.modules["demo_proj"]
    from typeflux.project import (
        list_deployment_plans,
        load_deployment_plan,
        verify_deployment_plan,
        write_deployment_plan,
    )

    project = _setup_plan_project(tmp_path)
    path, plan = write_deployment_plan(
        project,
        workflow_id="workflow",
        environment_id="local",
        image="example.com/worker:1@sha256:" + "a" * 64,
        policy_ids=("base",),
    )

    assert path.exists()
    reloaded = load_deployment_plan(project, path)
    assert reloaded.plan_hash == plan.plan_hash
    # Plan against the currently-resolved bundle: clean.
    verification = verify_deployment_plan(project, plan)
    assert verification.ok, [m.model_dump() for m in verification.mismatches]
    assert {p.plan_id for p in list_deployment_plans(project)} == {plan.plan_id}


def test_deployment_plan_base_env_writes_and_verifies_hermetically(tmp_path, monkeypatch):
    """#760 fix round item 3: the plan writer honors base_env end-to-end.

    A workflow whose spec interpolates ``${VAR}`` is planned under an injected base;
    the shell's value then CHANGES, and the plan still verifies under the same base
    (digest reproduced machine-independently) — while verifying WITHOUT the base
    reads the drifted shell and reports spec-digest drift, proving the seam (not
    accident) is what makes the artifact hermetic.
    """
    import sys as _sys

    monkeypatch.syspath_prepend(str(tmp_path))
    if "demo_proj" in _sys.modules:
        del _sys.modules["demo_proj"]
    from typeflux.project import verify_deployment_plan, write_deployment_plan

    project = _setup_plan_project(tmp_path)
    workflow_path = tmp_path / "workflow.yaml"
    # The workflow NAME joins the spec digest (task_queue does not), so an interpolated
    # name makes the digest provably env-dependent without the seam.
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace(
            "name: DemoWorkflow", "name: Demo${TF760_PLAN_SUFFIX}Workflow"
        ),
        encoding="utf-8",
    )
    base = {"TF760_PLAN_SUFFIX": "Hermetic"}
    monkeypatch.setenv("TF760_PLAN_SUFFIX", "ShellAtWrite")

    _, plan = write_deployment_plan(
        project,
        workflow_id="workflow",
        environment_id="local",
        image="example.com/worker:1@sha256:" + "c" * 64,
        policy_ids=("base",),
        base_env=base,
    )

    # Another machine, another shell: same base_env -> same digest -> clean verify.
    monkeypatch.setenv("TF760_PLAN_SUFFIX", "ShellAtPromote")
    verification = verify_deployment_plan(project, plan, base_env=base)
    assert verification.ok, [m.model_dump() for m in verification.mismatches]

    # Without the base the resolution reads the (different) shell: spec-digest drift —
    # the hermeticity came from the seam, not from the shell happening to match.
    ambient = verify_deployment_plan(project, plan)
    assert ambient.ok is False
    assert "identity.spec_digest" in {m.path for m in ambient.mismatches}


def test_deployment_plan_detects_spec_digest_drift(tmp_path, monkeypatch):
    import sys as _sys

    monkeypatch.syspath_prepend(str(tmp_path))
    if "demo_proj" in _sys.modules:
        del _sys.modules["demo_proj"]
    from typeflux.project import verify_deployment_plan, write_deployment_plan

    project = _setup_plan_project(tmp_path)
    _, plan = write_deployment_plan(
        project,
        workflow_id="workflow",
        environment_id="local",
        image="example.com/worker:1@sha256:" + "b" * 64,
        policy_ids=("base",),
    )
    # Forge drift: same shape, different spec_digest.
    drifted = plan.model_copy(
        update={"identity": plan.identity.model_copy(update={"spec_digest": "f" * 64})}
    )

    verification = verify_deployment_plan(project, drifted)
    assert verification.ok is False
    paths = {m.path for m in verification.mismatches}
    assert "identity.spec_digest" in paths


def test_deployment_plan_load_rejects_tampered_content(tmp_path, monkeypatch):
    """A post-approval edit with the stale hash left in place is TAMPERING (review item 1).

    Each of image / policy ids / spec_digest is edited in the YAML while ``plan_hash`` stays
    stale — load must recompute the canonical hash and reject, so ``--apply`` promotion can
    never consume tampered values.
    """
    import sys as _sys

    monkeypatch.syspath_prepend(str(tmp_path))
    if "demo_proj" in _sys.modules:
        del _sys.modules["demo_proj"]
    from typeflux.project import load_deployment_plan, write_deployment_plan
    from typeflux.project.deployment import ProjectDeploymentError

    project = _setup_plan_project(tmp_path)
    image = "example.com/worker:1@sha256:" + "e" * 64
    path, plan = write_deployment_plan(
        project,
        workflow_id="workflow",
        environment_id="local",
        image=image,
        policy_ids=("base",),
    )
    original = path.read_text(encoding="utf-8")
    tampers = (
        (f"image: {image}", "image: attacker.example/evil@sha256:" + "f" * 64),
        ("- base", "- weaker_policy"),
        (f"spec_digest: {plan.identity.spec_digest}", "spec_digest: " + "f0" * 32),
    )
    for old, new in tampers:
        assert old in original
        path.write_text(original.replace(old, new), encoding="utf-8")
        with pytest.raises(ProjectDeploymentError, match="integrity check"):
            load_deployment_plan(project, path)
    # The untampered file still loads clean (the recompute agrees with the stored hash).
    path.write_text(original, encoding="utf-8")
    assert load_deployment_plan(project, path).plan_hash == plan.plan_hash


def test_deployment_plan_load_rejects_unsafe_identity_ids(tmp_path, monkeypatch):
    """A CONSISTENTLY-rehashed plan with shell metacharacters in an identity id is rejected
    (review item 5): the charset gate is the injection guard the integrity check cannot be
    (an attacker can recompute the hash over their own edit)."""
    import sys as _sys

    import yaml as _yaml

    monkeypatch.syspath_prepend(str(tmp_path))
    if "demo_proj" in _sys.modules:
        del _sys.modules["demo_proj"]
    from typeflux.project import load_deployment_plan, write_deployment_plan
    from typeflux.project.deployment import ProjectDeploymentError
    from typeflux.project.deployments import _compute_plan_hash

    project = _setup_plan_project(tmp_path)
    path, plan = write_deployment_plan(
        project,
        workflow_id="workflow",
        environment_id="local",
        image="example.com/worker:1@sha256:" + "e" * 64,
        policy_ids=("base",),
    )
    payload = plan.model_dump(mode="json")
    payload["identity"]["workflow_id"] = "x; touch /tmp/pwn"
    payload["plan_hash"] = _compute_plan_hash(payload)
    evil = path.parent / "evil.yaml"
    evil.write_text(_yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    with pytest.raises(ProjectDeploymentError, match="unsafe identity.workflow_id"):
        load_deployment_plan(project, evil)


def test_deployment_plan_is_secret_free(tmp_path, monkeypatch):
    import json as _json
    import sys as _sys

    monkeypatch.syspath_prepend(str(tmp_path))
    if "demo_proj" in _sys.modules:
        del _sys.modules["demo_proj"]
    monkeypatch.setenv("SOME_SECRET", "shhh-do-not-leak")
    from typeflux.project import write_deployment_plan

    project = _setup_plan_project(tmp_path)
    _, plan = write_deployment_plan(
        project,
        workflow_id="workflow",
        environment_id="local",
        image="example.com/worker:1@sha256:" + "c" * 64,
        policy_ids=("base",),
    )

    serialized = _json.dumps(plan.model_dump(mode="json"))
    assert "shhh-do-not-leak" not in serialized
    # No prompt text leaks either: the inline template body must stay out.
    assert "assess {{value}}" not in serialized


# --- #253 Bugbot regressions: plan-out dir, plan-authoritative promote ---


_OTHER_DIGEST_IMAGE = f"ghcr.io/example/typeflux-worker@sha256:{'b' * 64}"


def test_project_cli_deploy_plan_out_writes_to_named_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project_path = _write_deployment_project(tmp_path)

    exit_code = project_cli.main(
        [
            "deploy",
            str(project_path),
            "--environment",
            "cloud",
            "--workflow",
            "claims",
            "--image",
            _DIGEST_IMAGE,
            "--plan-out",
            "plans",
            "--json",
        ]
    )

    assert exit_code == 0
    # The named directory is honored, not the fixed deployments/ default.
    written = list((tmp_path / "plans").glob("*.yaml"))
    assert len(written) == 1
    assert not (tmp_path / "deployments").exists()
    assert capsys.readouterr().err == ""


def test_project_cli_deploy_plan_promotes_from_plan_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An approved plan is authoritative: even when `--image` on the promote
    # call differs, the emitted artifact must use the plan's pinned image.
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project_path = _write_deployment_project(tmp_path)

    assert (
        project_cli.main(
            [
                "deploy",
                str(project_path),
                "--environment",
                "cloud",
                "--workflow",
                "claims",
                "--image",
                _DIGEST_IMAGE,
                "--plan-out",
                "plans",
            ]
        )
        == 0
    )
    capsys.readouterr()
    plan_file = next((tmp_path / "plans").glob("*.yaml"))

    exit_code = project_cli.main(
        [
            "deploy",
            str(project_path),
            "--environment",
            "cloud",
            "--workflow",
            "claims",
            "--image",
            _OTHER_DIGEST_IMAGE,  # deliberately not the plan's image
            "--apply",
            str(plan_file),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["image"] == _DIGEST_IMAGE
    assert payload["image"] != _OTHER_DIGEST_IMAGE


def test_list_deployment_plans_skips_malformed_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys as _sys

    monkeypatch.syspath_prepend(str(tmp_path))
    if "demo_proj" in _sys.modules:
        del _sys.modules["demo_proj"]
    from typeflux.project import list_deployment_plans, write_deployment_plan

    project = _setup_plan_project(tmp_path)
    _, plan = write_deployment_plan(
        project,
        workflow_id="workflow",
        environment_id="local",
        image="example.com/worker:1@sha256:" + "e" * 64,
        policy_ids=("base",),
    )
    # A malformed YAML alongside a valid plan must not break the listing.
    (project.manifest_path.parent / "deployments" / "broken.yaml").write_text(
        "plan_version: '1'\nidentity: not-a-mapping\n", encoding="utf-8"
    )
    # The strict loader raises on DUPLICATE KEYS where bare safe_load did not (#602) —
    # that failure mode must also be skipped, not break the whole listing.
    (project.manifest_path.parent / "deployments" / "dupes.yaml").write_text(
        "plan_version: '1'\nplan_version: '2'\n", encoding="utf-8"
    )

    listed = list_deployment_plans(project)
    assert [p.plan_id for p in listed] == [plan.plan_id]


def test_project_cli_deploy_plan_promotes_without_env_or_image_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The plan is authoritative, so promoting needs only the manifest and the
    # plan path — no --environment/--image (which are required otherwise).
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project_path = _write_deployment_project(tmp_path)

    assert (
        project_cli.main(
            [
                "deploy",
                str(project_path),
                "--environment",
                "cloud",
                "--workflow",
                "claims",
                "--image",
                _DIGEST_IMAGE,
                "--plan-out",
                "plans",
            ]
        )
        == 0
    )
    capsys.readouterr()
    plan_file = next((tmp_path / "plans").glob("*.yaml"))

    exit_code = project_cli.main(["deploy", str(project_path), "--apply", str(plan_file), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["image"] == _DIGEST_IMAGE
    assert payload["environment_id"] == "cloud"


def test_project_cli_deploy_requires_env_and_image_without_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project_path = _write_deployment_project(tmp_path)

    exit_code = project_cli.main(["deploy", str(project_path), "--workflow", "claims"])

    assert exit_code == 2
    assert "--environment" in capsys.readouterr().err


def test_project_cli_deploy_plan_resolves_relative_to_manifest_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The console's copyable promote command uses a manifest-relative plan
    # path (deployments/<id>.yaml). Promoting must work from any cwd, not just
    # the project root.
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project_path = _write_deployment_project(tmp_path)

    assert (
        project_cli.main(
            [
                "deploy",
                str(project_path),
                "--environment",
                "cloud",
                "--workflow",
                "claims",
                "--image",
                _DIGEST_IMAGE,
                "--plan-out",
                "deployments",
            ]
        )
        == 0
    )
    capsys.readouterr()
    plan_file = next((tmp_path / "deployments").glob("*.yaml"))

    # Run from a directory that is NOT the project root, with a relative plan.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    exit_code = project_cli.main(
        [
            "deploy",
            str(project_path),
            "--apply",
            f"deployments/{plan_file.name}",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["image"] == _DIGEST_IMAGE


# --- Transitive-closure admission at the deploy gate (#55 slice 5) -----------


def _write_closure_deployment_project(tmp_path: Path, *, child_has_review: bool) -> Path:
    """A parent (WITH a review gate) that references a child sub-workflow; the
    selected policy requires review routes, so the deploy plan is admissible only
    when the child declares a review too (`child_has_review`)."""
    _write_demo_project_package(tmp_path)
    (tmp_path / "parent.yaml").write_text(
        _dedent(
            """
            project: demo_project
            name: parent
            task_queue: closure-deploy-typeflux
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
              observability:
                type: none
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
              name: ClosureParentWorkflow
              input: schemas:InputModel
              output: schemas:OutputModel
              lifecycle:
                enabled: true
                review:
                  after_step: first
                  user_decisions:
                    proceed:
                      route: assess_child
              steps:
                - id: first
                  activity: first
                - id: assess_child
                  workflow: child
                - id: second
                  activity: second
            """
        ),
        encoding="utf-8",
    )
    child_lifecycle = (
        """
              lifecycle:
                enabled: true
                review:
                  after_step: only
                  user_decisions:
                    done:
                      route: tail
        """
        if child_has_review
        else ""
    )
    child_steps = (
        """
              steps:
                - id: only
                  activity: first
                - id: tail
                  activity: second
        """
        if child_has_review
        else """
              steps:
                - id: only
                  activity: first
        """
    )
    (tmp_path / "child.yaml").write_text(
        _dedent(
            f"""
            project: demo_project
            name: child
            task_queue: closure-deploy-typeflux
            runtime:
              temporal:
                address: localhost:7233
              registry:
                type: inline
                prompts:
                  first: first {{{{value}}}}
                  second: second {{{{value}}}}
              provider:
                type: fake
              observability:
                type: none
            activities:
              definitions:
                - name: first
                  input: schemas:OutputModel
                  output: schemas:OutputModel
                  prompt: first
                - name: second
                  input: schemas:OutputModel
                  output: schemas:OutputModel
                  prompt: second
            workflow:
              name: ClosureChildWorkflow
              input: schemas:OutputModel
              output: schemas:OutputModel
{child_lifecycle}{child_steps}
            """
        ),
        encoding="utf-8",
    )
    env_dir = tmp_path / "environments"
    env_dir.mkdir()
    (env_dir / "cloud.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: cloud
            """
        ),
        encoding="utf-8",
    )
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "reviewed.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: reviewed
            providers:
              allowed:
                fake: {}
            review:
              require_review_routes: true
            """
        ),
        encoding="utf-8",
    )
    return _write_project_yaml(
        tmp_path,
        (
            'version: "1"\n'
            "name: closure-deploy\n"
            "workflows:\n"
            "  - id: parent\n"
            "    path: parent.yaml\n"
            "  - id: child\n"
            "    path: child.yaml\n"
            "environments:\n"
            "  cloud: environments/cloud.yaml\n"
            "policies:\n"
            "  reviewed: policies/reviewed.yaml\n"
            "validation:\n"
            "  targets:\n"
            "    cloud:\n"
            "      workflows: [parent]\n"
            "      environment: cloud\n"
            "      policies: [reviewed]\n"
        ),
    )


def test_project_deployment_plan_rejects_noncompliant_subworkflow_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The PARENT satisfies require_review_routes, but its referenced CHILD declares no
    # review gate — deploy admission must reject the composed program transitively
    # (#55 §9): the deploy gate is an admission entry point like validate and the
    # runtime guard.
    monkeypatch.syspath_prepend(str(tmp_path))
    project = load_project_spec(_write_closure_deployment_project(tmp_path, child_has_review=False))

    with pytest.raises(ProjectDeploymentError, match="policy_subworkflow_closure") as excinfo:
        build_project_deployment_plan(
            project,
            environment_id="cloud",
            workflow_ids=("parent",),
            image=_DIGEST_IMAGE,
            allow_mutable_image=True,
        )
    assert "sub-workflow 'child'" in str(excinfo.value)
    assert "policy_review" in str(excinfo.value)


def test_project_deployment_plan_admits_compliant_subworkflow_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The compliant twin: the child declares its own review gate, so the whole
    # composed program admits and the plan builds.
    monkeypatch.syspath_prepend(str(tmp_path))
    project = load_project_spec(_write_closure_deployment_project(tmp_path, child_has_review=True))

    plan = build_project_deployment_plan(
        project,
        environment_id="cloud",
        workflow_ids=("parent",),
        image=_DIGEST_IMAGE,
        allow_mutable_image=True,
    )
    assert [worker.workflow_id for worker in plan.workers] == ["parent"]


def test_verify_plan_merged_to_default_branch_matrix(tmp_path: Path) -> None:
    # #790: the merged-plan gate's four verdicts, against a real git repo with a bare
    # origin whose HEAD ref is set (what a normal clone has).
    import os
    import subprocess

    from typeflux.project.deployments import verify_plan_merged_to_default_branch

    def git(cwd: Path, *args: str) -> None:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=env)

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    git(repo, "remote", "add", "origin", str(origin))
    plan = repo / "deployments" / "p.yaml"
    plan.parent.mkdir()
    plan.write_text("plan: merged\n", encoding="utf-8")
    git(repo, "add", "deployments/p.yaml")
    git(repo, "commit", "-m", "plan")
    git(repo, "push", "-u", "origin", "main")
    git(repo, "remote", "set-head", "origin", "--auto")

    # Merged, byte-identical -> None.
    assert verify_plan_merged_to_default_branch(plan, project_root=repo) is None
    # Local edit diverges from the merged artifact -> refusal naming the branch.
    plan.write_text("plan: tampered\n", encoding="utf-8")
    gap = verify_plan_merged_to_default_branch(plan, project_root=repo)
    assert gap is not None and "differs from the version merged" in gap
    # A never-merged plan -> refusal.
    unmerged = repo / "deployments" / "new.yaml"
    unmerged.write_text("plan: new\n", encoding="utf-8")
    gap = verify_plan_merged_to_default_branch(unmerged, project_root=repo)
    assert gap is not None and "has not been merged" in gap
    # A symlinked plan is rejected outright (it would verify another repo's artifact).
    link = repo / "deployments" / "link.yaml"
    link.symlink_to(plan)
    gap = verify_plan_merged_to_default_branch(link, project_root=repo)
    assert gap is not None and "symlink" in gap
    # An in-repo symlink back to the ROOT (ln -s . alias) is refused.
    root_alias = repo / "rootalias"
    root_alias.symlink_to(repo)
    gap = verify_plan_merged_to_default_branch(
        root_alias / "deployments" / "p.yaml", project_root=repo
    )
    assert gap is not None and "path component" in gap
    # A NESTED project manifest (monorepo layout) verifies correctly: pathspecs must
    # be anchored to the repo toplevel, not the manifest dir (Bugbot on #845).
    nested_root = repo / "apps" / "svc"
    (nested_root / "deployments").mkdir(parents=True)
    nested_plan = nested_root / "deployments" / "n.yaml"
    nested_plan.write_text("plan: nested\n", encoding="utf-8")
    git(repo, "add", "apps/svc/deployments/n.yaml")
    git(repo, "commit", "-m", "nested plan")
    git(repo, "push", "origin", "main")
    assert verify_plan_merged_to_default_branch(nested_plan, project_root=nested_root) is None
    # A git content filter on the plan path refuses (filter laundering, LFS-style).
    (repo / ".gitattributes").write_text("deployments/p.yaml filter=lfsish\n", encoding="utf-8")
    plan.write_text("plan: merged\n", encoding="utf-8")  # restore merged bytes
    gap = verify_plan_merged_to_default_branch(plan, project_root=repo)
    assert gap is not None and "content filter" in gap
    (repo / ".gitattributes").unlink()
    # A plan MERGED as a symlink is refused even when a local regular file matches.
    (repo / "target.yaml").write_text("plan: merged\n", encoding="utf-8")
    sym_plan = repo / "deployments" / "sym.yaml"
    sym_plan.symlink_to(repo / "target.yaml")
    git(repo, "add", "deployments/sym.yaml", "target.yaml")
    git(repo, "commit", "-m", "symlinked plan")
    git(repo, "push", "origin", "main")
    sym_plan.unlink()
    sym_plan.write_text("plan: merged\n", encoding="utf-8")
    gap = verify_plan_merged_to_default_branch(sym_plan, project_root=repo)
    assert gap is not None and "not a regular file" in gap
    # A symlinked DIRECTORY component (deployments-alias) is refused.
    (repo / "approved").mkdir()
    (repo / "approved" / "a.yaml").write_text("plan: merged\n", encoding="utf-8")
    git(repo, "add", "approved/a.yaml")
    git(repo, "commit", "-m", "approved")
    git(repo, "push", "origin", "main")
    alias = repo / "alias"
    alias.symlink_to(repo / "approved")
    gap = verify_plan_merged_to_default_branch(alias / "a.yaml", project_root=repo)
    assert gap is not None and "path component" in gap
    # A plan resolving outside the project's checkout is refused (escape guard).
    loose = tmp_path / "loose.yaml"
    loose.write_text("plan: loose\n", encoding="utf-8")
    gap = verify_plan_merged_to_default_branch(loose, project_root=repo)
    assert gap is not None and "outside the project's git checkout" in gap
    # A project manifest outside any git checkout -> clone remediation.
    nogit = tmp_path / "nogit"
    nogit.mkdir()
    (nogit / "p.yaml").write_text("plan: x\n", encoding="utf-8")
    gap = verify_plan_merged_to_default_branch(nogit / "p.yaml", project_root=nogit)
    assert gap is not None and "not inside a git checkout" in gap


def test_secret_references_include_custom_extension_config_sources(tmp_path: Path) -> None:
    """#792: a custom extension's declared config references must reach the generated
    deployment's Secret/env scaffolding — a required source the bundle inventories but the
    manifest never injects would crash the worker at startup. Literal config entries live
    in the spec itself and need no injection."""
    from typeflux.project.deployment import _secret_references
    from typeflux.yaml.loader import load_yaml_spec

    spec_path = tmp_path / "typeflux.yaml"
    spec_path.write_text(
        _dedent(
            """
            project: demo
            name: custom-config
            task_queue: q
            runtime:
              temporal:
                address: localhost:7233
              registry:
                type: inline
                prompts:
                  first: first {{value}}
              provider:
                type: custom
                class: demo.providers:AcmeProvider
                config:
                  endpoint: https://acme.internal
                  api_key:
                    value_from:
                      env: ACME_TOKEN
                  ca_bundle:
                    value_from:
                      file: /etc/typeflux/acme/ca.pem
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
        ),
        encoding="utf-8",
    )
    spec = load_yaml_spec(spec_path, load_dotenv=False)

    env_refs, file_refs = _secret_references(spec, secret_name="worker-secrets")

    env_by_path = {ref.runtime_path: ref for ref in env_refs}
    assert "runtime.provider.config[api_key]" in env_by_path
    assert env_by_path["runtime.provider.config[api_key]"].env_name == "ACME_TOKEN"
    file_by_path = {ref.runtime_path: ref for ref in file_refs}
    assert "runtime.provider.config[ca_bundle]" in file_by_path
    # The literal endpoint needs no injection and must not be scaffolded (or rejected).
    assert "runtime.provider.config[endpoint]" not in env_by_path
    assert "runtime.provider.config[endpoint]" not in file_by_path


def test_secret_references_skip_optional_file_config_sources(tmp_path: Path) -> None:
    """An optional (required: false) file-sourced config entry is NOT scaffolded: the secret
    volume's items list has no optional handling, so a listed-but-absent key would block the
    mount, while the runtime resolver simply omits the entry (codex on #792)."""
    from typeflux.project.deployment import _secret_references
    from typeflux.yaml.loader import load_yaml_spec

    spec_path = tmp_path / "typeflux.yaml"
    spec_path.write_text(
        _dedent(
            """
            project: demo
            name: custom-config
            task_queue: q
            runtime:
              temporal:
                address: localhost:7233
              registry:
                type: inline
                prompts:
                  first: first {{value}}
              provider:
                type: custom
                class: demo.providers:AcmeProvider
                config:
                  extra_ca:
                    value_from:
                      file: /etc/typeflux/acme/extra-ca.pem
                      required: false
                  api_key:
                    value_from:
                      env: ACME_TOKEN
                      required: false
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
        ),
        encoding="utf-8",
    )
    spec = load_yaml_spec(spec_path, load_dotenv=False)

    env_refs, file_refs = _secret_references(spec, secret_name="worker-secrets")

    # The optional file entry stays un-scaffolded; the optional env entry renders with
    # optional: true via the secretKeyRef `required` flag, so it IS scaffolded.
    assert "runtime.provider.config[extra_ca]" not in {ref.runtime_path for ref in file_refs}
    env_ref = {ref.runtime_path: ref for ref in env_refs}["runtime.provider.config[api_key]"]
    assert env_ref.required is False


def test_secret_references_include_declared_observability_credentials(tmp_path: Path) -> None:
    """#793: spec-declared observability credentials are typed slots in the deployment
    scaffolding; the standard-name fallback scaffold skips env names the typed refs claim,
    so declared and fallback surfaces never duplicate."""
    from typeflux.project.deployment import _secret_references
    from typeflux.yaml.loader import load_yaml_spec

    spec_path = tmp_path / "typeflux.yaml"
    spec_path.write_text(
        _dedent(
            """
            project: demo
            name: obs-creds
            task_queue: q
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
                  public_key:
                    value_from:
                      env: LANGFUSE_PUBLIC_KEY
                  secret_key:
                    value_from:
                      env: TEAM_LF_SECRET
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
    spec = load_yaml_spec(spec_path, load_dotenv=False)

    env_refs, _file_refs = _secret_references(spec, secret_name="worker-secrets")

    by_path = {ref.runtime_path: ref for ref in env_refs}
    assert by_path["runtime.observability.langfuse.public_key"].env_name == "LANGFUSE_PUBLIC_KEY"
    assert by_path["runtime.observability.langfuse.secret_key"].env_name == "TEAM_LF_SECRET"


def test_declared_observability_credentials_claim_names_from_fallback_scaffold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#793: a typed spec-declared credential ref claims its env name, and the standard-name
    fallback scaffold skips it — one ref per name, the declared one (with its real
    runtime_path), plus the fallback name for the still-undeclared field."""
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path = _write_observability_project(
        tmp_path, backend="langfuse", require_observability=True
    )
    env_file = tmp_path / "environments" / "prod.yaml"
    env_file.write_text(
        _dedent(
            """
            version: "1"
            name: prod
            variables:
              TYPEFLUX_ENVIRONMENT: prod
            overrides:
              runtime:
                observability:
                  type: langfuse
                  langfuse:
                    secret_key:
                      value_from:
                        env: LANGFUSE_SECRET_KEY
            """
        ),
        encoding="utf-8",
    )
    project = load_project_spec(project_path)
    plan = build_project_deployment_plan(
        project,
        environment_id="prod",
        workflow_ids=("review",),
        policy_ids=("base",),
        image=_DIGEST_IMAGE,
    )
    worker = plan.workers[0]
    refs = [ref for ref in worker.secret_env if ref.env_name.startswith("LANGFUSE_")]
    by_name = {ref.env_name: ref for ref in refs}
    assert len(refs) == 2  # no duplicate for LANGFUSE_SECRET_KEY
    assert (
        by_name["LANGFUSE_SECRET_KEY"].runtime_path == "runtime.observability.langfuse.secret_key"
    )
    assert (
        by_name["LANGFUSE_PUBLIC_KEY"].runtime_path == "runtime.observability.langfuse.public_key"
    )


def test_custom_named_declared_credential_suppresses_standard_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The finder-reproduced #793 regression: a credential declared under a CUSTOM env name
    claims its canonical slot, so the standard-name fallback must NOT be scaffolded — a
    required secretKeyRef for a key nothing populates fails the pod at rollout."""
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path = _write_observability_project(
        tmp_path, backend="langfuse", require_observability=True
    )
    (tmp_path / "environments" / "prod.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: prod
            variables:
              TYPEFLUX_ENVIRONMENT: prod
            overrides:
              runtime:
                observability:
                  type: langfuse
                  langfuse:
                    public_key:
                      value_from:
                        env: MYTEAM_LANGFUSE_PUBLIC
                    secret_key:
                      value_from:
                        env: MYTEAM_LANGFUSE_SECRET
            """
        ),
        encoding="utf-8",
    )
    plan = build_project_deployment_plan(
        load_project_spec(project_path),
        environment_id="prod",
        workflow_ids=("review",),
        policy_ids=("base",),
        image=_DIGEST_IMAGE,
    )
    worker = plan.workers[0]
    obs_refs = [
        ref for ref in worker.secret_env if ref.runtime_path.startswith("runtime.observability.")
    ]
    assert sorted(ref.env_name for ref in obs_refs) == [
        "MYTEAM_LANGFUSE_PUBLIC",
        "MYTEAM_LANGFUSE_SECRET",
    ]  # no bogus LANGFUSE_* fallback refs


def test_observability_fallback_slot_paths_stay_inside_the_shared_inventory() -> None:
    """The fallback map's values must be REAL SECRET_SLOT_PATHS entries — a divergence
    would silently reintroduce synthesized pseudo-paths in generated manifests."""
    from typeflux.project.deployment import _OBSERVABILITY_FALLBACK_SLOT_PATHS
    from typeflux.yaml.secrets import SECRET_SLOT_PATHS

    assert set(_OBSERVABILITY_FALLBACK_SLOT_PATHS.values()) <= set(SECRET_SLOT_PATHS)


def test_empty_interpolated_credential_does_not_suppress_the_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex round 3: `public_key: ${LF_PUBLIC:-}` interpolating to "" resolves to unset at
    runtime, so it must not claim the slot — the standard fallback ref stays scaffolded."""
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path = _write_observability_project(
        tmp_path, backend="langfuse", require_observability=True
    )
    (tmp_path / "environments" / "prod.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: prod
            variables:
              TYPEFLUX_ENVIRONMENT: prod
            overrides:
              runtime:
                observability:
                  type: langfuse
                  langfuse:
                    public_key: ""
                    secret_key:
                      value_from:
                        env: MYTEAM_LANGFUSE_SECRET
            """
        ),
        encoding="utf-8",
    )
    plan = build_project_deployment_plan(
        load_project_spec(project_path),
        environment_id="prod",
        workflow_ids=("review",),
        policy_ids=("base",),
        image=_DIGEST_IMAGE,
    )
    worker = plan.workers[0]
    names = sorted(
        ref.env_name
        for ref in worker.secret_env
        if ref.runtime_path.startswith("runtime.observability.")
    )
    # secret_key declared (claims its slot); public_key "" → the standard fallback stays.
    assert names == ["LANGFUSE_PUBLIC_KEY", "MYTEAM_LANGFUSE_SECRET"]


def test_optional_declared_credential_keeps_the_standard_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex round 4: a required: false declared reference falls back to the standard env
    var at runtime when its source is absent — both refs stay scaffolded."""
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path = _write_observability_project(
        tmp_path, backend="langfuse", require_observability=False
    )
    (tmp_path / "environments" / "prod.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: prod
            variables:
              TYPEFLUX_ENVIRONMENT: prod
            overrides:
              runtime:
                observability:
                  type: langfuse
                  langfuse:
                    public_key:
                      value_from:
                        env: TEAM_LF_PUBLIC
                        required: false
            """
        ),
        encoding="utf-8",
    )
    plan = build_project_deployment_plan(
        load_project_spec(project_path),
        environment_id="prod",
        workflow_ids=("review",),
        policy_ids=("base",),
        image=_DIGEST_IMAGE,
    )
    worker = plan.workers[0]
    names = sorted(
        ref.env_name
        for ref in worker.secret_env
        if ref.runtime_path.startswith("runtime.observability.")
    )
    # Declared-optional ref scaffolded AND the standard fallback preserved.
    assert names == ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "TEAM_LF_PUBLIC"]


def test_optional_declared_credential_demotes_the_fallback_to_optional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex round 5: under a required-observability policy the preserved fallback for an
    optionally-declared slot must be OPTIONAL — runtime can satisfy the slot via either
    source, so a hard secretKeyRef on the standard name would block a pod whose custom
    var is the populated one."""
    monkeypatch.syspath_prepend(str(tmp_path))
    project_path = _write_observability_project(
        tmp_path, backend="langfuse", require_observability=True
    )
    (tmp_path / "environments" / "prod.yaml").write_text(
        _dedent(
            """
            version: "1"
            name: prod
            variables:
              TYPEFLUX_ENVIRONMENT: prod
            overrides:
              runtime:
                observability:
                  type: langfuse
                  langfuse:
                    public_key:
                      value_from:
                        env: TEAM_LF_PUBLIC
                        required: false
            """
        ),
        encoding="utf-8",
    )
    plan = build_project_deployment_plan(
        load_project_spec(project_path),
        environment_id="prod",
        workflow_ids=("review",),
        policy_ids=("base",),
        image=_DIGEST_IMAGE,
    )
    worker = plan.workers[0]
    by_name = {
        ref.env_name: ref
        for ref in worker.secret_env
        if ref.runtime_path.startswith("runtime.observability.")
    }
    assert by_name["LANGFUSE_PUBLIC_KEY"].required is False  # demoted: either source works
    assert by_name["LANGFUSE_SECRET_KEY"].required is True  # undeclared slot: policy-hard


def test_optional_file_observability_credential_not_scaffolded(tmp_path: Path) -> None:
    """codex round 6: an optional FILE-sourced observability credential is not scaffolded —
    the secret volume's items list has no optional handling, and runtime falls back to env."""
    from typeflux.project.deployment import _secret_references
    from typeflux.yaml.loader import load_yaml_spec

    spec_path = tmp_path / "typeflux.yaml"
    spec_path.write_text(
        _dedent(
            """
            project: demo
            name: obs-optional-file
            task_queue: q
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
                  public_key:
                    value_from:
                      file: /etc/typeflux/langfuse/public.key
                      required: false
                  secret_key:
                    value_from:
                      env: TEAM_LF_SECRET
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
    spec = load_yaml_spec(spec_path, load_dotenv=False)

    env_refs, file_refs = _secret_references(spec, secret_name="worker-secrets")

    assert "runtime.observability.langfuse.public_key" not in {
        ref.runtime_path for ref in file_refs
    }
    assert "runtime.observability.langfuse.secret_key" in {ref.runtime_path for ref in env_refs}


def test_deploy_base_env_file_makes_plan_bytes_machine_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#798: --base-env-file wires the #760 hermetic seam — the same file yields the same
    plan bytes regardless of the process environment; --hermetic is the empty-base form
    and the two flags are mutually exclusive."""

    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project_path = _write_deployment_project(tmp_path)
    base_env = tmp_path / "plan.env"
    base_env.write_text("PLAN_ONLY_VAR=stable-value\n", encoding="utf-8")
    out_one = tmp_path / "plans-one"
    out_two = tmp_path / "plans-two"

    def _deploy(out_dir) -> None:
        exit_code = project_cli.main(
            [
                "deploy",
                str(project_path),
                "--environment",
                "cloud",
                "--workflow",
                "claims",
                "--image",
                _DIGEST_IMAGE,
                "--base-env-file",
                str(base_env),
                "--plan-out",
                str(out_dir),
            ]
        )
        assert exit_code == 0, capsys.readouterr().err

    monkeypatch.setenv("PLAN_ONLY_VAR", "machine-a-noise")
    _deploy(out_one)
    monkeypatch.setenv("PLAN_ONLY_VAR", "machine-b-noise")
    monkeypatch.setenv("SOME_OTHER_HOST_VAR", "even-more-noise")
    _deploy(out_two)
    capsys.readouterr()

    plans_one = sorted(out_one.rglob("*.yaml")) + sorted(out_one.rglob("*.json"))
    plans_two = sorted(out_two.rglob("*.yaml")) + sorted(out_two.rglob("*.json"))
    assert plans_one, "no plan files written"

    def _stable(path):
        # generated_at is run-provenance and excluded from plan_hash; everything else —
        # hash and filename included — must be byte-identical across machines.
        return [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.startswith("generated_at:")
        ]

    for a, b in zip(plans_one, plans_two, strict=True):
        assert a.name == b.name  # same composition ⇒ same plan_hash ⇒ same filename
        assert _stable(a) == _stable(b), f"plan content differs: {a.name}"

    # The flags are mutually exclusive — a USAGE error under the #818 contract (exit 2).
    with pytest.raises(SystemExit) as excinfo:
        project_cli.main(
            [
                "deploy",
                str(project_path),
                "--environment",
                "cloud",
                "--workflow",
                "claims",
                "--image",
                _DIGEST_IMAGE,
                "--base-env-file",
                str(base_env),
                "--hermetic",
            ]
        )
    assert excinfo.value.code == 2

    # A typo'd path must fail CLOSED (usage error), never silently behave like --hermetic.
    with pytest.raises(SystemExit) as missing:
        project_cli.main(
            [
                "deploy",
                str(project_path),
                "--environment",
                "cloud",
                "--workflow",
                "claims",
                "--image",
                _DIGEST_IMAGE,
                "--base-env-file",
                str(tmp_path / "no-such.env"),
            ]
        )
    assert missing.value.code == 2


def test_console_script_entry_points_resolve() -> None:
    """#810: the [project.scripts] targets must stay importable callables."""
    import importlib

    for target in (
        "typeflux.project.__main__:main",
        "typeflux.controlplane.__main__:main",
        "typeflux.observability.__main__:main",
    ):
        module_name, _, attr = target.partition(":")
        assert callable(getattr(importlib.import_module(module_name), attr))


def test_deploy_apply_verifies_under_the_same_base_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """codex: promote must verify under the SAME hermetic base the plan was authored
    with — a valid plan must not be rejected on an env-divergent promote machine."""
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_demo_project_package(tmp_path)
    project_path = _write_deployment_project(tmp_path)
    base_env = tmp_path / "plan.env"
    base_env.write_text("PLAN_ONLY_VAR=stable-value\n", encoding="utf-8")
    out_dir = tmp_path / "plans"

    exit_code = project_cli.main(
        [
            "deploy",
            str(project_path),
            "--environment",
            "cloud",
            "--workflow",
            "claims",
            "--image",
            _DIGEST_IMAGE,
            "--base-env-file",
            str(base_env),
            "--plan-out",
            str(out_dir),
        ]
    )
    assert exit_code == 0, capsys.readouterr().err
    capsys.readouterr()
    plan_file = sorted(out_dir.rglob("*.yaml"))[0]

    monkeypatch.setenv("PROMOTE_MACHINE_NOISE", "different-shell")
    exit_code = project_cli.main(
        [
            "deploy",
            str(project_path),
            "--apply",
            str(plan_file),
            "--base-env-file",
            str(base_env),
        ]
    )
    output = capsys.readouterr()
    assert exit_code == 0, output.err
