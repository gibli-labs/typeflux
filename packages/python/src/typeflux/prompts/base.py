from __future__ import annotations

from typing import Protocol

from typeflux.core.contracts import PromptRef, ResolvedPrompt


class PromptRegistry(Protocol):
    def resolve(self, ref: PromptRef) -> ResolvedPrompt: ...


__all__ = ["PromptRegistry"]
