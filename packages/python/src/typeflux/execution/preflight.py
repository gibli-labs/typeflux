from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from typeflux.core.artifacts import (
    ArtifactGroupPart,
    ArtifactPart,
    ProviderExtensionPart,
    TextPart,
)
from typeflux.core.contracts import AIActivity, PromptRef, ProviderParams, ResolvedPrompt
from typeflux.core.errors import TypefluxError
from typeflux.manifests import schema_hash
from typeflux.prompts import PromptRegistry
from typeflux.prompts.errors import PromptRegistryConfigError, PromptResolutionError
from typeflux.providers import (
    validate_artifact_kinds_supported,
    validate_provider_params_supported,
)
from typeflux.providers.errors import ProviderConfigError


@dataclass(frozen=True)
class PreflightFailure:
    activity_name: str
    prompt_ref: PromptRef
    error_type: str
    reason: str
    retryable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "activity_name": self.activity_name,
            "prompt_ref": _prompt_ref_payload(self.prompt_ref),
            "error_type": self.error_type,
            "reason": self.reason,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class PreflightResolvedPrompt:
    activity_name: str
    prompt_ref: PromptRef
    resolved_prompt_version: str | None
    provider_model: str | None
    temperature: float | None
    provider_params: dict[str, Any]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "activity_name": self.activity_name,
            "prompt_ref": _prompt_ref_payload(self.prompt_ref),
            "resolved_prompt_version": self.resolved_prompt_version,
            "provider_model": self.provider_model,
            "temperature": self.temperature,
            "provider_params": self.provider_params,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class PreflightReport:
    resolved: tuple[PreflightResolvedPrompt, ...]
    failures: tuple[PreflightFailure, ...]

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(warning for item in self.resolved for warning in item.warnings)

    def raise_for_failures(self) -> None:
        if self.failures:
            raise PreflightError(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "resolved": [item.to_dict() for item in self.resolved],
            "failures": [item.to_dict() for item in self.failures],
            "warnings": list(self.warnings),
        }


class PreflightError(TypefluxError, RuntimeError):
    def __init__(self, report: PreflightReport) -> None:
        self.report = report
        super().__init__(_report_message(report))


def preflight_ai_activities(
    *,
    activities: Sequence[Any],
    registry: PromptRegistry,
    provider: Any | str | None = None,
    provider_name: str | None = None,
    provider_default_params: ProviderParams | None = None,
) -> PreflightReport:
    resolved_items: list[PreflightResolvedPrompt] = []
    failures: list[PreflightFailure] = []

    # Preflight is AI-only by contract: plain Temporal activities carry no
    # prompt/provider configuration to validate.
    activities = [activity for activity in activities if isinstance(activity, AIActivity)]
    for activity in activities:
        try:
            resolved_prompt = registry.resolve(activity.prompt_ref)
            warnings = _validate_resolved_prompt(activity, resolved_prompt)
            provider_params = (provider_default_params or ProviderParams()).merge(
                resolved_prompt.provider_params,
                activity.provider_params,
            )
            validate_provider_params_supported(
                provider if provider is not None else provider_name,
                provider_params,
                activity_name=activity.name,
                prompt_name=activity.prompt_ref.name,
            )
            validate_artifact_kinds_supported(
                provider if provider is not None else provider_name,
                activity.artifact_inputs,
                activity_name=activity.name,
                prompt_name=activity.prompt_ref.name,
            )
        except PromptResolutionError as exc:
            failures.append(_failure_from_error(activity, exc))
            continue
        except ProviderConfigError as exc:
            failures.append(
                PreflightFailure(
                    activity_name=activity.name,
                    prompt_ref=activity.prompt_ref,
                    error_type=type(exc).__name__,
                    reason=exc.reason,
                    retryable=False,
                )
            )
            continue
        except Exception as exc:
            failures.append(
                PreflightFailure(
                    activity_name=activity.name,
                    prompt_ref=activity.prompt_ref,
                    error_type=type(exc).__name__,
                    reason=f"unexpected prompt preflight failure for {activity.name}",
                    retryable=False,
                )
            )
            continue

        resolved_items.append(
            PreflightResolvedPrompt(
                activity_name=activity.name,
                prompt_ref=activity.prompt_ref,
                resolved_prompt_version=resolved_prompt.resolved_version,
                provider_model=provider_params.model,
                temperature=provider_params.temperature,
                provider_params=provider_params.to_dict(),
                warnings=tuple(warnings),
            )
        )

    return PreflightReport(resolved=tuple(resolved_items), failures=tuple(failures))


def _validate_resolved_prompt(activity: AIActivity, resolved_prompt: ResolvedPrompt) -> list[str]:
    if not resolved_prompt.messages:
        raise PromptRegistryConfigError(
            f"resolved prompt for {activity.name} contained no chat messages",
            ref=activity.prompt_ref,
        )
    for message in resolved_prompt.messages:
        _validate_message_content(activity, message.content)
    if resolved_prompt.model is not None and not isinstance(resolved_prompt.model, str):
        raise PromptRegistryConfigError(
            f"resolved prompt model for {activity.name} must be a string",
            ref=activity.prompt_ref,
        )
    if resolved_prompt.temperature is not None and not isinstance(
        resolved_prompt.temperature, (int, float)
    ):
        raise PromptRegistryConfigError(
            f"resolved prompt temperature for {activity.name} must be numeric",
            ref=activity.prompt_ref,
        )

    warnings: list[str] = []
    config = resolved_prompt.metadata.get("langfuse.prompt_config")
    if config is None:
        return warnings
    if not isinstance(config, Mapping):
        raise PromptRegistryConfigError(
            f"resolved prompt config for {activity.name} must be a mapping",
            ref=activity.prompt_ref,
        )

    contracts = _nested_mapping(config, "typeflux", "contracts")
    if contracts is None:
        return warnings

    expected_input_hash = schema_hash(activity.input_type)
    expected_output_hash = schema_hash(activity.output_type)
    _validate_contract_hash(
        activity=activity,
        field="input_schema_hash",
        actual=contracts.get("input_schema_hash"),
        expected=expected_input_hash,
    )
    _validate_contract_hash(
        activity=activity,
        field="output_schema_hash",
        actual=contracts.get("output_schema_hash"),
        expected=expected_output_hash,
    )
    return warnings


def _validate_message_content(activity: AIActivity, content: Any) -> None:
    if isinstance(content, str):
        return
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        valid_part_types = (TextPart, ArtifactPart, ArtifactGroupPart, ProviderExtensionPart)
        if all(isinstance(part, valid_part_types) for part in content):
            return
    raise PromptRegistryConfigError(
        f"resolved prompt for {activity.name} contained unsupported message content",
        ref=activity.prompt_ref,
    )


def _validate_contract_hash(
    *,
    activity: AIActivity,
    field: str,
    actual: Any,
    expected: str,
) -> None:
    if actual is None:
        return
    if actual != expected:
        raise PromptRegistryConfigError(
            f"prompt config {field} mismatch for {activity.name}",
            ref=activity.prompt_ref,
        )


def _nested_mapping(payload: Mapping[str, Any], *path: str) -> Mapping[str, Any] | None:
    current: Any = payload
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current if isinstance(current, Mapping) else None


def _failure_from_error(activity: AIActivity, error: PromptResolutionError) -> PreflightFailure:
    return PreflightFailure(
        activity_name=activity.name,
        prompt_ref=activity.prompt_ref,
        error_type=type(error).__name__,
        reason=error.reason,
        retryable=error.retryable,
    )


def _prompt_ref_payload(ref: PromptRef) -> dict[str, Any]:
    return {"name": ref.name, "version": ref.version, "label": ref.label}


def _report_message(report: PreflightReport) -> str:
    failures = ", ".join(
        f"{failure.activity_name}: {failure.error_type}" for failure in report.failures
    )
    return f"Typeflux preflight failed ({failures})"


__all__ = [
    "PreflightError",
    "PreflightFailure",
    "PreflightReport",
    "PreflightResolvedPrompt",
    "preflight_ai_activities",
]
