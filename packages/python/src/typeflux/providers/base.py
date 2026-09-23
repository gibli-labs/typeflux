from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from typeflux.core.artifacts import ArtifactInput, ArtifactKind, ResolvedArtifactGroup
from typeflux.core.contracts import CachedSessionHandle, ChatMessage, ProviderParams
from typeflux.providers.errors import ProviderConfigError

_PROVIDER_PARAM_SUPPORT_BY_NAME: dict[str, frozenset[str]] = {}
_PROVIDER_ARTIFACT_SUPPORT_BY_NAME: dict[str, frozenset[str]] = {}


@dataclass(frozen=True)
class ProviderUsage:
    """Token usage reported by a provider for one structured call.

    Providers report usage through the optional ``usage_sink`` callback on
    ``structured_call``/``async_structured_call``; the executor forwards the
    last reported usage to the surrounding generation observation. Cost stays
    derived by the observability backend from the generation's model plus
    these token counts — Typeflux does no cost math.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    model: str | None = None
    #: Provider-side cache accounting for session caching (#60). ``cache_read``
    #: is input tokens served from a cached prefix (a cache *hit*); ``cache_write``
    #: is tokens written creating/refreshing the cache (a *miss*/first call).
    #: None ⇒ the provider did not report cache usage for this call.
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None

    @property
    def cache_hit(self) -> bool | None:
        """True if any input was served from cache, False if cache was used but
        only written, None if the provider reported no cache accounting."""
        if self.cache_read_tokens is None and self.cache_write_tokens is None:
            return None
        return bool(self.cache_read_tokens)

    def usage_details(self) -> dict[str, int]:
        details: dict[str, int] = {}
        if self.input_tokens is not None:
            details["input"] = self.input_tokens
        if self.output_tokens is not None:
            details["output"] = self.output_tokens
        total = self.total_tokens
        if total is None and self.input_tokens is not None and self.output_tokens is not None:
            total = self.input_tokens + self.output_tokens
        if total is not None:
            details["total"] = total
        if self.cache_read_tokens is not None:
            details["cache_read"] = self.cache_read_tokens
        if self.cache_write_tokens is not None:
            details["cache_write"] = self.cache_write_tokens
        return details


class ModelProvider(Protocol):
    """Structured model caller.

    ``metadata`` is observability metadata prepared by Typeflux. Implementations
    should forward it to provider/tracing SDKs as metadata and avoid mutating it.

    The protocol below is the *required* contract. The executor additionally
    detects optional capabilities by inspecting the call signature and the
    provider object, and only uses each one when present — a minimal provider
    that implements just this protocol works, and capabilities are opt-in:

    Optional ``structured_call`` keyword parameters (signature-detected; the
    executor passes each only if the callable accepts it):

    - ``provider_params: ProviderParams | None`` — full merged sampling/limit
      params. Required in practice once non-legacy params (anything beyond
      ``model``/``temperature``) are configured; the executor raises if params
      are configured but the provider cannot accept them.
    - ``artifacts: tuple[ResolvedArtifactGroup, ...]`` — resolved activity
      artifacts. Required in practice when the activity declares artifacts.
    - ``observation_context: dict[str, Any]`` — process-local observability
      context (e.g. the native Langfuse prompt handle). Never persisted.
    - ``usage_sink: Callable[[ProviderUsage], None]`` — per-call token-usage
      reporting; the executor forwards the last reported usage to the
      generation observation.

    Optional provider attributes (``getattr``-probed):

    - ``provider_name: str`` — stable identity for policy enforcement,
      metadata, and per-provider rate limits.
    - ``default_provider_params: ProviderParams`` — provider-level defaults
      merged under prompt- and activity-level params.
    - ``default_model: str`` — fallback when ``default_provider_params`` is
      absent.
    - ``supported_provider_params: Iterable[str]`` — params accepted by this
      provider; used by preflight/runtime validation to fail unsupported
      params early.
    - ``supports_async_structured_call: bool`` — gates whether asyncio
      workers prefer ``async_structured_call``.
    """

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
        artifacts: tuple[ResolvedArtifactGroup, ...] = (),
    ) -> BaseModel: ...


class AsyncModelProvider(Protocol):
    """Async structured model caller.

    Implement this only when the underlying provider SDK path is truly
    non-blocking. Temporal asyncio workers prefer this capability when present.
    The same optional capability parameters and attributes documented on
    :class:`ModelProvider` apply to ``async_structured_call``.
    """

    async def async_structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
        artifacts: tuple[ResolvedArtifactGroup, ...] = (),
    ) -> BaseModel: ...


def provider_default_params(provider: Any) -> ProviderParams:
    configured = getattr(provider, "default_provider_params", None)
    if isinstance(configured, ProviderParams):
        return configured
    default_model = getattr(provider, "default_model", None)
    return ProviderParams(model=default_model if isinstance(default_model, str) else None)


def register_provider_param_support(
    provider_name: str,
    supported_params: Iterable[str],
) -> None:
    if not provider_name:
        raise ValueError("provider_name must be non-empty")
    _PROVIDER_PARAM_SUPPORT_BY_NAME[provider_name] = frozenset(supported_params)


def _resolve_capability(
    provider: Any | str | None,
    attr_name: str,
    registry: dict[str, frozenset[str]],
) -> frozenset[str] | None:
    """A provider's declared capability: the ``getattr``-probed attribute, else
    the by-name registry. ``None`` means "undeclared" — callers must skip the
    check rather than treat it as "supports nothing"."""
    if provider is None:
        return None
    configured = getattr(provider, attr_name, None)
    if configured is not None:
        return frozenset(configured)
    provider_name = _provider_name(provider)
    if provider_name is None:
        return None
    return registry.get(provider_name)


def supported_provider_params(provider: Any | str | None) -> frozenset[str] | None:
    return _resolve_capability(
        provider, "supported_provider_params", _PROVIDER_PARAM_SUPPORT_BY_NAME
    )


def validate_provider_params_supported(
    provider: Any | str | None,
    params: ProviderParams,
    *,
    activity_name: str | None = None,
    prompt_name: str | None = None,
) -> None:
    provider_name = _provider_name(provider)
    supported = supported_provider_params(provider)
    if supported is None:
        return
    unsupported = sorted(set(params.configured_keys) - supported)
    if not unsupported:
        return
    context = ""
    if activity_name is not None:
        context += f" for activity {activity_name!r}"
    if prompt_name is not None:
        context += f" prompt {prompt_name!r}"
    raise ProviderConfigError(
        f"provider {provider_name!r} does not support provider param(s)"
        f"{context}: {', '.join(unsupported)}",
        provider=provider_name,
    )


def register_provider_artifact_support(
    provider_name: str,
    supported_kinds: Iterable[ArtifactKind],
) -> None:
    if not provider_name:
        raise ValueError("provider_name must be non-empty")
    _PROVIDER_ARTIFACT_SUPPORT_BY_NAME[provider_name] = frozenset(supported_kinds)


def supported_artifact_kinds(provider: Any | str | None) -> frozenset[str] | None:
    """The artifact kinds a provider can ingest, or ``None`` when unknown.

    Mirrors :func:`supported_provider_params`: an optional, ``getattr``-probed
    ``supported_artifact_kinds`` attribute on the provider, falling back to the
    name registry. ``None`` means "no capability declared" — callers must skip
    the check rather than reject everything.
    """
    return _resolve_capability(
        provider, "supported_artifact_kinds", _PROVIDER_ARTIFACT_SUPPORT_BY_NAME
    )


def validate_artifact_kinds_supported(
    provider: Any | str | None,
    artifact_inputs: Iterable[ArtifactInput],
    *,
    activity_name: str | None = None,
    prompt_name: str | None = None,
) -> None:
    """Fail loud when an activity declares an artifact kind a provider can't ingest.

    Conservative by design: only an explicitly-declared ``ArtifactInput.kind`` is
    checked here (at preflight/validate). An undeclared kind is inferred from the
    artifact's media type and source at runtime, where the provider's fail-closed
    content mapping still gates the finer source/media-type divergences.
    """
    supported = supported_artifact_kinds(provider)
    if supported is None:
        return
    provider_name = _provider_name(provider)
    for artifact_input in artifact_inputs:
        kind = artifact_input.kind
        if kind is None or kind in supported:
            continue
        context = ""
        if activity_name is not None:
            context += f" for activity {activity_name!r}"
        if prompt_name is not None:
            context += f" prompt {prompt_name!r}"
        raise ProviderConfigError(
            f"provider {provider_name!r} cannot ingest artifact kind {kind!r} "
            f"(artifact input {artifact_input.name!r}){context}; "
            f"supported kinds: {', '.join(sorted(supported))}",
            provider=provider_name,
        )


def supports_session_cache(provider: Any | str | None) -> bool:
    """Whether a provider exposes the opt-in provider-side session cache (#60).

    ``getattr``-probed ``supports_session_cache`` attribute; default ``False`` so
    providers that don't implement it keep today's behavior (every call sends the
    full context). Opt-in and never assumed, like the other capabilities here.

    When True, the provider additionally implements:

    - ``prepare_cached_session(*, messages, artifacts, model, provider_params,
      identity_hash, ttl_seconds) -> CachedSessionHandle`` — create the
      provider-side cache over the stable prefix and return a serializable
      handle (the non-deterministic call; Typeflux runs it in its own activity).
    - ``structured_call(..., cached_session: CachedSessionHandle | None)`` —
      send the small per-item input against the cached prefix; report cache
      hit/miss + cached-token counts via the usage sink.
    - ``release_cached_session(handle: CachedSessionHandle) -> None`` — explicit
      cleanup where the provider needs it (TTL is the fail-soft default).
    """
    if provider is None:
        return False
    return bool(getattr(provider, "supports_session_cache", False))


def no_session_cache_handle(
    provider: Any | str | None,
    *,
    identity_hash: str,
    model: str | None = None,
) -> CachedSessionHandle:
    """The fail-soft fallback handle: no provider-side cache, full context per call.

    Lets the runtime uniformly "prepare" a session and thread a handle into every
    item call even when the provider can't cache — the ``supported=False`` handle
    just means each call sends the full prefix (today's behavior)."""
    return CachedSessionHandle(
        provider=_provider_name(provider) or "unknown",
        identity_hash=identity_hash,
        supported=False,
        model=model,
    )


def _provider_name(provider: Any | str | None) -> str | None:
    if provider is None:
        return None
    if isinstance(provider, str):
        return provider
    configured = getattr(provider, "provider_name", None)
    return configured if isinstance(configured, str) else None


__all__ = [
    "AsyncModelProvider",
    "ModelProvider",
    "ProviderUsage",
    "no_session_cache_handle",
    "provider_default_params",
    "register_provider_artifact_support",
    "register_provider_param_support",
    "supported_artifact_kinds",
    "supported_provider_params",
    "supports_session_cache",
    "validate_artifact_kinds_supported",
    "validate_provider_params_supported",
]
