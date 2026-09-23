from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from typeflux.core.artifacts import (
    ResolvedArtifactGroup,
    artifact_groups_summary,
)
from typeflux.core.contracts import (
    ActivityDefinitionSource,
    AIActivity,
    ChatMessage,
    PromptRef,
    ProviderParams,
    ResolvedPrompt,
)
from typeflux.manifests._common import canonical_json, drop_none, schema_identity
from typeflux.manifests.hashing import messages_hash
from typeflux.prompts.errors import PromptResolutionError


@dataclass(frozen=True)
class AIActivityManifest:
    activity_name: str
    input_schema_name: str
    input_schema_hash: str
    output_schema_name: str
    output_schema_hash: str
    prompt_ref: PromptRef
    resolved_prompt_version: str | None
    provider_model: str | None
    hook_name: str | None
    manifest_hash: str
    provider_params: dict[str, Any] = field(default_factory=dict)
    artifact_inputs: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ActivityExecutionManifest:
    activity_name: str
    activity_manifest_hash: str
    definition_source: ActivityDefinitionSource
    input_schema_module: str | None
    input_schema_name: str
    input_schema_hash: str
    output_schema_module: str | None
    output_schema_name: str
    output_schema_hash: str
    prompt_ref: PromptRef
    resolved_prompt_version: str | None
    prompt_messages_hash: str
    rendered_messages_hash: str
    provider_model: str | None
    temperature: float | None
    hook_name: str | None
    validation_attempt: int
    provider_model_source: str | None = None
    start_to_close_timeout_seconds: float | None = None
    provider_params: dict[str, Any] = field(default_factory=dict)
    input_schema_module_status: str | None = None
    input_schema_module_warning: str | None = None
    output_schema_module_status: str | None = None
    output_schema_module_warning: str | None = None
    manifest_version: str = "1"
    manifest_hash: str = ""
    artifact_inputs: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return drop_none(
            {
                "manifest_version": self.manifest_version,
                "manifest_hash": self.manifest_hash,
                "activity_name": self.activity_name,
                "activity_manifest_hash": self.activity_manifest_hash,
                "definition_source": self.definition_source.to_dict(),
                "input_schema": drop_none(
                    {
                        "module": self.input_schema_module,
                        "name": self.input_schema_name,
                        "hash": self.input_schema_hash,
                        "module_status": self.input_schema_module_status,
                        "module_warning": self.input_schema_module_warning,
                    }
                ),
                "output_schema": drop_none(
                    {
                        "module": self.output_schema_module,
                        "name": self.output_schema_name,
                        "hash": self.output_schema_hash,
                        "module_status": self.output_schema_module_status,
                        "module_warning": self.output_schema_module_warning,
                    }
                ),
                "prompt_ref": self.prompt_ref.to_dict(),
                "resolved_prompt_version": self.resolved_prompt_version,
                "prompt_messages_hash": self.prompt_messages_hash,
                "rendered_messages_hash": self.rendered_messages_hash,
                "provider_model": self.provider_model,
                "provider_model_source": self.provider_model_source,
                "temperature": self.temperature,
                "provider_params": self.provider_params or None,
                "start_to_close_timeout_seconds": self.start_to_close_timeout_seconds,
                "hook_name": self.hook_name,
                "artifact_inputs": list(self.artifact_inputs) or None,
                "artifacts": list(self.artifacts) or None,
                "validation_attempt": self.validation_attempt,
            }
        )


@dataclass(frozen=True)
class AIInvocationContext:
    temporal_namespace: str | None
    temporal_workflow_type: str | None
    temporal_workflow_id: str | None
    temporal_run_id: str | None
    temporal_activity_type: str | None
    temporal_activity_id: str | None
    temporal_activity_attempt: int | None
    typeflux_activity_name: str
    typeflux_manifest_hash: str
    temporal_task_queue: str | None = None
    map_step_id: str | None = None
    map_index: int | None = None
    map_size: int | None = None
    map_concurrency: int | None = None
    #: Subject id(s) this execution processes (#715 slice 1), inherited from the
    #: workflow's ``TypefluxSubjectIds`` index. The carrier that lets a cross-run
    #: cache WRITE record which subjects produced an entry (``build_cache_record``)
    #: so a later per-subject invalidation can target it.
    subject_ids: tuple[str, ...] = ()


def build_activity_manifest(
    activity: AIActivity,
    resolved_prompt: ResolvedPrompt,
    provider_params: ProviderParams | None = None,
) -> AIActivityManifest:
    hook_name = None
    if activity.hook is not None:
        hook_name = f"{activity.hook.__module__}.{activity.hook.__qualname__}"

    input_identity = schema_identity(activity.input_type)
    output_identity = schema_identity(activity.output_type)
    artifact_inputs = tuple(item.safe_definition() for item in activity.artifact_inputs)
    effective_provider_params = _effective_provider_params(resolved_prompt, provider_params)
    behavior_provider_params = effective_provider_params.behavior_dict()
    base = {
        "activity_name": activity.name,
        "input_schema_name": input_identity.name,
        "input_schema_hash": input_identity.hash,
        "output_schema_name": output_identity.name,
        "output_schema_hash": output_identity.hash,
        "prompt_ref": activity.prompt_ref.to_dict(),
        "resolved_prompt_version": resolved_prompt.resolved_version,
        "provider_model": effective_provider_params.model,
        **({"provider_params": behavior_provider_params} if behavior_provider_params else {}),
        "hook_name": hook_name,
        **({"artifact_inputs": artifact_inputs} if artifact_inputs else {}),
    }
    manifest_hash = sha256(canonical_json(base).encode("utf-8")).hexdigest()
    return AIActivityManifest(
        activity_name=base["activity_name"],
        input_schema_name=base["input_schema_name"],
        input_schema_hash=base["input_schema_hash"],
        output_schema_name=base["output_schema_name"],
        output_schema_hash=base["output_schema_hash"],
        prompt_ref=activity.prompt_ref,
        resolved_prompt_version=resolved_prompt.resolved_version,
        provider_model=effective_provider_params.model,
        provider_params=behavior_provider_params,
        hook_name=hook_name,
        artifact_inputs=artifact_inputs,
        manifest_hash=manifest_hash,
    )


def provider_model_source(
    activity: AIActivity,
    resolved_prompt: ResolvedPrompt,
) -> str:
    # Activity-level provider params are YAML/control-plane configuration;
    # only a model supplied by the resolved prompt itself counts as
    # prompt_config.
    if activity.provider_params.model is not None:
        return "yaml_provider"
    if resolved_prompt.provider_params.model is not None:
        return "prompt_config"
    return "yaml_provider"


def build_activity_execution_manifest(
    *,
    activity: AIActivity,
    activity_manifest: AIActivityManifest,
    resolved_prompt: ResolvedPrompt,
    rendered_messages: Sequence[ChatMessage],
    artifact_groups: Sequence[ResolvedArtifactGroup] = (),
    validation_attempt: int,
    provider_model: str | None = None,
    provider_params: ProviderParams | None = None,
) -> ActivityExecutionManifest:
    effective_provider_params = _effective_provider_params(
        resolved_prompt,
        provider_params,
        provider_model=provider_model,
    )
    resolved_provider_model = effective_provider_params.model
    resolved_provider_model_source = provider_model_source(activity, resolved_prompt)
    input_identity = schema_identity(activity.input_type)
    output_identity = schema_identity(activity.output_type)
    artifact_inputs = tuple(item.safe_definition() for item in activity.artifact_inputs)
    artifacts = tuple(artifact_groups_summary(artifact_groups))
    execution_provider_params = effective_provider_params.to_dict()
    start_to_close_timeout_seconds = _timeout_seconds(activity)
    base = {
        "activity_name": activity.name,
        "activity_manifest_hash": activity_manifest.manifest_hash,
        "definition_source": activity.definition_source.to_dict(),
        **input_identity.to_flat_dict("input_schema"),
        **output_identity.to_flat_dict("output_schema"),
        "prompt_ref": activity.prompt_ref.to_dict(),
        "resolved_prompt_version": resolved_prompt.resolved_version,
        "prompt_messages_hash": messages_hash(resolved_prompt.messages),
        "rendered_messages_hash": messages_hash(rendered_messages),
        "provider_model": resolved_provider_model,
        "provider_model_source": resolved_provider_model_source,
        "temperature": effective_provider_params.temperature,
        **({"provider_params": execution_provider_params} if execution_provider_params else {}),
        "start_to_close_timeout_seconds": start_to_close_timeout_seconds,
        "hook_name": activity_manifest.hook_name,
        **({"artifact_inputs": artifact_inputs} if artifact_inputs else {}),
        **({"artifacts": artifacts} if artifacts else {}),
        "validation_attempt": validation_attempt,
        "manifest_version": "1",
    }
    manifest_hash = sha256(canonical_json(base).encode("utf-8")).hexdigest()
    return ActivityExecutionManifest(
        activity_name=activity.name,
        activity_manifest_hash=activity_manifest.manifest_hash,
        definition_source=activity.definition_source,
        input_schema_module=input_identity.module,
        input_schema_name=input_identity.name,
        input_schema_hash=input_identity.hash,
        output_schema_module=output_identity.module,
        output_schema_name=output_identity.name,
        output_schema_hash=output_identity.hash,
        prompt_ref=activity.prompt_ref,
        resolved_prompt_version=resolved_prompt.resolved_version,
        prompt_messages_hash=base["prompt_messages_hash"],
        rendered_messages_hash=base["rendered_messages_hash"],
        provider_model=resolved_provider_model,
        provider_model_source=resolved_provider_model_source,
        temperature=effective_provider_params.temperature,
        provider_params=execution_provider_params,
        start_to_close_timeout_seconds=start_to_close_timeout_seconds,
        hook_name=activity_manifest.hook_name,
        artifact_inputs=artifact_inputs,
        artifacts=artifacts,
        validation_attempt=validation_attempt,
        input_schema_module_status=input_identity.module_status,
        input_schema_module_warning=input_identity.module_warning,
        output_schema_module_status=output_identity.module_status,
        output_schema_module_warning=output_identity.module_warning,
        manifest_hash=manifest_hash,
    )


def build_activity_rollup_entry(
    *,
    activity: AIActivity,
    resolved_prompt: ResolvedPrompt,
    provider_model: str | None = None,
    provider_params: ProviderParams | None = None,
) -> dict[str, Any]:
    effective_provider_params = _effective_provider_params(
        resolved_prompt,
        provider_params,
        provider_model=provider_model,
    )
    activity_manifest = build_activity_manifest(
        activity,
        resolved_prompt,
        provider_params=effective_provider_params,
    )
    input_identity = schema_identity(activity.input_type)
    output_identity = schema_identity(activity.output_type)
    artifact_inputs = tuple(item.safe_definition() for item in activity.artifact_inputs)
    start_to_close_timeout_seconds = _timeout_seconds(activity)
    return drop_none(
        {
            "activity_name": activity.name,
            "activity_manifest_hash": activity_manifest.manifest_hash,
            "definition_source": activity.definition_source.to_dict(),
            "input_schema": input_identity.to_dict(),
            "output_schema": output_identity.to_dict(),
            "prompt_ref": activity.prompt_ref.to_dict(),
            "resolved_prompt_version": resolved_prompt.resolved_version,
            "prompt_messages_hash": messages_hash(resolved_prompt.messages),
            "provider_model": effective_provider_params.model,
            "provider_model_source": provider_model_source(activity, resolved_prompt),
            "temperature": effective_provider_params.temperature,
            "provider_params": effective_provider_params.to_dict() or None,
            "start_to_close_timeout_seconds": start_to_close_timeout_seconds,
            "hook_name": activity_manifest.hook_name,
            "artifact_inputs": list(artifact_inputs) or None,
        }
    )


def build_unresolved_activity_rollup_entry(
    *,
    activity: AIActivity,
    error: BaseException,
) -> dict[str, Any]:
    prompt_resolution = _prompt_resolution_failure_payload(error)
    input_identity = schema_identity(activity.input_type)
    output_identity = schema_identity(activity.output_type)
    return drop_none(
        {
            "activity_name": activity.name,
            "definition_source": activity.definition_source.to_dict(),
            "input_schema": input_identity.to_dict(),
            "output_schema": output_identity.to_dict(),
            "prompt_ref": activity.prompt_ref.to_dict(),
            "hook_name": (
                None
                if activity.hook is None
                else f"{activity.hook.__module__}.{activity.hook.__qualname__}"
            ),
            "artifact_inputs": [item.safe_definition() for item in activity.artifact_inputs]
            or None,
            "prompt_resolution": prompt_resolution,
        }
    )


def compact_activity_manifest(
    manifest: ActivityExecutionManifest | Mapping[str, Any],
) -> dict[str, Any]:
    payload = (
        manifest.to_dict() if isinstance(manifest, ActivityExecutionManifest) else dict(manifest)
    )
    return drop_none(
        {
            "activity_name": payload.get("activity_name"),
            "manifest_hash": payload.get("manifest_hash"),
            "activity_manifest_hash": payload.get("activity_manifest_hash"),
            "definition_source": payload.get("definition_source"),
            "input_schema": payload.get("input_schema"),
            "output_schema": payload.get("output_schema"),
            "prompt_ref": payload.get("prompt_ref"),
            "resolved_prompt_version": payload.get("resolved_prompt_version"),
            "prompt_messages_hash": payload.get("prompt_messages_hash"),
            "rendered_messages_hash": payload.get("rendered_messages_hash"),
            "provider_model": payload.get("provider_model"),
            "provider_model_source": payload.get("provider_model_source"),
            "temperature": payload.get("temperature"),
            "provider_params": payload.get("provider_params"),
            "hook_name": payload.get("hook_name"),
            "artifact_inputs": payload.get("artifact_inputs"),
            "artifacts": payload.get("artifacts"),
        }
    )


def merge_activity_rollup(
    workflow_manifest: Mapping[str, Any],
    activity_manifest: ActivityExecutionManifest | Mapping[str, Any],
) -> dict[str, Any]:
    activity = compact_activity_manifest(activity_manifest)
    activity_name = activity.get("activity_name")
    merged = dict(workflow_manifest)
    activities = []
    replaced = False
    for existing in workflow_manifest.get("activities") or ():
        if isinstance(existing, dict) and existing.get("activity_name") == activity_name:
            activities.append(activity)
            replaced = True
        elif isinstance(existing, str) and existing == activity_name:
            activities.append(activity)
            replaced = True
        else:
            activities.append(existing)
    if not replaced:
        activities.append(activity)
    merged["activities"] = activities
    return merged


def _prompt_resolution_failure_payload(error: BaseException) -> dict[str, Any]:
    if isinstance(error, PromptResolutionError):
        return error.to_metadata()
    return {
        "status": "failed",
        "error_type": type(error).__name__,
        "retryable": False,
        "reason": "unexpected prompt resolution failure",
    }


def _effective_provider_params(
    resolved_prompt: ResolvedPrompt,
    provider_params: ProviderParams | None,
    *,
    provider_model: str | None = None,
) -> ProviderParams:
    base = resolved_prompt.provider_params
    if provider_model is not None:
        base = base.merge(ProviderParams(model=provider_model))
    if provider_params is None:
        return base
    return base.merge(provider_params)


def _timeout_seconds(activity: AIActivity) -> float | None:
    if activity.start_to_close_timeout is None:
        return None
    return activity.start_to_close_timeout.total_seconds()


__all__ = [
    "ActivityExecutionManifest",
    "AIActivityManifest",
    "AIInvocationContext",
    "build_activity_execution_manifest",
    "build_activity_manifest",
    "build_activity_rollup_entry",
    "build_unresolved_activity_rollup_entry",
    "compact_activity_manifest",
    "merge_activity_rollup",
]
