"""Text-only structured calls through Vercel's OpenAI-compatible gateway."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable
from dataclasses import replace
from inspect import isawaitable, iscoroutinefunction
from threading import Lock
from typing import Any, NoReturn

from pydantic import BaseModel, ValidationError

from typeflux.core.artifacts import ArtifactKind, ResolvedArtifactGroup, TextPart
from typeflux.core.contracts import CachedSessionHandle, ChatMessage, ProviderParams
from typeflux.env import load_env
from typeflux.providers._shared import matches_provider_error, raise_if_truncated
from typeflux.providers.base import ProviderUsage, validate_provider_params_supported
from typeflux.providers.errors import ProviderAuthError, ProviderConfigError
from typeflux.providers.openai import (
    _classify_provider_error,
    _openai_call_kwargs,
    _openai_usage,
)

VERCEL_AI_GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"
VERCEL_AI_GATEWAY_PROVIDER = "vercel-ai-gateway"


def validate_gateway_model(model: str | None) -> None:
    if (
        not isinstance(model, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./:-]+", model) is None
    ):
        raise ProviderConfigError(
            "Vercel AI Gateway requires an explicit author/model ID",
            provider=VERCEL_AI_GATEWAY_PROVIDER,
        )


class VercelAIGatewayProvider:
    """Uses the optional ``typeflux[openai]`` extra; injected clients need no SDK.

    Select a model supporting Chat Completions JSON Schema output. No model
    catalog, artifact, routing, tool, or provider-side session-cache capability
    is assumed. SDK retries are disabled; Typeflux owns retries.
    """

    provider_name = VERCEL_AI_GATEWAY_PROVIDER
    supported_provider_params = frozenset(
        {"model", "temperature", "max_tokens", "top_p", "stop", "timeout"}
    )
    supported_artifact_kinds: frozenset[ArtifactKind] = frozenset()
    supports_session_cache = False

    def __init__(
        self,
        *,
        default_model: str,
        api_key: str | None = None,
        openai_client: Any | None = None,
        default_provider_params: ProviderParams | None = None,
    ) -> None:
        validate_gateway_model(default_model)
        self.default_model = default_model
        self.default_provider_params = ProviderParams(model=default_model).merge(
            default_provider_params
        )
        validate_gateway_model(self.default_provider_params.model)
        validate_provider_params_supported(self, self.default_provider_params)
        self._client: Any = openai_client
        self._async_client: Any | None = None
        self._client_lock = Lock()
        self._injected = openai_client is not None
        self._api_key = api_key
        if not self._injected:
            load_env()
            self._api_key = api_key if api_key is not None else os.getenv("AI_GATEWAY_API_KEY")
            if not self._api_key or not self._api_key.strip():
                raise ProviderAuthError(
                    "Vercel AI Gateway requires AI_GATEWAY_API_KEY or api_key",
                    provider=self.provider_name,
                )

    def _get_client(self, *, asynchronous: bool = False) -> Any:
        if self._injected:
            return self._client
        with self._client_lock:
            current = self._async_client if asynchronous else self._client
            if current is not None:
                return current
            try:
                from openai import AsyncOpenAI, OpenAI
            except ModuleNotFoundError:
                raise RuntimeError("install typeflux[openai] for VercelAIGatewayProvider") from None
            factory = AsyncOpenAI if asynchronous else OpenAI
            client = factory(
                api_key=self._api_key, base_url=VERCEL_AI_GATEWAY_BASE_URL, max_retries=0
            )
            if asynchronous:
                self._async_client = client
            else:
                self._client = client
            return client

    @property
    def supports_async_structured_call(self) -> bool:
        if not self._injected:
            return True
        return iscoroutinefunction(self._client.chat.completions.parse)

    def _request(
        self,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None,
        temperature: float | None,
        provider_params: ProviderParams | None,
        artifacts: tuple[ResolvedArtifactGroup, ...],
        cached_session: CachedSessionHandle | None,
    ) -> tuple[dict[str, Any], str | None]:
        params = self.default_provider_params.merge(
            provider_params, ProviderParams(model=model, temperature=temperature)
        )
        validate_gateway_model(params.model)
        validate_provider_params_supported(self, params)
        if artifacts or (cached_session is not None and cached_session.supported):
            raise ProviderConfigError(
                "Vercel AI Gateway does not support artifacts or explicit session caches",
                provider=self.provider_name,
            )
        for message in messages:
            if message.role not in {"system", "user", "assistant"} or (
                not isinstance(message.content, str)
                and any(not isinstance(part, TextPart) for part in message.content)
            ):
                raise ProviderConfigError(
                    "Vercel AI Gateway supports only system/user/assistant text messages",
                    provider=self.provider_name,
                )
        return _openai_call_kwargs(
            messages=messages,
            output_schema=output_schema,
            params=params,
            metadata=None,
            observation_context=None,
            include_trace_name=False,
        ), params.model

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
        usage_sink: Callable[[ProviderUsage], None] | None = None,
        cached_session: CachedSessionHandle | None = None,
    ) -> BaseModel:
        del metadata  # Observed by Typeflux; never forwarded to the gateway.
        request, requested_model = self._request(
            messages, output_schema, model, temperature, provider_params, artifacts, cached_session
        )
        client = self._get_client()
        try:
            completion = client.chat.completions.parse(**request)
        except Exception as exc:
            _raise_gateway_error(exc)
        return _result(completion, output_schema, requested_model, usage_sink)

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
        usage_sink: Callable[[ProviderUsage], None] | None = None,
        cached_session: CachedSessionHandle | None = None,
    ) -> BaseModel:
        del metadata
        request, requested_model = self._request(
            messages, output_schema, model, temperature, provider_params, artifacts, cached_session
        )
        client = await asyncio.to_thread(self._get_client, asynchronous=True)
        try:
            pending = client.chat.completions.parse(**request)
            if not isawaitable(pending):
                raise ProviderConfigError(
                    "injected Vercel AI Gateway client does not support async calls",
                    provider=self.provider_name,
                )
            completion = await pending
        except Exception as exc:
            _raise_gateway_error(exc)
        return _result(completion, output_schema, requested_model, usage_sink)


def _raise_gateway_error(exc: Exception) -> NoReturn:
    if isinstance(exc, (ValidationError, ProviderConfigError)):
        raise exc
    if matches_provider_error(exc, "openai", "LengthFinishReasonError"):
        raise ProviderConfigError(
            "Vercel AI Gateway response was truncated; raise max_tokens",
            provider=VERCEL_AI_GATEWAY_PROVIDER,
        ) from None
    if matches_provider_error(exc, "openai", "ContentFilterFinishReasonError"):
        raise ProviderConfigError(
            "Vercel AI Gateway filtered the structured response",
            provider=VERCEL_AI_GATEWAY_PROVIDER,
        ) from None
    error = _classify_provider_error(exc, provider=VERCEL_AI_GATEWAY_PROVIDER)
    # SDK errors can carry request headers and response bodies. Keep only typed
    # status/retry information, never their raw exception or chained traceback.
    error.original = None
    raise error from None


def _result(
    completion: Any,
    schema: type[BaseModel],
    model: str | None,
    usage_sink: Callable[[ProviderUsage], None] | None,
) -> BaseModel:
    choices = getattr(completion, "choices", None)
    choice = choices[0] if choices else None
    raise_if_truncated(
        truncated=getattr(choice, "finish_reason", None) == "length",
        provider=VERCEL_AI_GATEWAY_PROVIDER,
        display_name="Vercel AI Gateway",
    )
    message = getattr(choice, "message", None)
    if (
        getattr(message, "refusal", None)
        or getattr(choice, "finish_reason", None) == "content_filter"
    ):
        raise ProviderConfigError(
            "Vercel AI Gateway refused the request", provider=VERCEL_AI_GATEWAY_PROVIDER
        )
    parsed = getattr(message, "parsed", None)
    if parsed is None:
        raise ProviderConfigError(
            "Vercel AI Gateway returned no structured output", provider=VERCEL_AI_GATEWAY_PROVIDER
        )
    result = schema.model_validate(parsed)
    if usage_sink is not None:
        usage = _openai_usage(completion, model=model) or ProviderUsage(model=model)
        metadata = getattr(completion, "provider_metadata", None)
        gateway = metadata.get("gateway", {}) if isinstance(metadata, dict) else {}
        routing = gateway.get("routing", {}) if isinstance(gateway, dict) else {}
        upstream = routing.get("finalProvider") if isinstance(routing, dict) else None
        usage_sink(
            replace(
                usage,
                reported_model=_identity(getattr(completion, "model", None)),
                upstream_provider=_identity(upstream),
            )
        )
    return result


def _identity(value: Any) -> str | None:
    # Only bounded identifiers; no routing logs, request IDs, credentials or PII.
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_./:-]{1,200}", value)
        else None
    )
