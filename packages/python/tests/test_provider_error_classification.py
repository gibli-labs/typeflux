"""Cross-provider error-classification behavior.

All three built-in providers classify SDK/transport failures into the same
typed ``ProviderError`` hierarchy with the same status-code semantics. The
uniform matrix lives here, parameterized over the providers; anything
provider-specific (SDK error-type name mapping, retry-after header parsing,
reason sanitization wording) stays in the per-provider test modules.

Each provider's SDK surfaces the HTTP status on a different attribute
(``status_code`` for Anthropic/OpenAI, ``code`` for google-genai), so each
parameter case carries its own realistic error factory.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from typeflux.core.contracts import ChatMessage
from typeflux.providers import AnthropicProvider, GeminiProvider, OpenAIProvider
from typeflux.providers.base import ModelProvider
from typeflux.providers.errors import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTransientError,
)


class Output(BaseModel):
    label: str


class _SdkStatusError(Exception):
    """Anthropic/OpenAI SDK errors carry the HTTP status on ``status_code``."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class _GeminiApiError(Exception):
    """google-genai APIError carries the HTTP status on ``code``."""

    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"api error {code}")


class _FailingAnthropicMessages:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def parse(self, **kwargs: Any) -> Any:
        raise self.error


class _FailingInstructorCompletions:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def create(self, **kwargs: Any) -> Any:
        raise self.error

    def create_with_completion(self, **kwargs: Any) -> Any:
        raise self.error


class _FailingInstructorClient:
    def __init__(self, error: Exception) -> None:
        completions = _FailingInstructorCompletions(error)
        self.completions = completions
        self.chat = type("Chat", (), {"completions": completions})()


class _FailingGeminiModels:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def generate_content(self, **kwargs: Any) -> Any:
        raise self.error


class _FailingGeminiClient:
    def __init__(self, error: Exception) -> None:
        self.models = _FailingGeminiModels(error)


def _failing_anthropic(error: Exception) -> ModelProvider:
    return AnthropicProvider(
        default_model="claude-test",
        anthropic_client=SimpleNamespace(messages=_FailingAnthropicMessages(error)),
    )


def _failing_openai(error: Exception) -> ModelProvider:
    return OpenAIProvider(
        default_model="test-model", instructor_client=_FailingInstructorClient(error)
    )


def _failing_gemini(error: Exception) -> ModelProvider:
    return GeminiProvider(default_model="gemini-test", genai_client=_FailingGeminiClient(error))


PROVIDER_CASES = [
    pytest.param(
        _failing_anthropic,
        lambda status: _SdkStatusError(f"status {status}", status),
        id="anthropic",
    ),
    pytest.param(
        _failing_openai,
        lambda status: _SdkStatusError(f"status {status}", status),
        id="openai",
    ),
    pytest.param(_failing_gemini, lambda status: _GeminiApiError(status), id="gemini"),
]

_provider_cases = pytest.mark.parametrize(("make_provider", "make_error"), PROVIDER_CASES)
# The error factory only matters for status-code classification; tests that
# raise their own exceptions parametrize over the providers alone.
_provider_only_cases = pytest.mark.parametrize(
    "make_provider", [pytest.param(case.values[0], id=case.id) for case in PROVIDER_CASES]
)


def _call(make_provider: Callable[[Exception], ModelProvider], error: Exception) -> None:
    make_provider(error).structured_call(
        messages=[ChatMessage(role="user", content="x")],
        output_schema=Output,
    )


@_provider_cases
@pytest.mark.parametrize(
    ("status", "expected", "retryable"),
    [
        (429, ProviderRateLimitError, True),
        (401, ProviderAuthError, False),
        (403, ProviderAuthError, False),
        (400, ProviderConfigError, False),
        (404, ProviderConfigError, False),
        (408, ProviderTransientError, True),
        (409, ProviderTransientError, True),
        (500, ProviderTransientError, True),
        (503, ProviderTransientError, True),
    ],
)
def test_status_code_classification(
    make_provider: Callable[[Exception], ModelProvider],
    make_error: Callable[[int], Exception],
    status: int,
    expected: type[ProviderError],
    retryable: bool,
) -> None:
    with pytest.raises(expected) as exc_info:
        _call(make_provider, make_error(status))
    assert exc_info.value.retryable is retryable


@_provider_only_cases
@pytest.mark.parametrize("error", [ConnectionError("reset"), TimeoutError("slow")])
def test_network_and_timeout_errors_are_transient(
    make_provider: Callable[[Exception], ModelProvider],
    error: Exception,
) -> None:
    with pytest.raises(ProviderTransientError) as exc_info:
        _call(make_provider, error)
    assert exc_info.value.retryable is True


@_provider_only_cases
def test_unknown_error_wraps_as_non_retryable_provider_error(
    make_provider: Callable[[Exception], ModelProvider],
) -> None:
    with pytest.raises(ProviderError) as exc_info:
        _call(make_provider, RuntimeError("mystery"))
    assert type(exc_info.value) is ProviderError
    assert exc_info.value.retryable is False


@_provider_only_cases
def test_unknown_error_is_not_classified_from_message_text(
    make_provider: Callable[[Exception], ModelProvider],
) -> None:
    # Classification keys on exception types and status codes, never on
    # message wording — "rate limit" in the text must not upgrade the error.
    with pytest.raises(ProviderError) as exc_info:
        _call(make_provider, RuntimeError("invalid connection config hit rate limit"))
    assert type(exc_info.value) is ProviderError
    assert exc_info.value.retryable is False


@_provider_only_cases
def test_validation_error_propagates_unwrapped(
    make_provider: Callable[[Exception], ModelProvider],
) -> None:
    try:
        Output.model_validate({})
    except ValidationError as exc:
        validation_error = exc
    else:  # pragma: no cover - defensive assertion.
        raise AssertionError("expected validation error")

    with pytest.raises(ValidationError) as exc_info:
        _call(make_provider, validation_error)

    assert exc_info.value is validation_error
