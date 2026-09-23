from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Mapping
from inspect import isawaitable, iscoroutinefunction
from threading import Lock
from typing import Any, NoReturn

from pydantic import BaseModel, ValidationError

from typeflux.core.artifacts import (
    ARTIFACT_KINDS,
    ArtifactGroupPart,
    ArtifactPart,
    ProviderExtensionPart,
    ResolvedArtifact,
    ResolvedArtifactGroup,
    TextPart,
)
from typeflux.core.contracts import CachedSessionHandle, ChatMessage, ProviderParams
from typeflux.env import load_env
from typeflux.prompts.context import langfuse_prompt_from_context
from typeflux.providers._shared import (
    artifact_for_name,
    artifacts_for_group,
    is_network_or_timeout_error,
    matches_provider_error,
    raise_if_truncated,
    retry_after_seconds,
    status_code,
)
from typeflux.providers.base import ProviderUsage, validate_provider_params_supported
from typeflux.providers.errors import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTransientError,
)


class OpenAIProvider:
    provider_name = "openai"
    supported_provider_params = frozenset(
        {
            "model",
            "temperature",
            "max_tokens",
            "top_p",
            "stop",
            "seed",
            "timeout",
            "frequency_penalty",
            "presence_penalty",
        }
    )
    # Artifact-kind capability stated as the gap: this provider cannot ingest
    # audio, video, or archive. Everything else is accepted at preflight subject
    # to the finer runtime media-type/source gating in _to_openai_artifact_part
    # (which also decides the ambiguous external_uri/other kinds).
    supported_artifact_kinds = ARTIFACT_KINDS - frozenset({"audio", "video", "archive"})
    # Provider-side session caching (#60). OpenAI is "prefix" style and caches
    # prompt prefixes *automatically* (no markers, no cache object) — so the
    # runtime re-sends the full prefix and OpenAI bills matched tokens at cache
    # rates. prepare/release are logical no-ops (no warm-up, per the ratified
    # design); the only observable effect is cached_tokens in usage.
    supports_session_cache = True
    session_cache_style = "prefix"

    def __init__(
        self,
        *,
        default_model: str = "gpt-4o-mini",
        api_key: str | None = None,
        base_url: str | None = None,
        enable_langfuse: bool = False,
        instructor_client: Any | None = None,
        instructor_max_retries: int = 0,
        strict: bool = True,
        default_provider_params: ProviderParams | None = None,
    ) -> None:
        self.default_model = default_model
        self.api_key = api_key
        self.base_url = base_url
        self.enable_langfuse = enable_langfuse
        self.instructor_max_retries = instructor_max_retries
        self.strict = strict
        self.default_provider_params = ProviderParams(model=default_model).merge(
            default_provider_params
        )
        validate_provider_params_supported(self, self.default_provider_params)
        self._async_client: Any | None = None
        self._async_client_lock = Lock()
        self._injected_client = instructor_client is not None

        if instructor_client is not None:
            self._client = instructor_client
            return

        load_env()

        try:
            import instructor
        except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
            raise RuntimeError("instructor is required for OpenAIProvider") from exc

        if enable_langfuse:
            try:
                from langfuse.openai import OpenAI
            except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
                raise RuntimeError("langfuse is required when enable_langfuse=True") from exc
        else:
            try:
                from openai import OpenAI
            except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
                raise RuntimeError("openai is required for OpenAIProvider") from exc

        client_kwargs: dict[str, Any] = {}
        if api_key is not None:
            client_kwargs["api_key"] = api_key
        if base_url is not None:
            client_kwargs["base_url"] = base_url
        self._client = instructor.from_openai(
            OpenAI(**client_kwargs), mode=instructor.Mode.JSON_SCHEMA
        )

    @property
    def supports_async_structured_call(self) -> bool:
        if not self._injected_client:
            return True
        # Probe the method the async path actually invokes (create_with_completion),
        # not create — they can differ on an injected client.
        call = getattr(
            getattr(getattr(self._client, "chat", None), "completions", None),
            "create_with_completion",
            None,
        )
        return callable(call) and iscoroutinefunction(call)

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        provider_params: ProviderParams | None = None,
        metadata: dict[str, Any] | None = None,
        artifacts: tuple[ResolvedArtifactGroup, ...] = (),
        observation_context: dict[str, Any] | None = None,
        usage_sink: Callable[[ProviderUsage], None] | None = None,
        cached_session: CachedSessionHandle | None = None,
    ) -> BaseModel:
        # cached_session needs no request change: OpenAI prefix-caches the
        # re-sent prefix automatically; the hit shows up as cached_tokens below.
        del cached_session
        params = self.default_provider_params.merge(
            provider_params,
            ProviderParams(model=model, temperature=temperature),
        )
        validate_provider_params_supported(self, params)
        call_kwargs = _openai_call_kwargs(
            messages=messages,
            artifacts=artifacts,
            output_schema=output_schema,
            params=params,
            metadata=metadata,
            observation_context=observation_context,
            instructor_max_retries=self.instructor_max_retries,
            strict=self.strict,
            include_trace_name=self.enable_langfuse,
        )

        try:
            result, completion = self._client.chat.completions.create_with_completion(**call_kwargs)
        except Exception as exc:
            _raise_openai_provider_error(exc)

        # Outside the try: a truncation is a config error, not a transient/SDK
        # error to be reclassified by _raise_openai_provider_error.
        _raise_if_openai_truncated(completion)
        _forward_openai_usage(completion, model=params.model, usage_sink=usage_sink)
        return _validated_provider_result(result)

    async def async_structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        provider_params: ProviderParams | None = None,
        metadata: dict[str, Any] | None = None,
        artifacts: tuple[ResolvedArtifactGroup, ...] = (),
        observation_context: dict[str, Any] | None = None,
        usage_sink: Callable[[ProviderUsage], None] | None = None,
        cached_session: CachedSessionHandle | None = None,
    ) -> BaseModel:
        del cached_session  # implicit prefix caching; see structured_call
        params = self.default_provider_params.merge(
            provider_params,
            ProviderParams(model=model, temperature=temperature),
        )
        validate_provider_params_supported(self, params)
        call_kwargs = _openai_call_kwargs(
            messages=messages,
            artifacts=artifacts,
            output_schema=output_schema,
            params=params,
            metadata=metadata,
            observation_context=observation_context,
            instructor_max_retries=self.instructor_max_retries,
            strict=self.strict,
            include_trace_name=self.enable_langfuse,
        )
        client = await self._get_async_client_async()
        try:
            pending = client.chat.completions.create_with_completion(**call_kwargs)
            if not isawaitable(pending):
                raise ProviderConfigError(
                    "injected OpenAI instructor client does not support async structured calls",
                    provider="openai",
                )
            result, completion = await pending
        except Exception as exc:
            _raise_openai_provider_error(exc)

        _raise_if_openai_truncated(completion)
        _forward_openai_usage(completion, model=params.model, usage_sink=usage_sink)
        return _validated_provider_result(result)

    def prepare_cached_session(
        self,
        *,
        messages: list[ChatMessage],
        artifacts: tuple[ResolvedArtifactGroup, ...] = (),
        model: str | None = None,
        provider_params: ProviderParams | None = None,
        identity_hash: str,
        ttl_seconds: int | None = None,
    ) -> CachedSessionHandle:
        """Logical marker only: OpenAI prefix-caches automatically with no cache
        object and no warm-up call (ratified), so this makes no API request. The
        cache "hit" surfaces as ``cached_tokens`` once the prefix is re-sent."""
        del messages, artifacts, ttl_seconds
        resolved_model = self.default_provider_params.merge(
            provider_params, ProviderParams(model=model)
        ).model
        return CachedSessionHandle(
            provider=self.provider_name,
            identity_hash=identity_hash,
            supported=True,
            style="prefix",
            cache_id=None,
            model=resolved_model,
            # Implicit prefix cache, provider-managed retention — no TTL is sent,
            # so don't record one the provider never received.
            ttl_seconds=None,
        )

    def release_cached_session(self, handle: CachedSessionHandle) -> None:
        """No-op: OpenAI's implicit prefix cache has no object to delete."""
        del handle

    def _get_async_client(self) -> Any:
        if self._injected_client:
            return self._client
        with self._async_client_lock:
            if self._async_client is not None:
                return self._async_client

            load_env()

            try:
                import instructor
            except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
                raise RuntimeError("instructor is required for OpenAIProvider") from exc

            if self.enable_langfuse:
                try:
                    from langfuse.openai import AsyncOpenAI
                except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
                    raise RuntimeError("langfuse is required when enable_langfuse=True") from exc
            else:
                try:
                    from openai import AsyncOpenAI
                except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
                    raise RuntimeError("openai is required for OpenAIProvider") from exc

            client_kwargs: dict[str, Any] = {}
            if self.api_key is not None:
                client_kwargs["api_key"] = self.api_key
            if self.base_url is not None:
                client_kwargs["base_url"] = self.base_url
            self._async_client = instructor.from_openai(
                AsyncOpenAI(**client_kwargs), mode=instructor.Mode.JSON_SCHEMA
            )
            return self._async_client

    async def _get_async_client_async(self) -> Any:
        if self._injected_client or self._async_client is not None:
            return self._get_async_client()
        return await asyncio.to_thread(self._get_async_client)


def _openai_call_kwargs(
    *,
    messages: list[ChatMessage],
    artifacts: tuple[ResolvedArtifactGroup, ...] = (),
    output_schema: type[BaseModel],
    params: ProviderParams,
    metadata: dict[str, Any] | None,
    observation_context: dict[str, Any] | None,
    instructor_max_retries: int,
    strict: bool,
    include_trace_name: bool,
) -> dict[str, Any]:
    call_kwargs: dict[str, Any] = {
        "model": params.model,
        "messages": [_to_openai_message(message, artifacts=artifacts) for message in messages],
        "response_model": output_schema,
        "max_retries": instructor_max_retries,
        "strict": strict,
    }
    if params.temperature is not None:
        call_kwargs["temperature"] = params.temperature
    if params.max_tokens is not None:
        call_kwargs["max_tokens"] = params.max_tokens
    if params.top_p is not None:
        call_kwargs["top_p"] = params.top_p
    if params.stop:
        call_kwargs["stop"] = list(params.stop)
    if params.seed is not None:
        call_kwargs["seed"] = params.seed
    if params.timeout is not None:
        call_kwargs["timeout"] = params.timeout
    if params.frequency_penalty is not None:
        call_kwargs["frequency_penalty"] = params.frequency_penalty
    if params.presence_penalty is not None:
        call_kwargs["presence_penalty"] = params.presence_penalty
    if metadata is not None and include_trace_name:
        call_kwargs["metadata"] = metadata
        activity_name = _metadata_activity_name(metadata)
        call_kwargs["name"] = (
            f"{activity_name}.openai"
            if activity_name is not None
            else "typeflux-ai-activity.openai"
        )
        prompt = langfuse_prompt_from_context(observation_context)
        if prompt is not None:
            call_kwargs["langfuse_prompt"] = prompt
    return call_kwargs


def _metadata_activity_name(metadata: Mapping[str, Any]) -> str | None:
    typeflux = metadata.get("typeflux")
    if isinstance(typeflux, Mapping):
        activity_name = typeflux.get("activity_name")
        if isinstance(activity_name, str):
            return activity_name
    # Legacy flat join key emitted by older Typeflux versions.
    legacy = metadata.get("typeflux.activity_name")
    return legacy if isinstance(legacy, str) else None


def _raise_if_openai_truncated(completion: Any) -> None:
    # Mirror Anthropic's stop_reason == "max_tokens" guard: a response cut off by
    # the output-token cap (finish_reason == "length") is a degraded result, not a
    # success. instructor parses the truncated text into the schema (or burns
    # validation retries on invalid JSON) and would otherwise hide the cause. When
    # no raw completion is attached we can't read the reason, so the existing
    # validation-repair path stays the backstop.
    choices = getattr(completion, "choices", None)
    finish_reason = getattr(choices[0], "finish_reason", None) if choices else None
    raise_if_truncated(
        truncated=finish_reason == "length", provider="openai", display_name="OpenAI"
    )


def _forward_openai_usage(
    completion: Any,
    *,
    model: str | None,
    usage_sink: Callable[[ProviderUsage], None] | None,
) -> None:
    # Mirror Anthropic/Gemini: forward token counts through the sink so usage
    # reaches any observer (#340), not only the langfuse.openai instrumentation.
    # Called after the truncation guard, so a truncated response reports no usage.
    if usage_sink is None:
        return
    usage = _openai_usage(completion, model=model)
    if usage is not None:
        usage_sink(usage)


def _openai_usage(completion: Any, *, model: str | None) -> ProviderUsage | None:
    usage = getattr(completion, "usage", None)
    if usage is None and isinstance(completion, dict):
        usage = completion.get("usage")
    if usage is None:
        return None
    input_tokens = _usage_int(usage, "prompt_tokens")
    output_tokens = _usage_int(usage, "completion_tokens")
    total_tokens = _usage_int(usage, "total_tokens")
    # Implicit prefix-cache hit (#60): cached_tokens lives under
    # prompt_tokens_details (an object or dict).
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None and isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")
    cache_read = _usage_int(details, "cached_tokens") if details is not None else None
    if all(v is None for v in (input_tokens, output_tokens, total_tokens, cache_read)):
        return None
    return ProviderUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        model=model,
        cache_read_tokens=cache_read,
    )


def _usage_int(usage: Any, field: str) -> int | None:
    value = getattr(usage, field, None)
    if value is None and isinstance(usage, dict):
        value = usage.get(field)
    return value if isinstance(value, int) else None


def _validated_provider_result(result: Any) -> BaseModel:
    if not isinstance(result, BaseModel):
        raise ProviderConfigError(
            "instructor client returned a non-Pydantic result",
            provider="openai",
        )
    return result


def _raise_openai_provider_error(exc: Exception) -> NoReturn:
    validation_error = _extract_instructor_validation_error(exc)
    if validation_error is not None:
        raise validation_error from exc
    if isinstance(exc, ProviderError):
        raise exc
    raise _classify_provider_error(exc, provider="openai") from exc


def _to_openai_message(
    message: ChatMessage,
    *,
    artifacts: tuple[ResolvedArtifactGroup, ...] = (),
) -> dict[str, Any]:
    content = message.content
    payload: dict[str, Any] = {
        "role": message.role,
        "content": (
            content
            if isinstance(content, str)
            else _to_openai_content_parts(content, artifacts=artifacts)
        ),
    }
    if message.name is not None:
        payload["name"] = message.name
    return payload


def _to_openai_content_parts(
    parts: tuple[Any, ...],
    *,
    artifacts: tuple[ResolvedArtifactGroup, ...],
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, TextPart):
            payload.append({"type": "text", "text": part.text})
        elif isinstance(part, ArtifactGroupPart):
            if part.text is not None:
                payload.append({"type": "text", "text": part.text})
            for artifact in artifacts_for_group(artifacts, part.group, provider="openai"):
                payload.append(_to_openai_artifact_part(artifact))
        elif isinstance(part, ArtifactPart):
            if part.text is not None:
                payload.append({"type": "text", "text": part.text})
            payload.append(
                _to_openai_artifact_part(
                    artifact_for_name(artifacts, part.artifact, provider="openai")
                )
            )
        elif isinstance(part, ProviderExtensionPart):
            if part.provider != "openai":
                raise ProviderConfigError(
                    f"content part provider extension {part.provider!r} is not for OpenAI",
                    provider="openai",
                )
            payload.append(dict(part.payload))
        else:
            raise ProviderConfigError(
                f"unsupported OpenAI content part: {type(part).__name__}",
                provider="openai",
            )
    return payload


def _to_openai_artifact_part(artifact: ResolvedArtifact) -> dict[str, Any]:
    source = artifact.ref.source
    if artifact.kind == "image":
        if source.type == "url":
            return {"type": "image_url", "image_url": {"url": source.url}}
        if source.type == "local_path" and artifact.local_path is not None:
            media_type = artifact.media_type or "image/png"
            encoded = base64.b64encode(artifact.local_path.read_bytes()).decode("ascii")
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{encoded}"},
            }
    if source.type == "local_path" and artifact.local_path is not None:
        if _is_text_like_media_type(artifact.media_type):
            return {"type": "text", "text": artifact.local_path.read_text(encoding="utf-8")}
        if artifact.media_type == "application/pdf":
            encoded = base64.b64encode(artifact.local_path.read_bytes()).decode("ascii")
            return {
                "type": "file",
                "file": {
                    "filename": artifact.local_path.name,
                    "file_data": f"data:application/pdf;base64,{encoded}",
                },
            }
    if source.type == "provider_file" and source.provider == "openai":
        return {
            "type": "file",
            "file": {
                "file_id": source.file_id,
            },
        }
    raise ProviderConfigError(
        "OpenAIProvider cannot attach this artifact through the chat-completions path",
        provider="openai",
    )


def _is_text_like_media_type(media_type: str | None) -> bool:
    return media_type is not None and (
        media_type.startswith("text/")
        or media_type in {"application/json", "application/xml", "text/csv"}
    )


def _extract_instructor_validation_error(exc: Exception) -> ValidationError | None:
    failed_attempts = getattr(exc, "failed_attempts", None)
    if not failed_attempts:
        return exc if isinstance(exc, ValidationError) else None

    for attempt in reversed(failed_attempts):
        attempt_exception = getattr(attempt, "exception", None)
        if isinstance(attempt_exception, ValidationError):
            return attempt_exception
    return None


def _classify_provider_error(exc: Exception, *, provider: str) -> ProviderError:
    code = status_code(exc)
    reason = f"{provider} structured call failed"

    if matches_provider_error(exc, "openai", "RateLimitError") or code == 429:
        return ProviderRateLimitError(
            reason,
            provider=provider,
            status_code=code,
            original=exc,
            retry_after_seconds=retry_after_seconds(exc),
        )
    if matches_provider_error(
        exc, "openai", "AuthenticationError", "PermissionDeniedError"
    ) or code in {401, 403}:
        return ProviderAuthError(reason, provider=provider, status_code=code, original=exc)
    if matches_provider_error(
        exc, "openai", "BadRequestError", "NotFoundError", "UnprocessableEntityError"
    ) or code in {400, 404, 422}:
        return ProviderConfigError(reason, provider=provider, status_code=code, original=exc)
    if (
        matches_provider_error(
            exc, "openai", "APIConnectionError", "APITimeoutError", "InternalServerError"
        )
        or code in {408, 409}
        or (code is not None and code >= 500)
        or is_network_or_timeout_error(exc)
    ):
        return ProviderTransientError(
            reason,
            provider=provider,
            status_code=code,
            original=exc,
            retry_after_seconds=retry_after_seconds(exc),
        )
    return ProviderError(
        reason=reason, provider=provider, retryable=False, status_code=code, original=exc
    )


__all__ = ["OpenAIProvider"]
