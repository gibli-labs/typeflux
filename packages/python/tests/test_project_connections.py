"""Direct unit tests for typeflux.project.connections."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import make_minimal_project
from typeflux.project import connections as connections_module
from typeflux.project import load_project_spec, workflow_connections
from typeflux.project.connections import ConnectionStatus


def test_probe_short_circuits_for_backends_with_nothing_to_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _must_not_probe(host: str | None) -> None:
        raise AssertionError("only langfuse backends are probed")

    monkeypatch.setattr(connections_module, "_langfuse_probe", _must_not_probe)

    for kind in ("inline", "none", "custom"):
        status = connections_module._probe(kind=kind, configured_host=None)
        assert status == ConnectionStatus(type=kind, host=None, reachable=True)


def test_probe_host_falls_back_to_langfuse_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    probed: list[str | None] = []
    monkeypatch.setattr(connections_module, "_langfuse_probe", lambda host: probed.append(host))
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://lf-base.example")

    status = connections_module._probe(kind="langfuse", configured_host=None)

    assert status.reachable is True
    # LANGFUSE_BASE_URL is the fallback when LANGFUSE_HOST is unset; the probe
    # receives the same host the response displays.
    assert status.host == "https://lf-base.example"
    assert probed == ["https://lf-base.example"]

    # LANGFUSE_HOST outranks LANGFUSE_BASE_URL when both are set.
    monkeypatch.setenv("LANGFUSE_HOST", "https://lf-host.example")
    assert connections_module._probe(kind="langfuse", configured_host=None).host == (
        "https://lf-host.example"
    )


def test_workflow_connections_propagates_observer_manifest_and_redaction_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = make_minimal_project(
        tmp_path,
        monkeypatch,
        "connections_unit_project",
        observability_block=(
            "            type: langfuse\n"
            "            execution_manifest: false\n"
            "            redaction:\n"
            "              enabled: false"
        ),
    )
    monkeypatch.setattr(connections_module, "_langfuse_probe", lambda host: None)
    project = load_project_spec(manifest)

    status = workflow_connections(project, workflow_id="workflow", environment_id="local")

    assert status.registry.type == "inline"
    assert status.registry.reachable is True
    assert status.observability.type == "langfuse"
    assert status.observability.reachable is True
    assert status.observability.execution_manifest is False
    assert status.observability.redaction_enabled is False


def test_workflow_connections_reports_default_observer_flags_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = make_minimal_project(
        tmp_path,
        monkeypatch,
        "connections_unit_project",
        observability_block="            type: langfuse",
    )
    monkeypatch.setattr(connections_module, "_langfuse_probe", lambda host: None)
    project = load_project_spec(manifest)

    status = workflow_connections(project, workflow_id="workflow", environment_id="local")

    # YAML defaults: manifests on, redaction on.
    assert status.observability.execution_manifest is True
    assert status.observability.redaction_enabled is True


def test_connection_status_model_is_strict() -> None:
    status = ConnectionStatus(type="inline", reachable=True)
    assert status.host is None
    assert status.detail is None
    assert ConnectionStatus.model_validate(status.model_dump()) == status

    with pytest.raises(ValidationError):
        ConnectionStatus.model_validate({"type": "inline", "reachable": True, "token": "x"})
