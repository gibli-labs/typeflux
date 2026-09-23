from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import cache
from typing import Any, Literal, cast

from typeflux.core.artifacts import normalize_content_parts
from typeflux.core.contracts import ChatMessage, PromptRef, ProviderParams, ResolvedPrompt
from typeflux.env import load_env
from typeflux.prompts.context import langfuse_prompt_context
from typeflux.prompts.errors import (
    PromptNotFoundError,
    PromptRegistryAuthError,
    PromptRegistryConfigError,
    PromptRegistryUnavailableError,
    PromptResolutionError,
)


class LangfusePromptRegistry:
    def __init__(
        self,
        *,
        host: str | None = None,
        public_key: str | None = None,
        secret_key: str | None = None,
        client: Any | None = None,
        allow_prompt_model_override: bool = False,
    ) -> None:
        load_env()
        self._client = client or _langfuse_client(
            host=host,
            public_key=public_key,
            secret_key=secret_key,
        )
        self._allow_prompt_model_override = allow_prompt_model_override

    def resolve(self, ref: PromptRef) -> ResolvedPrompt:
        if ref.prompt_type == "auto":
            return self._resolve_auto(ref)
        return self._resolve_with_type(ref, ref.prompt_type)

    def _resolve_auto(self, ref: PromptRef) -> ResolvedPrompt:
        try:
            return self._resolve_with_type(ref, "text")
        except PromptResolutionError as exc:
            if not _should_retry_as_chat(exc):
                raise
            return self._resolve_with_type(ref, "chat")

    def _resolve_with_type(
        self, ref: PromptRef, prompt_type: Literal["text", "chat"]
    ) -> ResolvedPrompt:
        try:
            if ref.version is not None:
                prompt = self._client.get_prompt(ref.name, version=ref.version, type=prompt_type)
            else:
                prompt = self._client.get_prompt(
                    ref.name,
                    label=ref.label if ref.label is not None else "production",
                    type=prompt_type,
                )
        except PromptResolutionError:
            raise
        except Exception as exc:
            raise _classify_langfuse_prompt_error(exc, ref) from exc

        messages = _messages_from_langfuse_prompt(prompt, ref, prompt_type=prompt_type)

        raw_config = getattr(prompt, "config", None) or {}
        if not isinstance(raw_config, Mapping):
            raise PromptRegistryConfigError(
                f"resolved prompt {ref.name}@{ref.selector} config must be a mapping",
                ref=ref,
            )
        config: dict[str, Any] = dict(raw_config)
        model = config.get("model") or config.get("provider_model")
        temperature = config.get("temperature")
        if model is not None and not isinstance(model, str):
            raise PromptRegistryConfigError(
                f"resolved prompt {ref.name}@{ref.selector} model must be a string",
                ref=ref,
            )
        if temperature is not None and not isinstance(temperature, (int, float)):
            raise PromptRegistryConfigError(
                f"resolved prompt {ref.name}@{ref.selector} temperature must be numeric",
                ref=ref,
            )
        provider_params = _provider_params_from_config(
            config,
            ref=ref,
            model=model,
            temperature=temperature,
        )
        if not self._allow_prompt_model_override:
            # Provider/YAML configuration is authoritative for the execution
            # model; prompt config model fields stay visible through the
            # langfuse.prompt_config metadata only.
            model = None
            provider_params = _without_model(provider_params)
        resolved_version = (
            str(prompt.version) if getattr(prompt, "version", None) is not None else None
        )
        return ResolvedPrompt(
            ref=ref,
            messages=messages,
            resolved_version=resolved_version,
            model=model,
            temperature=temperature,
            provider_params=provider_params,
            metadata={
                "langfuse.prompt_type": prompt_type,
                "langfuse.prompt_version": resolved_version,
                "langfuse.prompt_config": config,
            },
            observation_context=langfuse_prompt_context(prompt),
        )

    def create_prompt(
        self,
        *,
        name: str,
        prompt: str,
        label: str = "production",
        tags: list[str] | None = None,
        config: dict[str, Any] | None = None,
        commit_message: str | None = None,
    ) -> str | None:
        created = self._client.create_prompt(
            name=name,
            prompt=prompt,
            labels=[label],
            tags=tags,
            type="text",
            config=config,
            commit_message=commit_message,
        )
        return str(created.version) if getattr(created, "version", None) is not None else None


def _langfuse_client(
    *,
    host: str | None = None,
    public_key: str | None = None,
    secret_key: str | None = None,
) -> Any:
    try:
        from langfuse import Langfuse
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
        raise RuntimeError("langfuse is required for LangfusePromptRegistry") from exc

    return Langfuse(
        host=host,
        public_key=public_key,
        secret_key=secret_key,
    )


def _messages_from_langfuse_prompt(
    prompt: Any, ref: PromptRef, *, prompt_type: Literal["text", "chat"]
) -> tuple[ChatMessage, ...]:
    prompt_content = getattr(prompt, "prompt", None)
    if prompt_type == "text":
        if not isinstance(prompt_content, str):
            raise PromptRegistryConfigError(
                f"resolved prompt {ref.name}@{ref.selector} did not contain text content",
                ref=ref,
            )
        return (ChatMessage(role="user", content=prompt_content),)

    if (
        isinstance(prompt_content, (str, bytes, bytearray))
        or not isinstance(prompt_content, Sequence)
        or not prompt_content
    ):
        raise PromptRegistryConfigError(
            f"resolved prompt {ref.name}@{ref.selector} did not contain chat messages",
            ref=ref,
        )
    return tuple(
        _chat_message_from_langfuse_message(message, ref=ref, index=index)
        for index, message in enumerate(prompt_content)
    )


def _without_model(provider_params: ProviderParams) -> ProviderParams:
    params = provider_params.to_dict(include_empty=True)
    params["model"] = None
    return ProviderParams.from_mapping(params)


def _provider_params_from_config(
    config: Mapping[str, Any],
    *,
    ref: PromptRef,
    model: str | None,
    temperature: float | None,
) -> ProviderParams:
    raw_params = config.get("provider_params")
    if raw_params is None:
        return ProviderParams(model=model, temperature=temperature)
    if not isinstance(raw_params, Mapping):
        raise PromptRegistryConfigError(
            f"resolved prompt {ref.name}@{ref.selector} provider_params must be a mapping",
            ref=ref,
        )
    try:
        params = ProviderParams.from_mapping(raw_params)
        if model is not None and params.model is not None and params.model != model:
            raise ValueError("model and provider_params.model must match")
        if (
            temperature is not None
            and params.temperature is not None
            and params.temperature != temperature
        ):
            raise ValueError("temperature and provider_params.temperature must match")
        return params.with_legacy(model=model, temperature=temperature)
    except (TypeError, ValueError) as exc:
        raise PromptRegistryConfigError(
            f"resolved prompt {ref.name}@{ref.selector} provider_params invalid: {exc}",
            ref=ref,
        ) from exc


def _chat_message_from_langfuse_message(message: Any, *, ref: PromptRef, index: int) -> ChatMessage:
    if isinstance(message, Mapping):
        role = message.get("role")
        content = message.get("content")
        name = message.get("name")
    else:
        role = getattr(message, "role", None)
        content = getattr(message, "content", None)
        name = getattr(message, "name", None)

    if role not in ("system", "user", "assistant"):
        raise PromptRegistryConfigError(
            f"resolved chat prompt {ref.name}@{ref.selector} message {index} "
            "role must be system, user, or assistant",
            ref=ref,
        )
    if not isinstance(content, str):
        try:
            content = normalize_content_parts(content)
        except (KeyError, TypeError, ValueError) as exc:
            raise PromptRegistryConfigError(
                f"resolved chat prompt {ref.name}@{ref.selector} message {index} "
                "content must be text or Typeflux content parts",
                ref=ref,
            ) from exc
    if name is not None and not isinstance(name, str):
        raise PromptRegistryConfigError(
            f"resolved chat prompt {ref.name}@{ref.selector} message {index} "
            "name must be text when provided",
            ref=ref,
        )
    return ChatMessage(
        role=cast(Literal["system", "user", "assistant"], role),
        content=content,
        name=name,
    )


def _classify_langfuse_prompt_error(exc: Exception, ref: PromptRef) -> PromptResolutionError:
    status_code = _status_code(exc)
    reason = f"failed to resolve prompt {ref.name}@{ref.selector}"

    if _matches_langfuse_error(exc, "NotFoundError") or status_code == 404:
        return PromptNotFoundError(reason, ref=ref, status_code=status_code, original=exc)
    if _matches_langfuse_error(exc, "UnauthorizedError", "AccessDeniedError") or status_code in {
        401,
        403,
    }:
        return PromptRegistryAuthError(reason, ref=ref, status_code=status_code, original=exc)
    if _matches_langfuse_error(exc, "MethodNotAllowedError") or status_code in {400, 405, 422}:
        return PromptRegistryConfigError(reason, ref=ref, status_code=status_code, original=exc)
    if (
        _matches_langfuse_error(exc, "ServiceUnavailableError")
        or status_code in {408, 429}
        or (status_code is not None and status_code >= 500)
        or _is_network_or_timeout_error(exc)
    ):
        return PromptRegistryUnavailableError(
            reason, ref=ref, status_code=status_code, original=exc
        )
    return PromptResolutionError(
        reason=reason, ref=ref, retryable=False, status_code=status_code, original=exc
    )


def _should_retry_as_chat(exc: PromptResolutionError) -> bool:
    if isinstance(exc, PromptNotFoundError):
        return True
    return isinstance(exc, PromptRegistryConfigError) and (
        exc.status_code in {400, 405, 422} or "did not contain text content" in exc.reason
    )


def _matches_langfuse_error(exc: Exception, *names: str) -> bool:
    if _matches_error_name(exc, *names):
        return True
    classes = _langfuse_error_classes(*names)
    return bool(classes) and isinstance(exc, classes)


@cache
def _langfuse_error_classes(*names: str) -> tuple[type[BaseException], ...]:
    try:
        from langfuse import api
    except ModuleNotFoundError:
        return ()
    classes = []
    for name in names:
        value = getattr(api, name, None)
        if isinstance(value, type) and issubclass(value, BaseException):
            classes.append(value)
    return tuple(classes)


def _matches_error_name(exc: Exception, *names: str) -> bool:
    expected = set(names)
    return any(cls.__name__ in expected for cls in type(exc).__mro__)


def _is_network_or_timeout_error(exc: Exception) -> bool:
    if isinstance(exc, (OSError, TimeoutError)):
        return True
    return any(
        "connection" in cls.__name__.lower() or "timeout" in cls.__name__.lower()
        for cls in type(exc).__mro__
    )


def _status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


__all__ = ["LangfusePromptRegistry"]
