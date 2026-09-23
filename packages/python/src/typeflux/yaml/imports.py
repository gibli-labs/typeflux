from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import timedelta
from importlib import import_module
from types import ModuleType
from typing import Any

from typeflux.core.contracts import (
    ActivityDefinitionSource,
    AIActivity,
    ModerationConfig,
    PromptRef,
    ProviderParams,
    SessionCacheConfig,
    TemporalActivityDescriptor,
    YamlWorkflowActivity,
)
from typeflux.moderation import gemini_moderator, openai_moderator
from typeflux.yaml.spec import (
    ActivityDefinitionSpec,
    ActivityModuleSpec,
    ImportPolicySpec,
    ModerationSpec,
    PromptRefSpec,
    TypefluxYamlSpec,
)

_ACTIVITY_EXPORTS = ("TYPEFLUX_ACTIVITIES", "ALL_ACTIVITIES")


def collect_activities(spec: TypefluxYamlSpec) -> dict[str, YamlWorkflowActivity]:
    activities: dict[str, YamlWorkflowActivity] = {}
    for module_spec in spec.activities.modules:
        validate_activity_module_import(
            project=spec.project,
            module_spec=module_spec,
            policy=spec.runtime.imports,
        )
        module_name = _module_name(spec.project, module_spec)
        module = import_module(module_name)
        module_activities, export_source = _activities_from_module(module)
        for value in module_activities:
            definition_source = ActivityDefinitionSource(
                kind="python",
                module=module_name,
                export=export_source,
            )
            activity: YamlWorkflowActivity
            if isinstance(value, AIActivity):
                activity = replace(value, definition_source=definition_source)
            else:
                activity = _temporal_descriptor_from_callable(
                    value,
                    definition_source=definition_source,
                )
            if not _included(activity, module_spec):
                continue
            if activity.name in activities:
                raise ValueError(f"duplicate activity name: {activity.name}")
            activities[activity.name] = activity
    for definition in spec.activities.definitions:
        yaml_activity = _activity_from_definition(
            spec.project, definition, yaml_name=spec.name, policy=spec.runtime.imports
        )
        if yaml_activity.name in activities:
            raise ValueError(f"duplicate activity name: {yaml_activity.name}")
        activities[yaml_activity.name] = yaml_activity
    return activities


def ai_activities(activities: Iterable[YamlWorkflowActivity]) -> tuple[AIActivity, ...]:
    """Filter to prompt/provider-backed activities; plain Temporal activities
    never enter AI preflight, rollups, or provider-params validation."""
    return tuple(activity for activity in activities if isinstance(activity, AIActivity))


def _temporal_descriptor_from_callable(
    value: Any,
    *,
    definition_source: ActivityDefinitionSource,
) -> TemporalActivityDescriptor:
    from temporalio.activity import _Definition

    definition = _Definition.from_callable(value)
    if definition is None:  # pragma: no cover - guarded by discovery filters.
        raise TypeError(f"not a Temporal activity definition: {value!r}")
    name = definition.name
    if not isinstance(name, str) or not name:
        raise TypeError(
            "Temporal activities in YAML workflows must have a static name; "
            f"dynamic activity definitions are not supported: {value!r}"
        )
    arg_types = definition.arg_types
    if arg_types is None or len(arg_types) != 1:
        raise TypeError(
            f"Temporal activity {name!r} must declare exactly one typed input "
            "parameter so YAML workflow graph validation can check the "
            "input/output chain"
        )
    ret_type = definition.ret_type
    if ret_type is None:
        raise TypeError(
            f"Temporal activity {name!r} must declare a typed return value so "
            "YAML workflow graph validation can check the input/output chain"
        )
    return TemporalActivityDescriptor(
        name=name,
        input_type=arg_types[0],
        output_type=ret_type,
        activity=value,
        definition_source=definition_source,
    )


def _moderation_from_spec(
    project: str, moderation: ModerationSpec | None, *, policy: ImportPolicySpec
) -> ModerationConfig | None:
    """Resolve a YAML ``moderation:`` block into a core ModerationConfig (#158).

    ``provider: openai``/``gemini`` uses a built-in moderator (no Python) — the
    built-in path bypasses the ``allow_moderator_callable`` import gate because it
    is not a callable import. A custom ``moderator: module:callable`` is gated by
    the imports policy (arbitrary code from YAML) exactly like a ``type: custom``
    extension class.
    """
    if moderation is None:
        return None
    if moderation.provider == "openai":
        moderator = (
            openai_moderator(model=moderation.model) if moderation.model else openai_moderator()
        )
    elif moderation.provider == "gemini":
        moderator = (
            gemini_moderator(model=moderation.model) if moderation.model else gemini_moderator()
        )
    else:
        validate_extension_class_import(
            project=project,
            class_path=moderation.moderator,
            policy=policy,
            allow_flag="allow_moderator_callable",
            field="activity moderation.moderator",
        )
        assert moderation.moderator is not None  # validated by the spec model
        moderator = import_object(moderation.moderator)
        if not callable(moderator):
            raise TypeError(
                f"moderation.moderator did not resolve to a callable: {moderation.moderator}"
            )
    return ModerationConfig(moderator=moderator, on_violation=moderation.on_violation)


def _activity_from_definition(
    project: str, definition: ActivityDefinitionSpec, *, yaml_name: str, policy: ImportPolicySpec
) -> AIActivity:
    return AIActivity(
        name=definition.name,
        input_type=import_type_ref(project, definition.input),
        output_type=import_type_ref(project, definition.output),
        prompt_ref=_prompt_ref(definition.prompt),
        validation_retries=definition.validation_retries,
        start_to_close_timeout=(
            timedelta(seconds=definition.start_to_close_timeout_seconds)
            if definition.start_to_close_timeout_seconds is not None
            else None
        ),
        heartbeat_timeout=(
            timedelta(seconds=definition.heartbeat_timeout_seconds)
            if definition.heartbeat_timeout_seconds is not None
            else None
        ),
        retry_policy=(definition.retry.to_retry_policy() if definition.retry is not None else None),
        artifact_inputs=tuple(artifact.to_artifact_input() for artifact in definition.artifacts),
        provider_params=(
            definition.provider_params.to_provider_params()
            if definition.provider_params is not None
            else ProviderParams()
        ),
        session_cache=(
            SessionCacheConfig(
                enabled=definition.cache.enabled,
                ttl_seconds=definition.cache.ttl_seconds,
            )
            if definition.cache is not None
            else None
        ),
        moderation=_moderation_from_spec(project, definition.moderation, policy=policy),
        definition_source=ActivityDefinitionSource(
            kind="yaml",
            yaml_project=project,
            yaml_name=yaml_name,
        ),
    )


def import_type_ref(project: str, ref: str) -> type:
    module_name, _, attr = ref.partition(":")
    if not module_name or not attr:
        raise ValueError(f"type reference must use module:Name syntax: {ref}")
    module = import_module(_type_module_name(project, module_name))
    value: Any = module
    for part in attr.split("."):
        value = getattr(value, part)
    if not isinstance(value, type):
        raise TypeError(f"type reference did not resolve to a type: {ref}")
    return value


def import_object(path: str) -> Any:
    module_name, _, attr = path.partition(":")
    if not module_name or not attr:
        raise ValueError(f"object reference must use module:Name syntax: {path}")
    module = import_module(module_name)
    value: Any = module
    for part in attr.split("."):
        value = getattr(value, part)
    return value


def validate_activity_module_import(
    *,
    project: str,
    module_spec: ActivityModuleSpec,
    policy: ImportPolicySpec,
) -> None:
    if not module_spec.absolute:
        return
    if not policy.allow_absolute_activity_modules:
        raise ValueError(
            "activities.modules[].absolute requires "
            "runtime.imports.allow_absolute_activity_modules: true"
        )
    _validate_allowed_module_root(
        project=project,
        module_name=module_spec.module,
        allowed_roots=policy.allowed_module_roots,
        field="activities.modules[].module",
    )


def validate_extension_class_import(
    *,
    project: str,
    class_path: str | None,
    policy: ImportPolicySpec,
    allow_flag: str,
    field: str,
) -> None:
    """Gate a `type: custom` extension class import (#189).

    Shared by provider, prompt-registry, and observability custom classes:
    each requires its own ``runtime.imports.<allow_flag>`` to be enabled and
    must resolve to a module inside the project or an approved
    ``allowed_module_roots`` entry.
    """
    if class_path is None:
        return
    if not getattr(policy, allow_flag):
        raise ValueError(f"{field} requires runtime.imports.{allow_flag}: true")
    module_name, _, attr = class_path.partition(":")
    if not module_name or not attr:
        raise ValueError(f"{field} must use module:Name syntax: {class_path}")
    _validate_allowed_module_root(
        project=project,
        module_name=module_name,
        allowed_roots=policy.allowed_module_roots,
        field=field,
    )


def validate_extension_imports(spec: TypefluxYamlSpec) -> None:
    """Validate every `type: custom` extension-class import for a spec (#189)."""
    for class_path, allow_flag, field in (
        (spec.runtime.provider.provider_class, "allow_provider_class", "runtime.provider.class"),
        (spec.runtime.registry.registry_class, "allow_registry_class", "runtime.registry.class"),
        (
            spec.runtime.observability.backend_class,
            "allow_observability_class",
            "runtime.observability.class",
        ),
    ):
        validate_extension_class_import(
            project=spec.project,
            class_path=class_path,
            policy=spec.runtime.imports,
            allow_flag=allow_flag,
            field=field,
        )


def _module_name(project: str, module_spec: ActivityModuleSpec) -> str:
    if module_spec.absolute:
        return module_spec.module
    return f"{project}.{module_spec.module}"


def _prompt_ref(prompt: str | PromptRefSpec) -> PromptRef:
    if isinstance(prompt, str):
        return PromptRef(prompt)
    return PromptRef(
        name=prompt.name,
        version=prompt.version,
        label=prompt.label,
        prompt_type=prompt.type,
    )


def _type_module_name(project: str, module_name: str) -> str:
    if module_name == project or module_name.startswith(f"{project}."):
        return module_name
    return f"{project}.{module_name}"


def _validate_allowed_module_root(
    *,
    project: str,
    module_name: str,
    allowed_roots: list[str],
    field: str,
) -> None:
    if _module_in_root(module_name, project):
        return
    for root in allowed_roots:
        if _module_in_root(module_name, root):
            return
    raise ValueError(
        f"{field} imports {module_name!r}, which is outside project {project!r}; "
        "add an approved root to runtime.imports.allowed_module_roots"
    )


def _module_in_root(module_name: str, root: str) -> bool:
    return module_name == root or module_name.startswith(f"{root}.")


def _activities_from_module(module: ModuleType) -> tuple[Iterable[Any], str]:
    for export_name in _ACTIVITY_EXPORTS:
        if hasattr(module, export_name):
            exported = getattr(module, export_name)
            return _coerce_activity_sequence(exported, export_name), export_name
    return tuple(
        value
        for value in module.__dict__.values()
        if isinstance(value, AIActivity) or _is_temporal_activity(value)
    ), "module_values"


def _is_temporal_activity(value: Any) -> bool:
    return callable(value) and hasattr(value, "__temporal_activity_definition")


def _coerce_activity_sequence(exported: Any, export_name: str) -> tuple[Any, ...]:
    if isinstance(exported, Mapping):
        values = tuple(exported.values())
    else:
        values = tuple(exported)
    for value in values:
        if not isinstance(value, AIActivity) and not _is_temporal_activity(value):
            raise TypeError(
                f"{export_name} contains a value that is neither an AIActivity nor a "
                f"@temporalio.activity.defn callable: {value!r}"
            )
    return values


def _included(activity: YamlWorkflowActivity, module_spec: ActivityModuleSpec) -> bool:
    include = set(module_spec.include or [])
    exclude = set(module_spec.exclude)
    if include and activity.name not in include:
        return False
    return activity.name not in exclude


__all__ = [
    "collect_activities",
    "import_object",
    "import_type_ref",
    "validate_activity_module_import",
    "validate_extension_class_import",
    "validate_extension_imports",
]
