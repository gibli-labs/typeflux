"""Prompt-registry drift (#254): label resolution vs last-run versions.

Label-pinned prompts are mutable by design (#192); this compares what a
label resolves to *now* against what the latest execution manifest says
*ran*. Drift requires both sides known — an unknown side reports
``unknown``, never a false in-sync. Versions and names only; never prompt
text.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from typeflux.core.contracts import AIActivity
from typeflux.project.environment import (
    project_environment_context,
    resolve_project_workflow,
)
from typeflux.project.spec import TypefluxProjectSpec
from typeflux.yaml.imports import collect_activities

PromptDriftStatus = Literal["in_sync", "drift", "unknown"]


class PromptStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    mode: Literal["pinned", "label", "inline"]
    selector: str
    registry_version: str | None = None
    last_run_version: str | None = None
    status: PromptDriftStatus
    used_by_activities: tuple[str, ...] = ()
    detail: str | None = None
    #: Inline registries only — the template text from the user's own YAML.
    #: Registry-managed prompt text never serializes.
    template: str | None = None


class WorkflowPromptStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    environment_id: str
    registry_type: str
    prompts: tuple[PromptStatus, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def workflow_prompt_status(
    project: TypefluxProjectSpec,
    *,
    workflow_id: str,
    environment_id: str,
) -> WorkflowPromptStatus:
    resolved = resolve_project_workflow(
        project,
        workflow_id=workflow_id,
        environment_id=environment_id,
    )
    registry_type = resolved.spec.runtime.registry.type
    with project_environment_context(resolved.application):
        activities = collect_activities(resolved.spec)
        refs: dict[str, tuple[Any, list[str]]] = {}
        for name in sorted(activities):
            activity = activities[name]
            if not isinstance(activity, AIActivity):
                continue
            ref = activity.prompt_ref
            entry = refs.setdefault(ref.name, (ref, []))
            entry[1].append(name)

        if registry_type == "inline":
            inline_templates = getattr(resolved.spec.runtime.registry, "prompts", {}) or {}
            prompts = tuple(
                PromptStatus(
                    name=ref.name,
                    mode="inline",
                    selector="inline",
                    status="in_sync",
                    used_by_activities=tuple(users),
                    template=(
                        _render_template(inline_templates[ref.name])
                        if ref.name in inline_templates
                        else None
                    ),
                )
                for ref, users in refs.values()
            )
            return WorkflowPromptStatus(
                workflow_id=workflow_id,
                environment_id=environment_id,
                registry_type=registry_type,
                prompts=prompts,
            )

        if registry_type in ("custom", "langsmith"):
            # A custom or LangSmith registry resolves at runtime (by class, or by
            # tag/commit) and exposes no statically introspectable templates or
            # versions; report the requested selector with an honest unknown drift
            # status rather than a fake inline body.
            prompts = tuple(
                PromptStatus(
                    name=ref.name,
                    mode="pinned" if ref.version is not None else "label",
                    selector=(f"v{ref.version}" if ref.version is not None else (ref.label or "—")),
                    status="unknown",
                    used_by_activities=tuple(users),
                )
                for ref, users in refs.values()
            )
            return WorkflowPromptStatus(
                workflow_id=workflow_id,
                environment_id=environment_id,
                registry_type=registry_type,
                prompts=prompts,
            )

        observer = getattr(resolved.spec.runtime.observability, "type", None) or "none"
        # Last-run versions come from manifests in the configured observer;
        # observer "none" means the last-run side is genuinely unknown.
        last_run = _last_run_versions(resolved.spec.workflow.name) if observer == "langfuse" else {}
        registry_spec = resolved.spec.runtime.registry
        default_label = getattr(registry_spec, "label", None) or "production"
        registry_host = getattr(registry_spec, "host", None)
        statuses: list[PromptStatus] = []
        for ref, users in refs.values():
            if ref.version is not None:
                statuses.append(
                    PromptStatus(
                        name=ref.name,
                        mode="pinned",
                        selector=f"v{ref.version}",
                        registry_version=str(ref.version),
                        last_run_version=last_run.get(ref.name),
                        status="in_sync",
                        used_by_activities=tuple(users),
                    )
                )
                continue
            label = ref.label if ref.label is not None else default_label
            current, detail = _registry_label_version(ref.name, label, host=registry_host)
            ran = last_run.get(ref.name)
            if current is None or ran is None:
                status: PromptDriftStatus = "unknown"
            elif str(current) != str(ran):
                status = "drift"
            else:
                status = "in_sync"
            statuses.append(
                PromptStatus(
                    name=ref.name,
                    mode="label",
                    selector=f"@{label}",
                    registry_version=str(current) if current is not None else None,
                    last_run_version=str(ran) if ran is not None else None,
                    status=status,
                    used_by_activities=tuple(users),
                    detail=detail,
                )
            )
        return WorkflowPromptStatus(
            workflow_id=workflow_id,
            environment_id=environment_id,
            registry_type=registry_type,
            prompts=tuple(statuses),
        )


def _render_template(value: Any) -> str:
    """Readable preview for inline templates: strings pass through;
    structured (chat) prompt specs render as YAML, never a model repr."""
    if isinstance(value, str):
        return value
    import yaml

    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    return yaml.safe_dump(value, sort_keys=False).strip()


def _registry_label_version(
    name: str, label: str, *, host: str | None = None
) -> tuple[Any, str | None]:
    try:
        from langfuse import Langfuse

        client = Langfuse(host=host) if host else Langfuse()
        prompt = client.get_prompt(name, label=label)
        return getattr(prompt, "version", None), None
    except Exception as exc:  # noqa: BLE001 - degrade to unknown, never raise.
        return None, f"registry lookup failed: {exc}"


def _last_run_versions(logical_workflow: str) -> dict[str, str]:
    """Prompt name -> resolved version recorded in the latest manifest."""
    try:
        from typeflux.manifests import reconstruct_execution_manifest
        from typeflux.observability.inspect import TraceSearchQuery, _trace_payload
        from typeflux.observability.langfuse import LangfuseObservabilityBackend

        backend = LangfuseObservabilityBackend.from_env()
        page = backend.reader.search_traces(
            TraceSearchQuery(workflow_name=logical_workflow, limit=1)
        )
        if not page.traces:
            return {}
        manifest = reconstruct_execution_manifest(_trace_payload(page.traces[0]))
        versions: dict[str, str] = {}
        for activity in manifest.activities:
            ref = activity.get("prompt_ref")
            name = ref.get("name") if isinstance(ref, dict) else str(ref).split("@")[0]
            version = activity.get("resolved_prompt_version")
            if isinstance(name, str) and version is not None:
                versions[name] = str(version)
        return versions
    except Exception:  # noqa: BLE001 - unknown side, never raise.
        return {}


__all__ = ["PromptStatus", "WorkflowPromptStatus", "workflow_prompt_status"]
