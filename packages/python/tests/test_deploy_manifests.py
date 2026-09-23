from __future__ import annotations

from pathlib import Path

import yaml

# parents[3] = repo root (test is at packages/python/tests/); deploy/ is a
# repo-level asset that stays at the root, not under the Python package.
REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY_DIR = REPO_ROOT / "deploy" / "yaml-worker"


def _load_kubernetes_documents() -> list[dict[str, object]]:
    path = DEPLOY_DIR / "kubernetes.yaml"
    return [
        document for document in yaml.safe_load_all(path.read_text()) if isinstance(document, dict)
    ]


def _deployment() -> dict[str, object]:
    for document in _load_kubernetes_documents():
        if document.get("kind") == "Deployment":
            return document
    raise AssertionError("Deployment was not found")


def _config_map() -> dict[str, object]:
    for document in _load_kubernetes_documents():
        if document.get("kind") == "ConfigMap":
            return document
    raise AssertionError("ConfigMap was not found")


def _secret() -> dict[str, object]:
    for document in _load_kubernetes_documents():
        if document.get("kind") == "Secret":
            return document
    raise AssertionError("Secret was not found")


def _worker_container() -> dict[str, object]:
    deployment = _deployment()
    spec = deployment["spec"]
    assert isinstance(spec, dict)
    template = spec["template"]
    assert isinstance(template, dict)
    pod_spec = template["spec"]
    assert isinstance(pod_spec, dict)
    containers = pod_spec["containers"]
    assert isinstance(containers, list)
    for container in containers:
        assert isinstance(container, dict)
        if container.get("name") == "worker":
            return container
    raise AssertionError("worker container was not found")


def test_yaml_worker_dockerfile_runs_as_non_root_locked_runtime() -> None:
    dockerfile = (DEPLOY_DIR / "Dockerfile").read_text()

    assert "FROM python:3.12-slim-bookworm" in dockerfile
    assert "ghcr.io/astral-sh/uv:0.11.8" in dockerfile
    assert "useradd" in dockerfile
    assert "USER typeflux:typeflux" in dockerfile
    assert "/app/.venv/bin/python -m typeflux.yaml.run" in dockerfile
    assert 'CMD ["sh", "-c", "rm -f /tmp/typeflux-preflight-ok && ' in dockerfile
    assert "--preflight && touch /tmp/typeflux-preflight-ok" in dockerfile
    assert "&& exec /app/.venv/bin/python" in dockerfile
    assert "uv run" not in dockerfile


def test_kubernetes_worker_image_is_not_latest() -> None:
    container = _worker_container()

    image = container["image"]
    assert isinstance(image, str)
    assert not image.endswith(":latest")
    assert ":" in image.rsplit("/", 1)[-1]


def test_kubernetes_temporal_api_key_uses_secret_not_config_map() -> None:
    # The production-labeled reference manifest must stay TLS-forward; the
    # plaintext in-cluster variant lives only in comments for local dev.
    config_map = _config_map()
    data = config_map["data"]
    assert isinstance(data, dict)
    assert data["TEMPORAL_TLS"] == "true"
    assert data["TYPEFLUX_ENVIRONMENT"] == "production"
    assert data["TYPEFLUX_DEPLOYMENT_ID"] == "typeflux-yaml-worker"
    assert data["TYPEFLUX_TEMPORAL_REGION"] == "us-east"
    assert "TEMPORAL_API_KEY" not in data

    secret = _secret()
    string_data = secret["stringData"]
    assert isinstance(string_data, dict)
    assert "TEMPORAL_API_KEY" in string_data


def test_kubernetes_worker_has_pod_security_context() -> None:
    deployment = _deployment()
    spec = deployment["spec"]
    assert isinstance(spec, dict)
    template = spec["template"]
    assert isinstance(template, dict)
    pod_spec = template["spec"]
    assert isinstance(pod_spec, dict)

    security_context = pod_spec["securityContext"]
    assert isinstance(security_context, dict)
    assert security_context["runAsNonRoot"] is True
    assert security_context["runAsUser"] != 0
    assert security_context["runAsGroup"] != 0
    assert security_context["fsGroup"] != 0
    assert security_context["seccompProfile"] == {"type": "RuntimeDefault"}


def test_kubernetes_worker_has_container_security_context() -> None:
    container = _worker_container()

    security_context = container["securityContext"]
    assert isinstance(security_context, dict)
    assert security_context["allowPrivilegeEscalation"] is False
    assert security_context["readOnlyRootFilesystem"] is True
    assert security_context["capabilities"] == {"drop": ["ALL"]}


def test_kubernetes_worker_has_explicit_tmp_volume() -> None:
    deployment = _deployment()
    spec = deployment["spec"]
    assert isinstance(spec, dict)
    template = spec["template"]
    assert isinstance(template, dict)
    pod_spec = template["spec"]
    assert isinstance(pod_spec, dict)
    volumes = pod_spec["volumes"]
    assert isinstance(volumes, list)

    assert {"name": "tmp", "emptyDir": {}} in volumes

    container = _worker_container()
    volume_mounts = container["volumeMounts"]
    assert isinstance(volume_mounts, list)
    assert {"name": "tmp", "mountPath": "/tmp"} in volume_mounts


def test_kubernetes_worker_has_health_probes_and_shutdown_budget() -> None:
    deployment = _deployment()
    spec = deployment["spec"]
    assert isinstance(spec, dict)
    template = spec["template"]
    assert isinstance(template, dict)
    pod_spec = template["spec"]
    assert isinstance(pod_spec, dict)
    assert pod_spec["terminationGracePeriodSeconds"] >= 30

    container = _worker_container()
    assert "startupProbe" in container
    assert "readinessProbe" in container
    assert "livenessProbe" in container

    startup_probe = container["startupProbe"]
    assert isinstance(startup_probe, dict)
    exec_probe = startup_probe["exec"]
    assert isinstance(exec_probe, dict)
    startup_command = exec_probe["command"]
    assert isinstance(startup_command, list)
    assert startup_command[-1] == "test -f /tmp/typeflux-preflight-ok"

    lifecycle = container["lifecycle"]
    assert isinstance(lifecycle, dict)
    assert "preStop" in lifecycle

    resources = container["resources"]
    assert isinstance(resources, dict)
    assert "requests" in resources
    assert "limits" in resources


def test_compose_file_is_dev_only_and_avoids_latest_temporal() -> None:
    compose = yaml.safe_load((DEPLOY_DIR / "docker-compose.yml").read_text())

    services = compose["services"]
    assert isinstance(services, dict)
    temporal = services["temporal"]
    assert isinstance(temporal, dict)
    assert temporal["image"].startswith("temporalio/temporal@sha256:")
    assert ":latest" not in temporal["image"]

    yaml_worker = services["yaml-worker"]
    assert isinstance(yaml_worker, dict)
    environment = yaml_worker["environment"]
    assert isinstance(environment, dict)
    assert environment["TEMPORAL_TLS"] == "false"
    for service in (temporal, yaml_worker):
        labels = service["labels"]
        assert isinstance(labels, dict)
        assert labels["com.typeflux.scope"] == "dev-only"
