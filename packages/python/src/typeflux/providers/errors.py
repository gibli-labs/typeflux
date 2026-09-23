from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from typeflux.core.errors import TypefluxError


@dataclass
class ProviderError(TypefluxError):
    reason: str
    provider: str | None = None
    retryable: bool = False
    status_code: int | None = None
    original: BaseException | None = None
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        super().__init__(self.reason)

    def to_metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "failed",
            "error_type": type(self).__name__,
            "retryable": self.retryable,
            "reason": self.reason,
        }
        if self.provider is not None:
            payload["provider"] = self.provider
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        return payload


class ProviderTransientError(ProviderError):
    def __init__(
        self,
        reason: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        original: BaseException | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(
            reason=reason,
            provider=provider,
            retryable=True,
            status_code=status_code,
            original=original,
            retry_after_seconds=retry_after_seconds,
        )


class ProviderRateLimitError(ProviderTransientError):
    pass


class ProviderAuthError(ProviderError):
    def __init__(
        self,
        reason: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        original: BaseException | None = None,
    ) -> None:
        super().__init__(
            reason=reason,
            provider=provider,
            retryable=False,
            status_code=status_code,
            original=original,
        )


class ProviderConfigError(ProviderError):
    def __init__(
        self,
        reason: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        original: BaseException | None = None,
    ) -> None:
        super().__init__(
            reason=reason,
            provider=provider,
            retryable=False,
            status_code=status_code,
            original=original,
        )


class ProviderPolicyError(ProviderConfigError):
    pass


class ProviderCacheUnavailableError(ProviderError):
    """A referenced provider-side session cache is gone (expired/deleted) at call
    time (#368). Not retryable as-is — retrying the same stale reference fails
    identically; the runtime recovers by re-running the item uncached (full
    context). Distinct from ProviderConfigError so the worker can catch it
    specifically rather than treating it as a terminal misconfiguration."""

    def __init__(
        self,
        reason: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        original: BaseException | None = None,
    ) -> None:
        super().__init__(
            reason=reason,
            provider=provider,
            retryable=False,
            status_code=status_code,
            original=original,
        )


__all__ = [
    "ProviderAuthError",
    "ProviderCacheUnavailableError",
    "ProviderConfigError",
    "ProviderError",
    "ProviderPolicyError",
    "ProviderRateLimitError",
    "ProviderTransientError",
]
