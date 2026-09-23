"""Provider-agnostic helpers shared by the built-in provider adapters.

These were duplicated across the OpenAI and Anthropic providers. They are
behavior-preserving extractions: error-classification leaf helpers and artifact
lookup, parametrized only by the provider name used in error messages and the
SDK module probed for error classes.
"""

from __future__ import annotations

from functools import cache

from typeflux.core.artifacts import ResolvedArtifact, ResolvedArtifactGroup
from typeflux.providers.errors import ProviderConfigError


def status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def retry_after_seconds(exc: Exception) -> float | None:
    for attr in ("retry_after", "retry_after_seconds"):
        value = getattr(exc, attr, None)
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after")
    except Exception:  # noqa: BLE001 - defensive against exotic header types.
        return None
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def matches_error_name(exc: Exception, *names: str) -> bool:
    expected = set(names)
    return any(cls.__name__ in expected for cls in type(exc).__mro__)


def is_network_or_timeout_error(exc: Exception) -> bool:
    if isinstance(exc, (OSError, TimeoutError)):
        return True
    return any(
        "connection" in cls.__name__.lower() or "timeout" in cls.__name__.lower()
        for cls in type(exc).__mro__
    )


@cache
def error_classes_from_module(module_name: str, *names: str) -> tuple[type[BaseException], ...]:
    """Resolve named exception classes from an optional provider SDK module."""
    try:
        module = __import__(module_name)
    except ModuleNotFoundError:
        return ()
    classes: list[type[BaseException]] = []
    for name in names:
        value = getattr(module, name, None)
        if isinstance(value, type) and issubclass(value, BaseException):
            classes.append(value)
    return tuple(classes)


def matches_provider_error(exc: Exception, module_name: str, *names: str) -> bool:
    if matches_error_name(exc, *names):
        return True
    classes = error_classes_from_module(module_name, *names)
    return bool(classes) and isinstance(exc, classes)


def raise_if_truncated(*, truncated: bool, provider: str, display_name: str) -> None:
    """Fail loud when a provider cut a response off at the output-token cap.

    The signal differs per provider (Anthropic ``stop_reason == "max_tokens"``,
    OpenAI ``finish_reason == "length"``) but the policy and the operator-facing
    message are shared: a truncated response is a config error, not a success or
    a transient error. Centralizing the message keeps the two providers from
    drifting (the portability contract's own no-silent-divergence principle).
    ``provider`` is the canonical id for the error; ``display_name`` is how the
    provider is named in the message (e.g. "OpenAI").
    """
    if not truncated:
        return
    raise ProviderConfigError(
        f"{display_name} response was truncated because max_tokens was exhausted; "
        "increase provider_params.max_tokens for this workflow or activity",
        provider=provider,
    )


def artifacts_for_group(
    groups: tuple[ResolvedArtifactGroup, ...],
    name: str,
    *,
    provider: str,
) -> tuple[ResolvedArtifact, ...]:
    for group in groups:
        if group.name == name:
            return group.artifacts
    raise ProviderConfigError(f"unknown artifact group: {name}", provider=provider)


def artifact_for_name(
    groups: tuple[ResolvedArtifactGroup, ...],
    name: str,
    *,
    provider: str,
) -> ResolvedArtifact:
    if "[" in name and name.endswith("]"):
        group_name, _, index_text = name[:-1].partition("[")
        try:
            index = int(index_text)
        except ValueError as exc:
            raise ProviderConfigError(
                f"invalid artifact reference: {name}", provider=provider
            ) from exc
        if index < 0:
            raise ProviderConfigError(f"invalid artifact reference: {name}", provider=provider)
        artifacts = artifacts_for_group(groups, group_name, provider=provider)
        try:
            return artifacts[index]
        except IndexError as exc:
            raise ProviderConfigError(
                f"unknown artifact reference: {name}", provider=provider
            ) from exc
    artifacts = artifacts_for_group(groups, name, provider=provider)
    if len(artifacts) != 1:
        raise ProviderConfigError(
            f"artifact reference {name!r} resolves to {len(artifacts)} artifacts; "
            "use group[index] syntax",
            provider=provider,
        )
    return artifacts[0]


__all__ = [
    "artifact_for_name",
    "artifacts_for_group",
    "error_classes_from_module",
    "is_network_or_timeout_error",
    "matches_error_name",
    "matches_provider_error",
    "raise_if_truncated",
    "retry_after_seconds",
    "status_code",
]
