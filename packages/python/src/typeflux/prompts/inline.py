from __future__ import annotations

from collections.abc import Mapping

from typeflux.core.contracts import ChatMessage, PromptRef, ResolvedPrompt
from typeflux.prompts.errors import PromptNotFoundError


class InlinePromptRegistry:
    def __init__(
        self,
        prompts: Mapping[
            str | PromptRef, str | ChatMessage | ResolvedPrompt | tuple[ChatMessage, ...]
        ],
    ) -> None:
        self._prompts = dict(prompts)

    def resolve(self, ref: PromptRef) -> ResolvedPrompt:
        value = self._prompts.get(ref)
        if value is None:
            value = self._prompts.get(ref.name)
        if value is None:
            raise PromptNotFoundError(f"prompt not found: {ref.name}@{ref.selector}", ref=ref)

        messages: tuple[ChatMessage, ...]
        if isinstance(value, ResolvedPrompt):
            return value
        if isinstance(value, ChatMessage):
            messages = (value,)
        elif isinstance(value, str):
            messages = (ChatMessage(role="user", content=value),)
        else:
            messages = tuple(value)
        return ResolvedPrompt(
            ref=ref,
            messages=messages,
            resolved_version=str(ref.version) if ref.version is not None else None,
        )


__all__ = ["InlinePromptRegistry"]
