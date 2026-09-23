from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from typeflux.core.errors import TypefluxError
from typeflux.project.environment import (
    ProjectResolvedWorkflow,
    project_environment_context,
    resolve_project_workflow,
)
from typeflux.project.loader import validate_project
from typeflux.project.policy import ComposedProjectPolicy, compose_project_policies
from typeflux.project.policy_enforcement import (
    _iter_subworkflow_closure,
    risk_tier_enforcement_gap,
    select_project_policy_ids_for_workflow,
    validate_project_policy,
    validate_subworkflow_closure_policy,
)
from typeflux.project.spec import ProjectValidationCheck, TypefluxProjectSpec
from typeflux.yaml.secrets import SecretValueSpec, custom_extension_config_sources
from typeflux.yaml.spec import TemporalTLSConfigSpec, TypefluxYamlSpec

# Matched against the substring after the image reference's last "@" so the
# check stays linear-time on attacker-shaped input (the image now arrives via
# the control-plane HTTP API, not just the CLI).
_SHA256_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-fA-F]{64}")
# The well-known all-zeros digest (#757 item 5). It is FORMAT-valid (sha256: + 64 hex), so the
# digest-pin check passes — but it resolves to no published image and dies at pod scheduling
# (ImagePullBackOff), AFTER the plan/PR/promote approval chain has rubber-stamped it. Rejected at
# plan-write AND promote/verify time unless the placeholder is explicitly opted in.
PLACEHOLDER_IMAGE_DIGEST = "sha256:" + "0" * 64


def is_placeholder_image_digest(image: str) -> bool:
    """Whether an image reference pins the all-zeros placeholder digest (#757 item 5)."""
    _, separator, digest = image.rpartition("@")
    return bool(separator) and digest.lower() == PLACEHOLDER_IMAGE_DIGEST


# Anchor secret indicators to the end of the name so operational variables that
# merely contain a secret-like token (e.g. API_KEY_ROTATION_DAYS, TOKEN_BUCKET_SIZE)
# are not mis-routed to Secret references. Genuine secrets that do not end in a
# recognized indicator are not silently dropped: they fall through to the
# unclassified-variable hard error, or can be declared via value_from / --config-env.
_SECRET_LIKE_ENV_PATTERN = re.compile(
    r"(?:^|_)(?:API_?KEY|PUBLIC_?KEY|PRIVATE_?KEY|CLIENT_?KEY|SECRET(?:_?KEY)?|TOKEN|PASSWORD|CERT|KEY)$",
    re.IGNORECASE,
)
# The credential env NAMES a rendered worker needs for each observability backend (#757 item 3).
# NAMES only, per the Secret-scaffold convention (the renderer never emits a value). A ``custom``
# transport carries no platform-known credentials, so it gets no scaffold. Mirrors what the langfuse
# / langsmith observers read from the environment at runtime.
_OBSERVABILITY_FALLBACK_SLOT_PATHS: dict[str, str] = {
    # The env-fallback scaffold's runtime_path is the CANONICAL spec slot (#793) — the same
    # path a declared runtime.observability.<backend>.* reference carries, so the two
    # surfaces name one slot, not a synthesized pseudo-path.
    "LANGFUSE_PUBLIC_KEY": "runtime.observability.langfuse.public_key",
    "LANGFUSE_SECRET_KEY": "runtime.observability.langfuse.secret_key",
    "LANGSMITH_API_KEY": "runtime.observability.langsmith.api_key",
}

_OBSERVABILITY_BACKEND_SECRET_ENV: dict[str, tuple[str, ...]] = {
    "langfuse": ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"),
    "langsmith": ("LANGSMITH_API_KEY",),
}
_SAFE_CONFIG_ENV_NAMES = {
    "LANGFUSE_BASE_URL",
    "LANGFUSE_HOST",
    "LANGFUSE_PROMPT_LABEL",
    "TEMPORAL_ADDRESS",
    "TEMPORAL_NAMESPACE",
    "TEMPORAL_TASK_QUEUE",
    "TEMPORAL_TLS",
    "TYPEFLUX_ANTHROPIC_MODEL",
    "TYPEFLUX_DEPLOYMENT_ID",
    "TYPEFLUX_ENVIRONMENT",
    "TYPEFLUX_OPENAI_MODEL",
    "TYPEFLUX_TEMPORAL_REGION",
}
# ConfigMap model entries exist so in-container YAML interpolation reproduces the
# planned resolution; each provider's YAML reads its own key. The fake provider
# has no interpolation convention, so it gets no model entry.
_PROVIDER_MODEL_ENV_NAMES = {
    "openai": "TYPEFLUX_OPENAI_MODEL",
    "anthropic": "TYPEFLUX_ANTHROPIC_MODEL",
}
# The generated worker command runs preflight, writes this marker, then execs the
# worker, so probes gate on the same preflight that protects the reference image.
# /tmp is the pod's writable emptyDir under the read-only root filesystem.
_PREFLIGHT_MARKER_PATH = "/tmp/typeflux-preflight-ok"
_SECRET_NAME_KEY_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")
_RFC1123_CHARS_PATTERN = re.compile(r"[^a-z0-9-]+")
_RFC1123_DASH_PATTERN = re.compile(r"-+")
_KUBERNETES_LABEL_CHARS_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")
_KUBERNETES_LABEL_EDGE_PATTERN = re.compile(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$")


class ProjectDeploymentError(TypefluxError, ValueError):
    """Raised when a project deployment plan cannot be generated safely."""

    def __init__(
        self,
        message: str,
        *,
        checks: Sequence[ProjectValidationCheck] = (),
    ) -> None:
        super().__init__(message)
        self.checks = tuple(checks)


class ProjectDeploymentPolicyIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_policy_ids: tuple[str, ...]
    applied_policy_ids: tuple[str, ...]
    policy_names: tuple[str, ...]
    policy_hash: str

    @classmethod
    def from_policy(cls, policy: ComposedProjectPolicy) -> ProjectDeploymentPolicyIdentity:
        return cls(
            selected_policy_ids=policy.selected_policy_ids,
            applied_policy_ids=policy.applied_policy_ids,
            policy_names=policy.policy_names,
            policy_hash=policy.policy_hash,
        )


class ProjectDeploymentSecretEnvRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_path: str
    env_name: str
    secret_name: str
    secret_key: str
    required: bool
    configured: bool


class ProjectDeploymentSecretFileRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_path: str
    mount_path: str
    secret_name: str
    secret_key: str
    required: bool
    configured: bool


class ProjectDeploymentWorkerPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    workflow_id: str
    workflow_name: str
    yaml_project: str
    yaml_name: str
    workflow_path: str
    environment_id: str
    environment_name: str
    task_queue: str
    project_path_in_image: str
    command: tuple[str, ...]
    config_map_name: str
    secret_name: str
    config_map: dict[str, str] = Field(default_factory=dict)
    secret_env: tuple[ProjectDeploymentSecretEnvRef, ...] = ()
    secret_files: tuple[ProjectDeploymentSecretFileRef, ...] = ()
    policy: ProjectDeploymentPolicyIdentity


class ProjectDeploymentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["1"] = "1"
    target: Literal["kubernetes"] = "kubernetes"
    project_name: str
    project_manifest_path: str
    project_path_in_image: str
    environment_id: str
    environment_name: str
    image: str
    image_digest_pinned: bool
    workers: tuple[ProjectDeploymentWorkerPlan, ...]


class ProjectDeploymentRenderedFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    kind: Literal["plan", "kubernetes", "secret_template"]
    sha256: str


class ProjectDeploymentRenderResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    output_dir: str
    files: tuple[ProjectDeploymentRenderedFile, ...]


def build_project_deployment_plan(
    project: TypefluxProjectSpec,
    *,
    environment_id: str,
    workflow_ids: Sequence[str] = (),
    policy_ids: Sequence[str] = (),
    image: str,
    allow_mutable_image: bool = False,
    allow_placeholder_image: bool = False,
    allow_shared_task_queue: bool = False,
    project_path_in_image: str | None = None,
    project_manifest_path: str | None = None,
    config_env_names: Sequence[str] = (),
    target: Literal["kubernetes"] = "kubernetes",
    base_env: Mapping[str, str] | None = None,
) -> ProjectDeploymentPlan:
    """Build a deterministic, secret-free deployment plan for resolved project workers.

    ``project_manifest_path`` overrides the value RECORDED in ``project_manifest_path`` (surfaced in
    render annotations + deployment-plan.json; #757 item 1). The CLI passes a manifest-dir-relative
    value so committed artifacts are machine-independent — this field is provenance only, never part
    of the plan hash or drift detection, so a relative default is a safe behavior change.

    ``base_env`` (#760): the hermetic interpolation base for each worker's `${VAR}` spec
    references. When provided, every workflow — and, via the base retained on the resolved
    artifact, every sub-workflow the policy-admission and observability closures walk —
    resolves against the injected mapping, never the operator's shell, so the rendered
    ``deployment-plan.json``/ConfigMap bytes are machine-independent. Omitted, resolution
    reads the process environment as before.
    """

    image_digest_pinned = _validate_image(
        image,
        allow_mutable_image=allow_mutable_image,
        allow_placeholder_image=allow_placeholder_image,
    )
    selected_workflow_ids = _selected_workflow_ids(project, workflow_ids)
    report = validate_project(project)
    if not report.ok:
        messages = "; ".join(issue.message for issue in report.issues)
        raise ProjectDeploymentError(f"project deployment references are invalid: {messages}")

    in_image_path = (
        _default_project_path_in_image(project)
        if project_path_in_image is None
        else (project_path_in_image)
    )
    _validate_project_path_in_image(in_image_path)

    config_env_set = frozenset(config_env_names)
    workers: list[ProjectDeploymentWorkerPlan] = []
    task_queues: dict[str, str] = {}
    for workflow_id in selected_workflow_ids:
        resolved = resolve_project_workflow(
            project,
            workflow_id=workflow_id,
            environment_id=environment_id,
            base_env=base_env,
        )
        policy = _admit_policy(
            project,
            resolved,
            explicit_policy_ids=tuple(policy_ids),
        )
        # #757 review: observability closure consistency runs AFTER policy admission (policy
        # errors take precedence; unresolved refs already failed closed) and BEFORE the
        # credential scaffolding in _worker_plan, so the scaffold provably keys off the verified
        # single effective backend.
        assert_closure_observability_consistent(project, resolved, environment_id=environment_id)
        name = _resource_name(project.name, environment_id, workflow_id)
        if not allow_shared_task_queue:
            previous_workflow_id = task_queues.get(resolved.spec.task_queue)
            if previous_workflow_id is not None:
                raise ProjectDeploymentError(
                    "project deployment would create separate workers for workflows "
                    f"{previous_workflow_id!r} and {workflow_id!r} on shared task queue "
                    f"{resolved.spec.task_queue!r}; pass --allow-shared-task-queue to allow this"
                )
            task_queues[resolved.spec.task_queue] = workflow_id
        workers.append(
            _worker_plan(
                project=project,
                resolved=resolved,
                policy=policy,
                resource_name=name,
                project_path_in_image=in_image_path,
                config_env_names=config_env_set,
            )
        )

    workers = sorted(workers, key=lambda worker: worker.workflow_id)
    return ProjectDeploymentPlan(
        target=target,
        project_name=project.name,
        project_manifest_path=(
            project_manifest_path
            if project_manifest_path is not None
            else str(project.manifest_path)
        ),
        project_path_in_image=in_image_path,
        environment_id=environment_id,
        environment_name=workers[0].environment_name,
        image=image,
        image_digest_pinned=image_digest_pinned,
        workers=tuple(workers),
    )


def render_project_deployment_plan(
    plan: ProjectDeploymentPlan,
    output_dir: str | Path,
) -> ProjectDeploymentRenderResult:
    """Render deterministic, secret-free deployment artifacts for a project plan."""

    output_path = Path(output_dir).expanduser().resolve()
    if output_path.exists() and not output_path.is_dir():
        raise ProjectDeploymentError(f"deployment output path is not a directory: {output_path}")
    output_path.mkdir(parents=True, exist_ok=True)

    files = (
        _write_rendered_file(
            output_path / "deployment-plan.json",
            _render_plan_json(plan),
            kind="plan",
        ),
        _write_rendered_file(
            output_path / "kubernetes.yaml",
            _render_kubernetes_yaml(plan),
            kind="kubernetes",
        ),
        _write_rendered_file(
            output_path / "secret.scaffold.yaml",
            _render_secret_scaffold_yaml(plan),
            kind="secret_template",
        ),
        _write_rendered_file(
            output_path / "secrets.env.example",
            _render_secret_env_example(plan),
            kind="secret_template",
        ),
    )
    return ProjectDeploymentRenderResult(output_dir=str(output_path), files=files)


def _render_plan_json(plan: ProjectDeploymentPlan) -> str:
    import json

    return json.dumps(plan.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def _write_rendered_file(
    path: Path,
    content: str,
    *,
    kind: Literal["plan", "kubernetes", "secret_template"],
) -> ProjectDeploymentRenderedFile:
    path.write_text(content, encoding="utf-8")
    return ProjectDeploymentRenderedFile(
        path=str(path),
        kind=kind,
        sha256=sha256(content.encode("utf-8")).hexdigest(),
    )


def _render_kubernetes_yaml(plan: ProjectDeploymentPlan) -> str:
    # Intentionally excludes Secret manifests so this file is safe to `kubectl apply`
    # repeatedly without clobbering populated secrets. The Deployment references
    # Secrets by name; provision them from secret.scaffold.yaml / your secret manager.
    docs: list[dict[str, object]] = []
    for worker in plan.workers:
        docs.append(_kubernetes_config_map(plan, worker))
        docs.append(_kubernetes_deployment(plan, worker))
    return yaml.safe_dump_all(
        docs,
        explicit_start=True,
        sort_keys=False,
        default_flow_style=False,
    )


def _render_secret_scaffold_yaml(plan: ProjectDeploymentPlan) -> str:
    header = (
        "# Typeflux deployment Secret scaffold.\n"
        "# WARNING: these are BLANK placeholder values. Do NOT `kubectl apply` this\n"
        "# over a populated Secret — it would erase real secret values. Provision\n"
        "# these Secrets from your secret manager; kubernetes.yaml references them by\n"
        "# name and never defines or overwrites them.\n"
    )
    docs = [
        _kubernetes_secret_template(plan, worker)
        for worker in plan.workers
        if worker.secret_env or worker.secret_files
    ]
    if not docs:
        return header + "# No Secret values are required for this deployment.\n"
    return header + yaml.safe_dump_all(
        docs,
        explicit_start=True,
        sort_keys=False,
        default_flow_style=False,
    )


def _kubernetes_config_map(
    plan: ProjectDeploymentPlan,
    worker: ProjectDeploymentWorkerPlan,
) -> dict[str, object]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": _kubernetes_metadata(
            plan,
            worker,
            name=worker.config_map_name,
            component="config",
        ),
        "data": dict(sorted(worker.config_map.items())),
    }


def _kubernetes_secret_template(
    plan: ProjectDeploymentPlan,
    worker: ProjectDeploymentWorkerPlan,
) -> dict[str, object]:
    keys = sorted(
        {ref.secret_key for ref in worker.secret_env}
        | {ref.secret_key for ref in worker.secret_files}
    )
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": _kubernetes_metadata(
            plan,
            worker,
            name=worker.secret_name,
            component="secrets",
        ),
        "type": "Opaque",
        "stringData": {key: "" for key in keys},
    }


def _preflight_marker_probe_command() -> list[str]:
    # Fresh list per probe so YAML rendering never emits anchors/aliases for a
    # shared object.
    return ["sh", "-c", f"test -f {_PREFLIGHT_MARKER_PATH}"]


def _kubernetes_deployment(
    plan: ProjectDeploymentPlan,
    worker: ProjectDeploymentWorkerPlan,
) -> dict[str, object]:
    labels = _worker_labels(plan, worker, component="worker")
    volume_mounts: list[dict[str, object]] = [{"name": "tmp", "mountPath": "/tmp"}]
    env: list[dict[str, object]] = _runtime_placement_env(plan, worker)
    container: dict[str, object] = {
        "name": "worker",
        "image": plan.image,
        "imagePullPolicy": "IfNotPresent",
        "command": list(worker.command),
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "envFrom": [{"configMapRef": {"name": worker.config_map_name}}],
        "startupProbe": {
            "exec": {"command": _preflight_marker_probe_command()},
            "failureThreshold": 12,
            "periodSeconds": 10,
            "timeoutSeconds": 10,
        },
        "readinessProbe": {
            "exec": {"command": _preflight_marker_probe_command()},
            "initialDelaySeconds": 5,
            "periodSeconds": 15,
            "timeoutSeconds": 5,
        },
        "livenessProbe": {
            "exec": {"command": _preflight_marker_probe_command()},
            "initialDelaySeconds": 30,
            "periodSeconds": 30,
            "timeoutSeconds": 5,
        },
        "resources": {
            "requests": {"cpu": "500m", "memory": "512Mi"},
            "limits": {"cpu": "2", "memory": "2Gi"},
        },
        "volumeMounts": volume_mounts,
    }
    if worker.secret_env:
        env.extend(
            {
                "name": ref.env_name,
                "valueFrom": {
                    "secretKeyRef": {
                        "name": ref.secret_name,
                        "key": ref.secret_key,
                        "optional": not ref.required,
                    }
                },
            }
            for ref in worker.secret_env
        )
    if env:
        container["env"] = env
    if worker.secret_files:
        for ref in worker.secret_files:
            volume_mounts.append(
                {
                    "name": "typeflux-secret-files",
                    "mountPath": ref.mount_path,
                    "subPath": ref.secret_key,
                    "readOnly": True,
                }
            )
        container["volumeMounts"] = volume_mounts

    volumes: list[dict[str, object]] = [{"name": "tmp", "emptyDir": {}}]
    if worker.secret_files:
        volumes.append(
            {
                "name": "typeflux-secret-files",
                "secret": {
                    "secretName": worker.secret_name,
                    "items": [
                        {"key": ref.secret_key, "path": ref.secret_key}
                        for ref in worker.secret_files
                    ],
                },
            }
        )

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": _kubernetes_metadata(
            plan,
            worker,
            name=worker.name,
            component="worker",
        ),
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "terminationGracePeriodSeconds": 45,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [container],
                    "volumes": volumes,
                },
            },
        },
    }


def _runtime_placement_env(
    plan: ProjectDeploymentPlan,
    worker: ProjectDeploymentWorkerPlan,
) -> list[dict[str, object]]:
    return [
        {"name": "TYPEFLUX_RUNTIME_PLATFORM", "value": "kubernetes"},
        {
            "name": "TYPEFLUX_K8S_NAMESPACE",
            "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
        },
        {
            "name": "TYPEFLUX_K8S_POD_NAME",
            "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
        },
        {
            "name": "TYPEFLUX_K8S_POD_UID",
            "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
        },
        {
            "name": "TYPEFLUX_K8S_NODE_NAME",
            "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}},
        },
        {
            "name": "TYPEFLUX_K8S_SERVICE_ACCOUNT",
            "valueFrom": {"fieldRef": {"fieldPath": "spec.serviceAccountName"}},
        },
        {"name": "TYPEFLUX_K8S_DEPLOYMENT_NAME", "value": worker.name},
        {"name": "TYPEFLUX_K8S_WORKER_NAME", "value": worker.name},
        {"name": "TYPEFLUX_CONTAINER_IMAGE", "value": plan.image},
    ]


def _kubernetes_metadata(
    plan: ProjectDeploymentPlan,
    worker: ProjectDeploymentWorkerPlan,
    *,
    name: str,
    component: str,
) -> dict[str, object]:
    return {
        "name": name,
        "labels": _worker_labels(plan, worker, component=component),
        "annotations": {
            "typeflux.io/project-manifest-path": plan.project_manifest_path,
            "typeflux.io/project-path-in-image": plan.project_path_in_image,
            "typeflux.io/workflow-path": worker.workflow_path,
            "typeflux.io/policy-hash": worker.policy.policy_hash,
        },
    }


def _worker_labels(
    plan: ProjectDeploymentPlan,
    worker: ProjectDeploymentWorkerPlan,
    *,
    component: str,
) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": worker.name,
        "app.kubernetes.io/component": _label_value(component),
        "app.kubernetes.io/part-of": _label_value(plan.project_name),
        "typeflux.io/project": _label_value(plan.project_name),
        "typeflux.io/environment": _label_value(plan.environment_id),
        "typeflux.io/workflow": _label_value(worker.workflow_id),
    }


def _label_value(value: str) -> str:
    label = _KUBERNETES_LABEL_CHARS_PATTERN.sub("-", value).strip("-_.")
    label = _KUBERNETES_LABEL_EDGE_PATTERN.sub("", label)
    if not label:
        return "typeflux"
    if len(label) <= 63:
        return label
    digest = sha256(label.encode("utf-8")).hexdigest()[:8]
    return f"{label[:54].rstrip('-_.')}-{digest}"


def _render_secret_env_example(plan: ProjectDeploymentPlan) -> str:
    lines = [
        "# Typeflux deployment secret template",
        f"# project={plan.project_name}",
        f"# environment={plan.environment_id}",
        "# Populate these values in your secret manager. Do not commit real secrets.",
        "# For local smoke tests with kubectl --from-env-file, use unquoted KEY=value",
        "# lines or generate a kubectl-only env file with a dotenv parser first.",
        "",
    ]
    for worker in plan.workers:
        lines.append(f"# {worker.workflow_id}: Kubernetes Secret {worker.secret_name}")
        for ref in worker.secret_env:
            lines.append(f"{ref.secret_key}=")
        for file_ref in worker.secret_files:
            lines.append(
                f"# file secret key {file_ref.secret_key} mounts to {file_ref.mount_path}; "
                "put the file contents in the Kubernetes Secret template"
            )
        if not worker.secret_env and not worker.secret_files:
            lines.append("# no secret values required")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _validate_image(
    image: str,
    *,
    allow_mutable_image: bool,
    allow_placeholder_image: bool = False,
) -> bool:
    if not image or image.strip() != image:
        raise ProjectDeploymentError("deployment image must be non-empty and trimmed")
    name, separator, digest = image.rpartition("@")
    digest_pinned = (
        bool(separator) and bool(name) and _SHA256_DIGEST_PATTERN.fullmatch(digest) is not None
    )
    if not digest_pinned and not allow_mutable_image:
        raise ProjectDeploymentError(
            "Kubernetes deployment images must be pinned by digest "
            "(expected ...@sha256:<64 hex chars>); pass --allow-mutable-image for dev/local output"
        )
    if not allow_placeholder_image and is_placeholder_image_digest(image):
        raise ProjectDeploymentError(
            f"deployment image {image!r} pins the well-known all-zeros placeholder digest "
            f"({PLACEHOLDER_IMAGE_DIGEST}), which resolves to no published image and fails at pod "
            "scheduling (ImagePullBackOff) after the plan/PR/promote approval chain; regenerate with "
            "a real published digest, or pass --allow-placeholder-image (allow_placeholder_image) to "
            "write a placeholder plan intentionally"
        )
    return digest_pinned


def _selected_workflow_ids(
    project: TypefluxProjectSpec,
    workflow_ids: Sequence[str],
) -> tuple[str, ...]:
    if workflow_ids:
        return tuple(_dedupe(workflow_ids))
    return tuple(workflow.id for workflow in project.workflows)


def assert_closure_observability_consistent(
    project: TypefluxProjectSpec,
    resolved: ProjectResolvedWorkflow,
    *,
    environment_id: str,
) -> None:
    """Run the #756 closure observability-consistency gate at plan build (#757 review).

    A composed worker builds ONE observer — the parent's — so a child declaring a DIFFERENT
    real backend (parent langfuse + child langsmith) must fail HERE, at authoring time, with
    the same ``ObservabilityCompositionError`` the worker boot raises — not pass
    plan/PR/promote cleanly (each spec individually policy-compliant, only the parent's creds
    scaffolded) and then crash-loop at pod boot. With consistency verified, the parent's
    backend IS the closure's single effective backend — exactly what
    ``_observability_secret_references`` scaffolds credentials for. Reuses the ONE closure walk
    policy admission runs (``_iter_subworkflow_closure``); unresolvable refs are skipped here —
    policy closure admission already fails them closed.
    """
    # Lazy import (the precedent yaml.runtime itself sets for the reverse direction): the
    # worker-runtime module is heavy, and this keeps the plan builder importable without it.
    from typeflux.yaml.runtime import _assert_consistent_observability_config

    for ref, child, _error in _iter_subworkflow_closure(
        project, resolved=resolved, environment_id=environment_id
    ):
        if child is None:
            continue
        _assert_consistent_observability_config(resolved.spec, ref, child.spec)


def _admit_policy(
    project: TypefluxProjectSpec,
    resolved: ProjectResolvedWorkflow,
    *,
    explicit_policy_ids: Sequence[str],
) -> ComposedProjectPolicy:
    selected_policy_ids = select_project_policy_ids_for_workflow(
        project,
        environment_id=resolved.environment_id,
        workflow_id=resolved.workflow_id,
        explicit_policy_ids=explicit_policy_ids,
    )
    if not selected_policy_ids:
        raise ProjectDeploymentError(
            "project deployment requires at least one selected policy for "
            f"environment {resolved.environment_id!r} workflow {resolved.workflow_id!r}"
        )
    with project_environment_context(resolved.application):
        policy = compose_project_policies(project, selected_policy_ids)
        checks = _deployment_policy_checks(project=project, resolved=resolved, policy=policy)
    failures = tuple(check for check in checks if check.status == "failed")
    if failures:
        message = "; ".join(f"{check.code}: {check.message or 'failed'}" for check in failures)
        raise ProjectDeploymentError(
            f"project policy admission failed for workflow {resolved.workflow_id!r}: {message}",
            checks=checks,
        )
    return policy


def _deployment_policy_checks(
    *,
    project: TypefluxProjectSpec,
    resolved: ProjectResolvedWorkflow,
    policy: ComposedProjectPolicy,
) -> tuple[ProjectValidationCheck, ...]:
    checks = list(validate_project_policy(project=project, resolved=resolved, policy=policy))
    # #788: a deployment plan for an elevated-tier workflow (closure-aware) must be
    # backed by a policy that actually enforces the tier — same gap predicate as
    # validation/admission/the runtime guard.
    binding_gap = risk_tier_enforcement_gap(
        project,
        resolved=resolved,
        environment_id=resolved.environment_id,
        selected_policy_ids=policy.selected_policy_ids,
        policy=policy,
    )
    if binding_gap is not None:
        checks.append(
            ProjectValidationCheck(code="risk_tier_binding", status="failed", message=binding_gap)
        )
    adjusted: list[ProjectValidationCheck] = []
    for check in checks:
        if check.status != "failed" or check.code != "policy_temporal":
            adjusted.append(check)
            continue
        messages = [item.strip() for item in (check.message or "").split(";") if item.strip()]
        if isinstance(resolved.spec.runtime.temporal.api_key, SecretValueSpec):
            messages = [
                message
                for message in messages
                if message != "Temporal API key is required by project policy"
            ]
        if messages:
            adjusted.append(
                ProjectValidationCheck(
                    code=check.code,
                    status="failed",
                    message="; ".join(messages),
                    details=check.details,
                )
            )
        else:
            adjusted.append(
                ProjectValidationCheck(
                    code=check.code,
                    status="passed",
                    details={
                        **check.details,
                        "api_key_configured": True,
                        "api_key_source": "deployment_secret_reference",
                    },
                )
            )
    # Transitive-closure admission (#55 §9): a deploy plan for a parent must admit the
    # parent's referenced sub-workflows under the parent's composed policy too — the
    # deploy gate is an admission entry point like the validate report and the runtime
    # guard, and a composed program is one versioned artifact. Never emitted for a
    # non-composed workflow, and never subject to the api-key adjustment above.
    closure = validate_subworkflow_closure_policy(
        project=project,
        resolved=resolved,
        policy=policy,
        environment_id=resolved.environment_id,
    )
    if closure is not None:
        adjusted.append(closure)
    return tuple(adjusted)


def _worker_plan(
    *,
    project: TypefluxProjectSpec,
    resolved: ProjectResolvedWorkflow,
    policy: ComposedProjectPolicy,
    resource_name: str,
    project_path_in_image: str,
    config_env_names: frozenset[str] = frozenset(),
) -> ProjectDeploymentWorkerPlan:
    config_map_name = f"{resource_name}-config"
    secret_name = f"{resource_name}-secrets"
    secret_env, secret_files = _secret_references(resolved.spec, secret_name=secret_name)
    spec_env_names = {ref.env_name for ref in secret_env}
    env_refs = _environment_secret_references(
        resolved,
        secret_name=secret_name,
        existing_env_names=spec_env_names,
        config_env_names=config_env_names,
    )
    # Observability creds (#757 item 3) are appended AFTER the typed + env-derived refs and skip any
    # name already claimed by them, so a spec that also declares LANGFUSE_SECRET_KEY as an env var
    # is not double-referenced.
    observability_refs = _observability_secret_references(
        resolved.spec,
        policy=policy,
        secret_name=secret_name,
        existing_env_names=spec_env_names | {ref.env_name for ref in env_refs},
    )
    secret_env = _sort_secret_env_refs((*secret_env, *env_refs, *observability_refs))
    config_map = _config_map_entries(
        resolved,
        policy=policy,
        secret_env=secret_env,
        config_env_names=config_env_names,
    )
    _validate_environment_variables_classified(
        resolved,
        config_map=config_map,
        secret_env=secret_env,
    )
    command = _worker_command(
        project_path_in_image=project_path_in_image,
        workflow_id=resolved.workflow_id,
        environment_id=resolved.environment_id,
        policy_ids=policy.selected_policy_ids,
        expected_policy_hash=policy.policy_hash,
    )
    return ProjectDeploymentWorkerPlan(
        name=resource_name,
        workflow_id=resolved.workflow_id,
        workflow_name=resolved.spec.workflow.name,
        yaml_project=resolved.spec.project,
        yaml_name=resolved.spec.name,
        workflow_path=str(resolved.workflow_path),
        environment_id=resolved.environment_id,
        environment_name=resolved.environment.name,
        task_queue=resolved.spec.task_queue,
        project_path_in_image=project_path_in_image,
        command=command,
        config_map_name=config_map_name,
        secret_name=secret_name,
        config_map=config_map,
        secret_env=secret_env,
        secret_files=secret_files,
        policy=ProjectDeploymentPolicyIdentity.from_policy(policy),
    )


def _worker_command(
    *,
    project_path_in_image: str,
    workflow_id: str,
    environment_id: str,
    policy_ids: Sequence[str],
    expected_policy_hash: str,
) -> tuple[str, ...]:
    run = [
        "python",
        "-m",
        "typeflux.project",
        "run",
        project_path_in_image,
        "--workflow",
        workflow_id,
        "--environment",
        environment_id,
    ]
    for policy_id in policy_ids:
        run.extend(["--policy", policy_id])
    run.extend(["--expect-policy-hash", expected_policy_hash])
    quoted_run = " ".join(shlex.quote(part) for part in run)
    # Remove any marker inherited from a previous container in the same pod
    # (/tmp is a pod-lifetime emptyDir) so probes never report a restarted
    # container healthy before its own preflight has passed.
    return (
        "sh",
        "-c",
        f"rm -f {_PREFLIGHT_MARKER_PATH}"
        f" && {quoted_run} --preflight"
        f" && touch {_PREFLIGHT_MARKER_PATH}"
        f" && exec {quoted_run}",
    )


def _config_map_entries(
    resolved: ProjectResolvedWorkflow,
    *,
    policy: ComposedProjectPolicy,
    secret_env: Sequence[ProjectDeploymentSecretEnvRef],
    config_env_names: frozenset[str] = frozenset(),
) -> dict[str, str]:
    secret_env_names = {ref.env_name for ref in secret_env}
    safe_names = _SAFE_CONFIG_ENV_NAMES | config_env_names
    entries: dict[str, str] = {
        "TEMPORAL_ADDRESS": resolved.spec.runtime.temporal.address,
        "TEMPORAL_NAMESPACE": resolved.spec.runtime.temporal.namespace,
        "TEMPORAL_TASK_QUEUE": resolved.spec.task_queue,
        "TEMPORAL_TLS": _tls_env_value(resolved.spec),
        "TYPEFLUX_EXPECTED_POLICY_HASH": policy.policy_hash,
    }
    for key in resolved.application.profile_variable_names:
        if key in secret_env_names or key not in safe_names:
            continue
        value = resolved.application.variables.get(key)
        if value is not None:
            entries[key] = value
    for key, value in resolved.application.variables.items():
        if key in entries or key in secret_env_names or key not in safe_names:
            continue
        entries[key] = value
    provider = resolved.spec.runtime.provider
    model_env_name = _PROVIDER_MODEL_ENV_NAMES.get(provider.type)
    if model_env_name is not None and provider.model is not None:
        entries.setdefault(model_env_name, provider.model)
    registry = resolved.spec.runtime.registry
    if registry.host is not None:
        entries.setdefault("LANGFUSE_HOST", registry.host)
    if registry.label is not None:
        entries.setdefault("LANGFUSE_PROMPT_LABEL", registry.label)
    return {key: entries[key] for key in sorted(entries)}


def _environment_secret_references(
    resolved: ProjectResolvedWorkflow,
    *,
    secret_name: str,
    existing_env_names: set[str],
    config_env_names: frozenset[str] = frozenset(),
) -> tuple[ProjectDeploymentSecretEnvRef, ...]:
    refs: list[ProjectDeploymentSecretEnvRef] = []
    for key in sorted(resolved.application.variables):
        if (
            key in existing_env_names
            or key in config_env_names
            or not _SECRET_LIKE_ENV_PATTERN.search(key)
        ):
            continue
        refs.append(
            ProjectDeploymentSecretEnvRef(
                runtime_path=f"environment.variables.{key}",
                env_name=key,
                secret_name=secret_name,
                secret_key=key,
                required=True,
                configured=True,
            )
        )
    return tuple(refs)


def _observability_secret_references(
    spec: TypefluxYamlSpec,
    *,
    policy: ComposedProjectPolicy,
    secret_name: str,
    existing_env_names: set[str],
) -> tuple[ProjectDeploymentSecretEnvRef, ...]:
    """Secret env refs for the resolved spec's observability backend credentials (#757 item 3).

    WITHOUT this the renderer scaffolds ONLY typed spec secrets, so a worker declaring
    ``runtime.observability.type: langfuse|langsmith`` carries NO credential surface — and because
    the observer degrades to an untraced run with one warning when the keys are absent, a
    required-observability deployment would come up HEALTHY and run SILENTLY UNTRACED (the exact gap
    #756's runtime gate closes at run time). We model the backend's credential NAMES into the
    worker's Secret surface. ``required`` mirrors whether the effective composed policy marks
    observability as REQUIRED (``observability.required``, #756): a policy-required backend's creds
    are a hard secretKeyRef, otherwise optional (missing ⇒ untraced warning only). Names already
    claimed by typed/env secret refs are skipped (no duplication).
    """
    backend = spec.runtime.observability.type or ""
    names = _OBSERVABILITY_BACKEND_SECRET_ENV.get(backend)
    if names is None:
        return ()
    obs_policy = policy.payload.get("observability")
    required = isinstance(obs_policy, Mapping) and obs_policy.get("required") is True

    # #793: a spec-declared credential CLAIMS its canonical slot whatever its source name —
    # scaffolding the standard fallback name for a claimed slot would render a required
    # secretKeyRef for a key nothing populates (CreateContainerConfigError at rollout).
    # Only a NON-EMPTY literal ("" is the documented `${VAR:-}` unset sentinel) or a
    # REQUIRED reference claims: an optional reference whose source is absent falls back
    # to the standard env var at runtime, so its fallback ref must stay scaffolded (codex).
    def _claims_slot(value: str | SecretValueSpec | None) -> bool:
        if isinstance(value, str):
            return value != ""
        if isinstance(value, SecretValueSpec):
            return value.value_from.required is not False
        return False

    langfuse = spec.runtime.observability.langfuse
    langsmith = spec.runtime.observability.langsmith
    slot_values: tuple[tuple[str, str | SecretValueSpec | None], ...] = (
        ("runtime.observability.langfuse.public_key", langfuse.public_key if langfuse else None),
        ("runtime.observability.langfuse.secret_key", langfuse.secret_key if langfuse else None),
        ("runtime.observability.langsmith.api_key", langsmith.api_key if langsmith else None),
    )
    declared_slots = {path for path, value in slot_values if _claims_slot(value)}
    # A declared-but-OPTIONAL reference keeps its fallback ref but demotes it to optional:
    # runtime resolution can succeed via EITHER source, so a hard secretKeyRef on the
    # standard name would block a pod whose custom optional var is the populated one (codex).
    optional_declared_slots = {
        path
        for path, value in slot_values
        if isinstance(value, SecretValueSpec) and value.value_from.required is False
    }
    refs: list[ProjectDeploymentSecretEnvRef] = []
    for env_name in names:
        if env_name in existing_env_names:
            continue
        slot = _OBSERVABILITY_FALLBACK_SLOT_PATHS.get(env_name)
        if slot in declared_slots:
            continue
        refs.append(
            ProjectDeploymentSecretEnvRef(
                runtime_path=slot or f"runtime.observability.{backend}.{env_name.lower()}",
                env_name=env_name,
                secret_name=secret_name,
                secret_key=env_name,
                required=required and slot not in optional_declared_slots,
                configured=True,
            )
        )
    return tuple(refs)


def _validate_environment_variables_classified(
    resolved: ProjectResolvedWorkflow,
    *,
    config_map: Mapping[str, str],
    secret_env: Sequence[ProjectDeploymentSecretEnvRef],
) -> None:
    classified = set(config_map) | {ref.env_name for ref in secret_env}
    unclassified = sorted(key for key in resolved.application.variables if key not in classified)
    if unclassified:
        joined = ", ".join(unclassified)
        raise ProjectDeploymentError(
            "project deployment cannot classify environment variable(s) as safe ConfigMap "
            f"values or Secret refs: {joined}"
        )


def _sort_secret_env_refs(
    refs: Sequence[ProjectDeploymentSecretEnvRef],
) -> tuple[ProjectDeploymentSecretEnvRef, ...]:
    return tuple(sorted(refs, key=lambda ref: (ref.runtime_path, ref.env_name)))


def _tls_env_value(spec: TypefluxYamlSpec) -> str:
    tls = spec.runtime.temporal.tls
    return "true" if tls is not False else "false"


def _secret_references(
    spec: TypefluxYamlSpec,
    *,
    secret_name: str,
) -> tuple[
    tuple[ProjectDeploymentSecretEnvRef, ...],
    tuple[ProjectDeploymentSecretFileRef, ...],
]:
    records: list[tuple[str, SecretValueSpec]] = []
    _append_secret(records, "runtime.temporal.api_key", spec.runtime.temporal.api_key)
    tls = spec.runtime.temporal.tls
    if isinstance(tls, TemporalTLSConfigSpec):
        _append_secret(
            records,
            "runtime.temporal.tls.server_root_ca_cert",
            tls.server_root_ca_cert,
        )
        _append_secret(
            records,
            "runtime.temporal.tls.client_cert",
            tls.client_cert,
        )
        _append_secret(
            records,
            "runtime.temporal.tls.client_private_key",
            tls.client_private_key,
        )
    _append_secret(records, "runtime.provider.api_key", spec.runtime.provider.api_key)
    # AES-256-GCM payload codec key material (#188): one secret slot per declared key so
    # the generated deployment scaffolds/injects the AES key a codec-enabled worker needs
    # at preflight/startup. The runtime_path + env-var name (source.env) match exactly what
    # secret_reference_records reports and what build_payload_codec resolves at runtime.
    codec = spec.runtime.temporal.payload_codec
    if codec is not None:
        for key_spec in codec.keys:
            _append_secret(
                records,
                f"runtime.temporal.payload_codec.keys[{key_spec.id}].value_from",
                SecretValueSpec(value_from=key_spec.value_from),
            )

    # Spec-declared observability credentials (#793): typed value_from references, same
    # contract as api_key; the standard-name scaffold (_OBSERVABILITY_BACKEND_SECRET_ENV)
    # skips any env name these claim, so declared and fallback surfaces never duplicate.
    # Optional FILE sources are skipped like the custom-config ones below: the secret
    # volume's items list has no optional handling, so scaffolding one would make an
    # intentionally-omitted key block the mount while runtime falls back to env (codex).
    def _append_observability_secret(
        runtime_path: str, value: str | SecretValueSpec | None
    ) -> None:
        if (
            isinstance(value, SecretValueSpec)
            and value.value_from.file is not None
            and value.value_from.required is False
        ):
            return
        _append_secret(records, runtime_path, value)

    observability = spec.runtime.observability
    if observability.langfuse is not None:
        _append_observability_secret(
            "runtime.observability.langfuse.public_key", observability.langfuse.public_key
        )
        _append_observability_secret(
            "runtime.observability.langfuse.secret_key", observability.langfuse.secret_key
        )
    if observability.langsmith is not None:
        _append_observability_secret(
            "runtime.observability.langsmith.api_key", observability.langsmith.api_key
        )
    # Custom-extension config references (#792): derived from the shared slot walk, so a
    # config credential the bundle inventories is also scaffolded/injected here — a declared
    # required source that never reaches the pod would crash the worker at startup. Literal
    # config entries live in the spec itself and need no injection. Optional FILE sources
    # are skipped: the secret volume's items list has no optional handling (unlike env
    # secretKeyRefs), so scaffolding one would make an intentionally-omitted key block the
    # mount, while the runtime resolver simply omits the entry (codex).
    records.extend(
        (path, value)
        for path, value in custom_extension_config_sources(spec)
        if value.value_from.file is None or value.value_from.required
    )

    env_refs: list[ProjectDeploymentSecretEnvRef] = []
    file_refs: list[ProjectDeploymentSecretFileRef] = []
    for runtime_path, value in sorted(records, key=lambda item: item[0]):
        source = value.value_from
        if source.env is not None:
            env_refs.append(
                ProjectDeploymentSecretEnvRef(
                    runtime_path=runtime_path,
                    env_name=source.env,
                    secret_name=secret_name,
                    secret_key=source.env,
                    required=source.required,
                    configured=True,
                )
            )
            continue
        if source.file is not None:
            file_refs.append(
                ProjectDeploymentSecretFileRef(
                    runtime_path=runtime_path,
                    mount_path=_safe_secret_mount_path(
                        source.file,
                        runtime_path=runtime_path,
                    ),
                    secret_name=secret_name,
                    secret_key=_secret_file_key(runtime_path),
                    required=source.required,
                    configured=True,
                )
            )
    return (tuple(env_refs), tuple(file_refs))


def _append_secret(
    records: list[tuple[str, SecretValueSpec]],
    runtime_path: str,
    value: object,
) -> None:
    if isinstance(value, SecretValueSpec):
        records.append((runtime_path, value))
        return
    if isinstance(value, str) and value:
        raise ProjectDeploymentError(
            f"deployment generation requires typed secret references for {runtime_path}; "
            "use value_from.env or value_from.file"
        )


def _safe_secret_mount_path(path_value: str, *, runtime_path: str) -> str:
    path = Path(path_value)
    if not path.is_absolute():
        raise ProjectDeploymentError(
            f"secret file reference for {runtime_path} must use an absolute mount path"
        )
    if ".." in path.parts:
        raise ProjectDeploymentError(
            f"secret file reference for {runtime_path} must use a normalized file path"
        )
    normalized = Path("/") / path.relative_to("/")
    if str(normalized) != path_value or normalized == Path("/"):
        raise ProjectDeploymentError(
            f"secret file reference for {runtime_path} must use a normalized file path"
        )
    return str(normalized)


def _secret_file_key(runtime_path: str) -> str:
    key = _SECRET_NAME_KEY_PATTERN.sub("_", runtime_path).strip("_")
    return key or "secret"


def _default_project_path_in_image(project: TypefluxProjectSpec) -> str:
    cwd = Path.cwd().resolve()
    try:
        relative = project.manifest_path.relative_to(cwd)
    except ValueError:
        relative = Path(project.manifest_path.name)
    return f"/app/{relative.as_posix()}"


def _validate_project_path_in_image(value: str) -> None:
    if not value or value.strip() != value or not value.startswith("/"):
        raise ProjectDeploymentError("--project-path-in-image must be an absolute image path")


def _resource_name(project_name: str, environment_id: str, workflow_id: str) -> str:
    base = f"typeflux-{project_name}-{environment_id}-{workflow_id}".lower()
    name = _RFC1123_CHARS_PATTERN.sub("-", base)
    name = _RFC1123_DASH_PATTERN.sub("-", name).strip("-")
    if not name:
        return "typeflux-worker"
    if len(name) <= 63:
        return name
    digest = sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{name[:54].rstrip('-')}-{digest}"


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return tuple(result)


__all__ = [
    "PLACEHOLDER_IMAGE_DIGEST",
    "ProjectDeploymentError",
    "ProjectDeploymentPlan",
    "ProjectDeploymentPolicyIdentity",
    "ProjectDeploymentRenderedFile",
    "ProjectDeploymentRenderResult",
    "ProjectDeploymentSecretEnvRef",
    "ProjectDeploymentSecretFileRef",
    "ProjectDeploymentWorkerPlan",
    "build_project_deployment_plan",
    "is_placeholder_image_digest",
    "render_project_deployment_plan",
]
