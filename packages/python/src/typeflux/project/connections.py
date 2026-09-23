"""Registry/observability connection status, YAML-dictated (#258).

The control-plane server reports and probes the backends the workflow's
YAML configures — types and hosts only, never credentials. Probes are
single, cheap, and request-scoped: failures degrade to ``reachable:
false`` with a safe message.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ConfigDict

from typeflux.project.environment import (
    project_environment_context,
    resolve_project_workflow,
)
from typeflux.project.spec import TypefluxProjectSpec


class ConnectionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str
    host: str | None = None
    reachable: bool
    detail: str | None = None


class ObserverStatus(ConnectionStatus):
    execution_manifest: bool = False
    redaction_enabled: bool = False


class WorkflowConnections(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    environment_id: str
    registry: ConnectionStatus
    observability: ObserverStatus

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def workflow_connections(
    project: TypefluxProjectSpec,
    *,
    workflow_id: str,
    environment_id: str,
) -> WorkflowConnections:
    resolved = resolve_project_workflow(
        project,
        workflow_id=workflow_id,
        environment_id=environment_id,
    )
    registry_spec = resolved.spec.runtime.registry
    observability_spec = resolved.spec.runtime.observability

    with project_environment_context(resolved.application):
        registry = _probe(
            kind=registry_spec.type,
            configured_host=getattr(registry_spec, "host", None),
        )
        observer_type = getattr(observability_spec, "type", None) or "none"
        observer = _probe(kind=observer_type, configured_host=None)

    redaction = getattr(observability_spec, "redaction", None)
    return WorkflowConnections(
        workflow_id=workflow_id,
        environment_id=environment_id,
        registry=registry,
        observability=ObserverStatus(
            **observer.model_dump(),
            execution_manifest=bool(getattr(observability_spec, "execution_manifest", False)),
            redaction_enabled=bool(getattr(redaction, "enabled", False)),
        ),
    )


def _probe(*, kind: str, configured_host: str | None) -> ConnectionStatus:
    if kind != "langfuse":
        # inline registries and observer "none" have nothing to reach.
        return ConnectionStatus(type=kind, host=None, reachable=True)
    # One host resolution shared by the displayed value and the probe.
    host = configured_host or os.getenv("LANGFUSE_HOST") or os.getenv("LANGFUSE_BASE_URL")
    try:
        _langfuse_probe(host)
    except Exception as exc:  # noqa: BLE001 - degrade, never raise to the panel.
        return ConnectionStatus(
            type=kind,
            host=host,
            reachable=False,
            detail=f"probe failed: {exc}",
        )
    return ConnectionStatus(type=kind, host=host, reachable=True)


def _langfuse_probe(host: str | None) -> None:
    from typeflux.observability.inspect import TraceListQuery
    from typeflux.observability.langfuse import LangfuseObservabilityBackend

    backend = LangfuseObservabilityBackend.from_env(host=host)
    backend.reader.list_traces(TraceListQuery(limit=1))


__all__ = ["ConnectionStatus", "ObserverStatus", "WorkflowConnections", "workflow_connections"]
