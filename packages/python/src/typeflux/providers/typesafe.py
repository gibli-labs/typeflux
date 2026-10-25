"""Native Jev decisions: a JSON question map in the system prompt, user text as state."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable
from typing import Any, NoReturn

from pydantic import BaseModel

from typeflux.core.artifacts import ArtifactKind, ResolvedArtifactGroup, TextPart
from typeflux.core.contracts import CachedSessionHandle, ChatMessage, ProviderParams
from typeflux.env import load_env
from typeflux.providers._shared import is_network_or_timeout_error, retry_after_seconds
from typeflux.providers.base import ProviderUsage, validate_provider_params_supported
from typeflux.providers.errors import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderRateLimitError,
    ProviderTransientError,
)

TYPESAFE_BASE_URL = "https://api.typesafe.ai"
DEFAULT_TYPESAFE_MODEL = "jev-1.14.0"


def _config(reason: str) -> ProviderConfigError:
    return ProviderConfigError(f"TypeSafe {reason}", provider="typesafe")


def _model(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
        raise _config("requires a nonempty model identifier")
    return value


def _content(value: Any) -> bool:
    return isinstance(value, (str, dict, list))


def _questions(messages: list[ChatMessage]) -> tuple[dict[str, Any], str | list[dict[str, str]]]:
    if len(messages) < 2 or messages[0].role != "system":
        raise _config(
            "requires one system message containing JSON questions followed by user state"
        )
    text = []
    for message in messages:
        if message.role not in {"system", "user", "assistant"}:
            raise _config("supports only system/user/assistant text messages")
        if isinstance(message.content, str):
            text.append(message.content)
        elif all(isinstance(part, TextPart) for part in message.content):
            text.append(
                "".join(part.text for part in message.content if isinstance(part, TextPart))
            )
        else:
            raise _config("supports only text content")
    try:
        questions = json.loads(text[0])
        # Reject nonfinite numbers at any depth, including in structured criteria.
        json.dumps(questions, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise _config("system message must be a JSON question map with finite values") from None
    if not isinstance(questions, dict) or not questions:
        raise _config("requires a nonempty question map")
    for name, question in questions.items():
        if (
            not name
            or not isinstance(question, dict)
            or set(question) - {"type", "instructions", "criteria"}
        ):
            raise _config("received an invalid question")
        if not _content(question.get("instructions")):
            raise _config("question instructions must be text, an object or an array")
        kind, criteria = question.get("type"), question.get("criteria")
        if kind == "choice":
            if (
                not isinstance(criteria, dict)
                or not 1 <= len(criteria) <= 255
                or any(
                    not key or (value is not None and not _content(value))
                    for key, value in criteria.items()
                )
            ):
                raise _config("choice criteria require 1 to 255 named descriptions")
        elif kind == "score":
            if (
                not isinstance(criteria, list)
                or not 2 <= len(criteria) <= 10
                or not all(_content(value) for value in criteria)
            ):
                raise _config("score criteria require 2 to 10 ordered descriptions")
        elif kind == "noul":
            if criteria is not None and (
                not isinstance(criteria, dict)
                or set(criteria) != {"true", "false"}
                or not all(_content(value) for value in criteria.values())
            ):
                raise _config("noul criteria require true and false descriptions")
        else:
            raise _config("supports choice, score and noul questions")
    if not any(message.role == "user" for message in messages[1:]):
        raise _config("requires user state after the system question map")
    state = (
        text[1]
        if len(text) == 2
        else [
            {"role": message.role, "content": value}
            for message, value in zip(messages[1:], text[1:], strict=True)
        ]
    )
    return questions, state


def _number(value: Any, maximum: float = 1) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum


def _answers(response: Any, questions: dict[str, Any]) -> dict[str, Any]:
    if isinstance(response, BaseModel):
        response = response.model_dump(mode="json")
    if not isinstance(response, dict) or not isinstance(response.get("answers"), dict):
        raise _config("returned an invalid response")
    answers = response["answers"]
    if set(answers) != set(questions):
        raise _config("returned answers that do not match the questions")
    for name, question in questions.items():
        answer = answers[name]
        kind = question["type"]
        if not isinstance(answer, dict) or answer.get("type") != kind:
            raise _config("returned an invalid answer type")
        if kind == "noul":
            if not _number(answer.get("noul")):
                raise _config("returned an invalid noul probability")
            continue
        keys = (
            set(question["criteria"])
            if kind == "choice"
            else {str(i) for i in range(len(question["criteria"]))}
        )
        probabilities = answer.get("probabilities")
        if (
            not isinstance(probabilities, dict)
            or set(probabilities) != keys
            or not all(_number(value) for value in probabilities.values())
            or not _number(answer.get("confidence"))
        ):
            raise _config("returned invalid probabilities or confidence")
        if kind == "choice" and (
            not isinstance(answer.get("choice"), str) or answer["choice"] not in keys
        ):
            raise _config("returned an unknown choice")
        if kind == "score" and (
            not _number(answer.get("score"), len(keys) - 1)
            or not isinstance(answer.get("legend"), dict)
            or set(answer["legend"]) != keys
        ):
            raise _config("returned an invalid score or legend")
    return response


class TypeSafeProvider:
    """Native choice/score/noul answers validated against the activity output model.

    Uses the optional ``typeflux[typesafe]`` SDK. Clients supplied by applications
    must disable their own retries; Typeflux's provider retry policy owns retries.
    """

    provider_name = "typesafe"
    supported_provider_params = frozenset({"model", "timeout"})
    supported_artifact_kinds: frozenset[ArtifactKind] = frozenset()
    supports_session_cache = False
    supports_async_structured_call = True

    def __init__(
        self,
        *,
        default_model: str = DEFAULT_TYPESAFE_MODEL,
        api_key: str | None = None,
        typesafe_client: Any | None = None,
        async_typesafe_client: Any | None = None,
        default_provider_params: ProviderParams | None = None,
    ) -> None:
        self.default_model = _model(default_model)
        self.default_provider_params = ProviderParams(model=default_model).merge(
            default_provider_params
        )
        _model(self.default_provider_params.model)
        validate_provider_params_supported(self, self.default_provider_params)
        self._client = typesafe_client
        self._async_client = async_typesafe_client
        self._injected = typesafe_client is not None or async_typesafe_client is not None
        self._api_key = api_key
        if not self._injected:
            load_env()
            self._api_key = api_key if api_key is not None else os.getenv("TYPESAFE_API_KEY")
            if not self._api_key or not self._api_key.strip():
                raise ProviderAuthError(
                    "TypeSafe requires TYPESAFE_API_KEY or api_key", provider="typesafe"
                )
        self.supports_async_structured_call = (
            not self._injected or async_typesafe_client is not None
        )

    def _request(
        self,
        messages: list[ChatMessage],
        model: str | None,
        temperature: float | None,
        provider_params: ProviderParams | None,
        artifacts: tuple[ResolvedArtifactGroup, ...],
        cached_session: CachedSessionHandle | None,
    ) -> dict[str, Any]:
        params = self.default_provider_params.merge(
            provider_params, ProviderParams(model=model, temperature=temperature)
        )
        validate_provider_params_supported(self, params)
        if artifacts or (cached_session is not None and cached_session.supported):
            raise _config("does not support artifacts or explicit session caches")
        questions, state = _questions(messages)
        return {
            "model": _model(params.model),
            "questions": questions,
            "state": state,
            **({"timeout": params.timeout} if params.timeout is not None else {}),
        }

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
        request = self._request(
            messages, model, temperature, provider_params, artifacts, cached_session
        )
        if self._injected:
            if self._client is None:
                raise _config("requires a synchronous client for synchronous calls")
            response = self._call(self._client, request)
        else:
            try:
                from typesafe_sdk import RetryPolicy, TypeSafeClient
            except ModuleNotFoundError:
                raise RuntimeError("install typeflux[typesafe] for TypeSafeProvider") from None
            with TypeSafeClient(
                api_key=self._api_key, base_url=TYPESAFE_BASE_URL, retry=RetryPolicy(max_retries=0)
            ) as client:
                response = self._call(client, request)
        return _result(response, request, output_schema, usage_sink)

    @staticmethod
    def _call(client: Any, request: dict[str, Any]) -> Any:
        try:
            return client.system_one(**request)
        except Exception as exc:
            _raise_error(exc)

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
        request = self._request(
            messages, model, temperature, provider_params, artifacts, cached_session
        )
        if self._injected:
            if self._async_client is None:
                raise _config("requires an asynchronous client for async calls")
            response = await self._async_call(self._async_client, request)
        else:
            try:
                from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy
            except ModuleNotFoundError:
                raise RuntimeError("install typeflux[typesafe] for TypeSafeProvider") from None
            async with AsyncTypeSafeClient(
                api_key=self._api_key, base_url=TYPESAFE_BASE_URL, retry=RetryPolicy(max_retries=0)
            ) as client:
                response = await self._async_call(client, request)
        return _result(response, request, output_schema, usage_sink)

    @staticmethod
    async def _async_call(client: Any, request: dict[str, Any]) -> Any:
        try:
            return await client.system_one(**request)
        except Exception as exc:
            _raise_error(exc)


def _result(
    response: Any,
    request: dict[str, Any],
    schema: type[BaseModel],
    sink: Callable[[ProviderUsage], None] | None,
) -> BaseModel:
    response = _answers(response, request["questions"])
    if sink is not None:
        usage = response.get("usage", {})

        def tokens(key: str) -> int | None:
            value = usage.get(key) if isinstance(usage, dict) else None
            return value if type(value) is int and value >= 0 else None

        reported = response.get("model")
        sink(
            ProviderUsage(
                model=request["model"],
                input_tokens=tokens("input_tokens"),
                output_tokens=tokens("output_tokens"),
                reported_model=reported
                if isinstance(reported, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", reported)
                else None,
            )
        )
    return schema.model_validate(response["answers"])


def _retry_hint(exc: Exception) -> float | None:
    milliseconds = getattr(exc, "retry_after_ms", None)
    if (
        isinstance(milliseconds, (int, float))
        and not isinstance(milliseconds, bool)
        and math.isfinite(milliseconds)
        and milliseconds >= 0
    ):
        return milliseconds / 1000
    # Official SDK errors carry headers directly, including millisecond hints
    # on server errors that do not expose the rate-limit-only retry_after_ms.
    headers = getattr(exc, "headers", None)
    if headers is not None:
        for name, scale in (("retry-after-ms", 1000), ("retry-after", 1)):
            try:
                value = float(headers.get(name))
            except (AttributeError, TypeError, ValueError):
                continue
            if math.isfinite(value) and value >= 0:
                return value / scale
    seconds = retry_after_seconds(exc)
    return seconds if seconds is not None and math.isfinite(seconds) else None


def _raise_error(exc: Exception) -> NoReturn:
    status = getattr(exc, "status", None)
    if type(status) is not int:
        status = getattr(exc, "status_code", None)
    status = status if type(status) is int else None
    if status in (401, 403):
        raise ProviderAuthError(
            "TypeSafe authentication failed", provider="typesafe", status_code=status
        ) from None
    if status == 429:
        raise ProviderRateLimitError(
            "TypeSafe rate limit",
            provider="typesafe",
            status_code=status,
            retry_after_seconds=_retry_hint(exc),
        ) from None
    if (
        status in (408, 409)
        or (status is not None and status >= 500)
        or is_network_or_timeout_error(exc)
    ):
        raise ProviderTransientError(
            "TypeSafe transient transport failure",
            provider="typesafe",
            status_code=status,
            retry_after_seconds=_retry_hint(exc),
        ) from None
    raise _config("rejected the decision request") from None
