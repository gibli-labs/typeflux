from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from inspect import isawaitable
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

_SUPPORTED_IMAGE_MEDIA_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}


class AnthropicProvider:
    provider_name = "anthropic"
    supported_provider_params = frozenset(
        {
            "model",
            "temperature",
            "max_tokens",
            "top_p",
            "top_k",
            "stop",
            "timeout",
        }
    )
    # Artifact-kind capability stated as the gap: this provider cannot ingest
    # audio, video, or archive. Everything else is accepted at preflight subject
    # to the finer runtime media-type/source gating in _to_anthropic_artifact_part
    # (which also decides the ambiguous external_uri/other kinds).
    supported_artifact_kinds = ARTIFACT_KINDS - frozenset({"audio", "video", "archive"})
    # Provider-side session caching (#60). Anthropic prompt caching is "prefix"
    # style: there is no server-side cache object to reference, so the runtime
    # re-sends the stable prefix every call and the provider marks it with
    # ``cache_control: ephemeral`` — billed at cache-read rates on a hit.
    supports_session_cache = True
    session_cache_style = "prefix"

    def __init__(
        self,
        *,
        default_model: str = "claude-sonnet-4-6",
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 4096,
        default_provider_params: ProviderParams | None = None,
        anthropic_client: Any | None = None,
        async_anthropic_client: Any | None = None,
    ) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")
        self.default_model = default_model
        self.api_key = api_key
        self.base_url = base_url
        self.max_tokens = max_tokens
        self.default_provider_params = ProviderParams(
            model=default_model,
            max_tokens=max_tokens,
        ).merge(default_provider_params)
        validate_provider_params_supported(self, self.default_provider_params)
        self.max_tokens = self.default_provider_params.max_tokens or max_tokens
        self._client = anthropic_client
        self._async_client = async_anthropic_client
        self._async_client_lock = Lock()
        # Any injected client (sync or async) means the caller controls client
        # construction: never import the SDK, load env, or build the missing
        # counterpart implicitly.
        self._injected_client = anthropic_client is not None or async_anthropic_client is not None

        if self._injected_client:
            return

        load_env()
        try:
            from anthropic import Anthropic
        except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
            raise RuntimeError("anthropic is required for AnthropicProvider") from exc

        self._client = Anthropic(**_anthropic_client_kwargs(api_key=api_key, base_url=base_url))

    @property
    def supports_async_structured_call(self) -> bool:
        return self._async_client is not None or not self._injected_client

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
        del metadata, observation_context
        client = self._client
        if client is None:
            raise ProviderConfigError(
                "injected Anthropic async client does not support sync structured calls",
                provider="anthropic",
            )
        params = self.default_provider_params.merge(
            provider_params,
            ProviderParams(model=model, temperature=temperature),
        )
        validate_provider_params_supported(self, params)

        call_kwargs = _anthropic_call_kwargs(
            messages=messages,
            artifacts=artifacts,
            output_schema=output_schema,
            params=params,
            cache_prefix=_should_cache_prefix(cached_session),
            prefix_stable_messages=_prefix_stable_count(cached_session),
            per_item_artifact_messages=_prefix_has_per_item_artifacts(cached_session),
        )
        try:
            parse = getattr(client.messages, "parse", None)
            if callable(parse):
                result = parse(**call_kwargs)
            else:
                result = client.messages.create(**_anthropic_create_kwargs(call_kwargs))
        except Exception as exc:
            _raise_anthropic_provider_error(exc)

        return _validated_provider_result(
            result,
            output_schema,
            usage_sink=usage_sink,
            model=params.model,
        )

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
        del metadata, observation_context
        client = await self._get_async_client_async()
        params = self.default_provider_params.merge(
            provider_params,
            ProviderParams(model=model, temperature=temperature),
        )
        validate_provider_params_supported(self, params)
        call_kwargs = _anthropic_call_kwargs(
            messages=messages,
            artifacts=artifacts,
            output_schema=output_schema,
            params=params,
            cache_prefix=_should_cache_prefix(cached_session),
            prefix_stable_messages=_prefix_stable_count(cached_session),
            per_item_artifact_messages=_prefix_has_per_item_artifacts(cached_session),
        )
        try:
            parse = getattr(client.messages, "parse", None)
            if callable(parse):
                result = parse(**call_kwargs)
            else:
                result = client.messages.create(**_anthropic_create_kwargs(call_kwargs))
            if not isawaitable(result):
                raise ProviderConfigError(
                    "Anthropic async client returned a non-awaitable result",
                    provider="anthropic",
                )
            result = await result
        except Exception as exc:
            _raise_anthropic_provider_error(exc)

        return _validated_provider_result(
            result,
            output_schema,
            usage_sink=usage_sink,
            model=params.model,
        )

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
        """Prepare a cached session over the stable prefix (#60).

        Prefix-style: there is no server-side cache object to create up front —
        the prefix is re-sent and marked ``cache_control: ephemeral`` on each
        call. So this just returns a ``supported`` handle (no API call, no
        ``cache_id``); ``structured_call(cached_session=...)`` does the marking.
        ``created_at`` is left for the runtime's prep activity to stamp
        deterministically (replay-safe).
        """
        del messages, artifacts, ttl_seconds  # prefix re-supplied per call
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
            # ttl is provider-managed for prefix style — Anthropic's ephemeral
            # cache uses its own default (~5m) and the requested ttl_seconds is
            # not sent here, so don't record a TTL the provider never received.
            ttl_seconds=None,
        )

    def release_cached_session(self, handle: CachedSessionHandle) -> None:
        """No-op: Anthropic's ephemeral prompt cache is TTL-managed with no
        explicit delete API, so there is nothing to release."""
        del handle

    def _get_async_client(self) -> Any:
        if self._async_client is not None:
            return self._async_client
        if self._injected_client:
            raise ProviderConfigError(
                "injected Anthropic client does not support async structured calls",
                provider="anthropic",
            )
        with self._async_client_lock:
            if self._async_client is not None:
                return self._async_client

            load_env()
            try:
                from anthropic import AsyncAnthropic
            except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
                raise RuntimeError("anthropic is required for AnthropicProvider") from exc

            self._async_client = AsyncAnthropic(
                **_anthropic_client_kwargs(api_key=self.api_key, base_url=self.base_url)
            )
            return self._async_client

    async def _get_async_client_async(self) -> Any:
        if self._async_client is not None:
            return self._async_client
        return await asyncio.to_thread(self._get_async_client)


def _anthropic_client_kwargs(*, api_key: str | None, base_url: str | None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if api_key is not None:
        kwargs["api_key"] = api_key
    if base_url is not None:
        kwargs["base_url"] = base_url
    return kwargs


def _should_cache_prefix(cached_session: CachedSessionHandle | None) -> bool:
    return cached_session is not None and cached_session.supported


def _prefix_stable_count(cached_session: CachedSessionHandle | None) -> int | None:
    """The count of stable reference turns leading the conversation (#362), used to
    place the cache breakpoint; None for handles without reference artifacts."""
    if cached_session is None:
        return None
    return cached_session.prefix_stable_messages


def _prefix_has_per_item_artifacts(cached_session: CachedSessionHandle | None) -> bool:
    """Whether the activity attaches per-item (non-reference) artifact turns after
    the varying per-item input (#698). When it does — and no leading reference span
    is marked instead — the legacy ``conversation[-2]`` breakpoint is unsound (it
    lands on the varying query), so the provider skips conversation marking."""
    if cached_session is None:
        return False
    return cached_session.per_item_artifact_messages


#: Anthropic content-block types that accept a ``cache_control`` breakpoint.
_ANTHROPIC_CACHEABLE_BLOCK_TYPES = frozenset(
    {"text", "image", "document", "tool_use", "tool_result"}
)


def _mark_prefix_cache_breakpoint(
    conversation: list[dict[str, Any]],
    stable_messages: int | None = None,
    per_item_artifact_messages: bool = False,
) -> None:
    """Mark the end of the stable prefix so Anthropic caches up to and including it.

    The runtime passes ``[stable prefix..., per-item input]`` as the conversation
    (prefix style), so the cache breakpoint goes on the last *cacheable* block of
    the second-to-last message — the final message is the variable per-item input.
    With a single message (no separate prefix turn) the system breakpoint already
    covers the stable part, so there is nothing to mark here.

    ``stable_messages`` (#362): when reference artifacts lead the conversation the
    handle carries their turn count ``k``; the breakpoint then lands on the last
    stable turn ``conversation[k - 1]`` so the documents join the cached prefix.
    The final (variable) message is never marked — if ``k`` would reach or exceed
    it, clamp back to the legacy ``conversation[-2]`` contract. ``k`` None/0 keeps
    that legacy contract byte-for-byte.

    ``per_item_artifact_messages`` (#698): with no leading reference span (``k``
    None/0) but per-item artifact turns trailing the varying input — the shape
    ``[system, per-item query, per-item artifact]`` — ``conversation[-2]`` is the
    varying query, not a stable turn. Marking it would key the conversation cache on
    content that differs per item, so mark NOTHING in the conversation: the shape has
    no stable conversation span (only the system block caches). Ignored when ``k`` > 0
    (the reference span is the authoritative breakpoint).
    """
    if len(conversation) < 2:
        return
    if stable_messages is not None and stable_messages > 0:
        index = min(stable_messages - 1, len(conversation) - 2)
    elif per_item_artifact_messages:
        # No stable conversation span to cache — the trailing per-item artifact means
        # the last two turns both vary. Leave the conversation unmarked (#698).
        return
    else:
        index = len(conversation) - 2
    prefix_message = conversation[index]
    content = prefix_message.get("content")
    if isinstance(content, str):
        prefix_message["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
        return
    if not isinstance(content, list):
        return
    # Mark the last cacheable block (everything up to it is cached). Skip a
    # trailing non-cacheable block (e.g. a provider-extension) that would be
    # rejected with cache_control (#60 review).
    for index in range(len(content) - 1, -1, -1):
        block = content[index]
        if isinstance(block, dict) and block.get("type") in _ANTHROPIC_CACHEABLE_BLOCK_TYPES:
            content[index] = {**block, "cache_control": {"type": "ephemeral"}}
            return


def _anthropic_call_kwargs(
    *,
    messages: list[ChatMessage],
    artifacts: tuple[ResolvedArtifactGroup, ...],
    output_schema: type[BaseModel],
    params: ProviderParams,
    cache_prefix: bool = False,
    prefix_stable_messages: int | None = None,
    per_item_artifact_messages: bool = False,
) -> dict[str, Any]:
    system_parts: list[str] = []
    conversation: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            system_parts.append(_to_anthropic_system_text(message, artifacts=artifacts))
            continue
        if message.role not in {"user", "assistant"}:
            raise ProviderConfigError(
                f"unsupported Anthropic message role: {message.role!r}",
                provider="anthropic",
            )
        conversation.append(
            {
                "role": message.role,
                "content": _to_anthropic_message_content(message, artifacts=artifacts),
            }
        )

    call_kwargs: dict[str, Any] = {
        "model": params.model,
        "max_tokens": params.max_tokens,
        "messages": conversation,
        "output_format": output_schema,
    }
    if system_parts:
        system_text = "\n\n".join(system_parts)
        # A list-of-blocks system is how Anthropic attaches cache_control; the
        # stable instructions are the cheapest always-stable thing to cache.
        call_kwargs["system"] = (
            [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
            if cache_prefix
            else system_text
        )
    if cache_prefix:
        _mark_prefix_cache_breakpoint(
            conversation, prefix_stable_messages, per_item_artifact_messages
        )
    if params.temperature is not None:
        call_kwargs["temperature"] = params.temperature
    if params.top_p is not None:
        call_kwargs["top_p"] = params.top_p
    if params.top_k is not None:
        call_kwargs["top_k"] = params.top_k
    if params.stop:
        call_kwargs["stop_sequences"] = list(params.stop)
    if params.timeout is not None:
        call_kwargs["timeout"] = params.timeout
    return call_kwargs


def _anthropic_create_kwargs(call_kwargs: dict[str, Any]) -> dict[str, Any]:
    output_schema = call_kwargs["output_format"]
    create_kwargs = {key: value for key, value in call_kwargs.items() if key != "output_format"}
    create_kwargs["output_config"] = {
        "format": {
            "type": "json_schema",
            "schema": output_schema.model_json_schema(),
        }
    }
    return create_kwargs


def _validated_provider_result(
    result: Any,
    output_schema: type[BaseModel],
    *,
    usage_sink: Callable[[ProviderUsage], None] | None = None,
    model: str | None = None,
) -> BaseModel:
    _raise_if_truncated(result)
    if usage_sink is not None:
        usage = _anthropic_usage(result, model=model)
        if usage is not None:
            usage_sink(usage)
    parsed = _anthropic_parsed_output(result)
    if parsed is not None:
        if isinstance(parsed, output_schema):
            return parsed
        return output_schema.model_validate(parsed)
    if isinstance(result, output_schema):
        return result
    text = _anthropic_response_text(result)
    if text is not None:
        return output_schema.model_validate_json(text)
    raise ProviderConfigError(
        "Anthropic client returned a result without parsed structured output",
        provider="anthropic",
    )


def _anthropic_parsed_output(result: Any) -> Any:
    # The Anthropic structured-output result exposes ``parsed_output`` on the
    # parsed text content block (ParsedTextBlock), not on the top-level message.
    # Check the top level first for forward/backward compatibility, then the blocks.
    top = getattr(result, "parsed_output", None)
    if top is not None:
        return top
    content = getattr(result, "content", None)
    if isinstance(content, list):
        for block in content:
            block_parsed = getattr(block, "parsed_output", None)
            if block_parsed is not None:
                return block_parsed
            if isinstance(block, dict) and block.get("parsed_output") is not None:
                return block["parsed_output"]
    return None


def _raise_if_truncated(result: Any) -> None:
    stop_reason = getattr(result, "stop_reason", None)
    if stop_reason is None and isinstance(result, dict):
        stop_reason = result.get("stop_reason")
    raise_if_truncated(
        truncated=stop_reason == "max_tokens", provider="anthropic", display_name="Anthropic"
    )


def _anthropic_usage(result: Any, *, model: str | None) -> ProviderUsage | None:
    usage = getattr(result, "usage", None)
    if usage is None and isinstance(result, dict):
        usage = result.get("usage")
    if usage is None:
        return None
    input_tokens = _usage_int(usage, "input_tokens")
    output_tokens = _usage_int(usage, "output_tokens")
    # Prompt-cache accounting (#60): read = served from cache (hit), creation =
    # written this call (miss/first). Anthropic reports input_tokens as the
    # *uncached* input, so the cache fields are separate signals.
    cache_read = _usage_int(usage, "cache_read_input_tokens")
    cache_write = _usage_int(usage, "cache_creation_input_tokens")
    if all(v is None for v in (input_tokens, output_tokens, cache_read, cache_write)):
        return None
    return ProviderUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model=model,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


def _usage_int(usage: Any, field: str) -> int | None:
    value = getattr(usage, field, None)
    if value is None and isinstance(usage, dict):
        value = usage.get(field)
    return value if isinstance(value, int) else None


def _anthropic_response_text(result: Any) -> str | None:
    content = getattr(result, "content", None)
    if isinstance(content, list) and content:
        first = content[0]
        text = getattr(first, "text", None)
        if isinstance(text, str):
            return text
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            return first["text"]
    text = getattr(result, "text", None)
    return text if isinstance(text, str) else None


def _to_anthropic_system_text(
    message: ChatMessage,
    *,
    artifacts: tuple[ResolvedArtifactGroup, ...],
) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    text_parts: list[str] = []
    for part in content:
        if isinstance(part, TextPart):
            text_parts.append(part.text)
            continue
        if isinstance(part, (ArtifactPart, ArtifactGroupPart)):
            if part.text is not None:
                text_parts.append(part.text)
                continue
        raise ProviderConfigError(
            "AnthropicProvider only supports text content in system messages",
            provider="anthropic",
        )
    return "\n\n".join(text_parts)


def _to_anthropic_message_content(
    message: ChatMessage,
    *,
    artifacts: tuple[ResolvedArtifactGroup, ...],
) -> str | list[dict[str, Any]]:
    content = message.content
    if isinstance(content, str):
        return content
    return _to_anthropic_content_blocks(content, artifacts=artifacts)


def _to_anthropic_content_blocks(
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
            for artifact in artifacts_for_group(artifacts, part.group, provider="anthropic"):
                payload.append(_to_anthropic_artifact_part(artifact))
        elif isinstance(part, ArtifactPart):
            if part.text is not None:
                payload.append({"type": "text", "text": part.text})
            payload.append(
                _to_anthropic_artifact_part(
                    artifact_for_name(artifacts, part.artifact, provider="anthropic")
                )
            )
        elif isinstance(part, ProviderExtensionPart):
            if part.provider != "anthropic":
                raise ProviderConfigError(
                    f"content part provider extension {part.provider!r} is not for Anthropic",
                    provider="anthropic",
                )
            payload.append(dict(part.payload))
        else:
            raise ProviderConfigError(
                f"unsupported Anthropic content part: {type(part).__name__}",
                provider="anthropic",
            )
    return payload


def _to_anthropic_artifact_part(artifact: ResolvedArtifact) -> dict[str, Any]:
    source = artifact.ref.source
    if artifact.kind == "image" or (artifact.media_type or "").startswith("image/"):
        if source.type == "url":
            return {"type": "image", "source": {"type": "url", "url": source.url}}
        if source.type == "local_path" and artifact.local_path is not None:
            media_type = artifact.media_type or "image/png"
            if media_type not in _SUPPORTED_IMAGE_MEDIA_TYPES:
                raise ProviderConfigError(
                    f"AnthropicProvider does not support image media type {media_type!r}",
                    provider="anthropic",
                )
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": _base64_file(artifact.local_path),
                },
            }
    if source.type == "local_path" and artifact.local_path is not None:
        if _is_text_like_media_type(artifact.media_type):
            return {"type": "text", "text": artifact.local_path.read_text(encoding="utf-8")}
        if artifact.media_type == "application/pdf":
            return {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": _base64_file(artifact.local_path),
                },
            }
    if source.type == "provider_file":
        if source.provider != "anthropic":
            raise ProviderConfigError(
                f"provider file {source.provider!r} is not for Anthropic",
                provider="anthropic",
            )
        block_type = "image" if _provider_file_is_image(artifact) else "document"
        return {
            "type": block_type,
            "source": {
                "type": "file",
                "file_id": source.file_id,
            },
        }
    raise ProviderConfigError(
        "AnthropicProvider cannot attach this artifact through the messages path",
        provider="anthropic",
    )


def _provider_file_is_image(artifact: ResolvedArtifact) -> bool:
    return artifact.kind == "image" or (artifact.media_type or "").startswith("image/")


def _base64_file(path: Any) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _is_text_like_media_type(media_type: str | None) -> bool:
    if media_type is None:
        return False
    normalized = media_type.split(";", 1)[0].strip().lower()
    return normalized.startswith("text/") or normalized in {
        "application/csv",
        "application/json",
        "application/xml",
        "application/x-ndjson",
        "text/csv",
    }


def _raise_anthropic_provider_error(exc: Exception) -> NoReturn:
    if isinstance(exc, ValidationError):
        raise exc
    if isinstance(exc, ProviderError):
        raise exc
    raise _classify_provider_error(exc, provider="anthropic") from exc


def _classify_provider_error(exc: Exception, *, provider: str) -> ProviderError:
    code = status_code(exc)
    # The raw exception text can carry API response bodies into manifests,
    # traces, and Temporal history; match OpenAI's sanitized reason shape and
    # keep the original exception object for programmatic handling.
    reason = f"{provider} structured call failed"
    if matches_provider_error(exc, "anthropic", "RateLimitError") or code == 429:
        return ProviderRateLimitError(
            reason,
            provider=provider,
            status_code=code,
            original=exc,
            retry_after_seconds=retry_after_seconds(exc),
        )
    if matches_provider_error(
        exc,
        "anthropic",
        "AuthenticationError",
        "PermissionDeniedError",
    ) or code in {401, 403}:
        return ProviderAuthError(
            reason,
            provider=provider,
            status_code=code,
            original=exc,
        )
    if is_network_or_timeout_error(exc) or (code is not None and code >= 500):
        return ProviderTransientError(
            reason,
            provider=provider,
            status_code=code,
            original=exc,
            retry_after_seconds=retry_after_seconds(exc),
        )
    if code in {408, 409}:
        return ProviderTransientError(
            reason,
            provider=provider,
            status_code=code,
            original=exc,
            retry_after_seconds=retry_after_seconds(exc),
        )
    if matches_provider_error(
        exc,
        "anthropic",
        "BadRequestError",
        "NotFoundError",
        "UnprocessableEntityError",
    ) or (code is not None and 400 <= code < 500):
        return ProviderConfigError(
            reason,
            provider=provider,
            status_code=code,
            original=exc,
        )
    return ProviderError(
        reason=reason,
        provider=provider,
        retryable=False,
        status_code=code,
        original=exc,
    )


__all__ = ["AnthropicProvider"]
