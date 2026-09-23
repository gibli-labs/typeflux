from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from typeflux.core.contracts import PromptRef
from typeflux.core.errors import TypefluxError


@dataclass
class PromptResolutionError(TypefluxError):
    reason: str
    ref: PromptRef | None = None
    retryable: bool = False
    status_code: int | None = None
    original: BaseException | None = None

    def __post_init__(self) -> None:
        super().__init__(self.reason)

    def to_metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "failed",
            "error_type": type(self).__name__,
            "retryable": self.retryable,
            "reason": self.reason,
        }
        if self.ref is not None:
            payload["prompt_ref"] = {
                "name": self.ref.name,
                "version": self.ref.version,
                "label": self.ref.label,
            }
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        return payload


class PromptNotFoundError(PromptResolutionError):
    def __init__(
        self,
        reason: str,
        *,
        ref: PromptRef | None = None,
        status_code: int | None = None,
        original: BaseException | None = None,
    ) -> None:
        super().__init__(
            reason=reason,
            ref=ref,
            retryable=False,
            status_code=status_code,
            original=original,
        )


class PromptRegistryUnavailableError(PromptResolutionError):
    def __init__(
        self,
        reason: str,
        *,
        ref: PromptRef | None = None,
        status_code: int | None = None,
        original: BaseException | None = None,
    ) -> None:
        super().__init__(
            reason=reason,
            ref=ref,
            retryable=True,
            status_code=status_code,
            original=original,
        )


class PromptRegistryAuthError(PromptResolutionError):
    def __init__(
        self,
        reason: str,
        *,
        ref: PromptRef | None = None,
        status_code: int | None = None,
        original: BaseException | None = None,
    ) -> None:
        super().__init__(
            reason=reason,
            ref=ref,
            retryable=False,
            status_code=status_code,
            original=original,
        )


class PromptRegistryConfigError(PromptResolutionError):
    def __init__(
        self,
        reason: str,
        *,
        ref: PromptRef | None = None,
        status_code: int | None = None,
        original: BaseException | None = None,
    ) -> None:
        super().__init__(
            reason=reason,
            ref=ref,
            retryable=False,
            status_code=status_code,
            original=original,
        )


__all__ = [
    "PromptNotFoundError",
    "PromptRegistryAuthError",
    "PromptRegistryConfigError",
    "PromptRegistryUnavailableError",
    "PromptResolutionError",
]
