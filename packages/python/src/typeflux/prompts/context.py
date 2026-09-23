from __future__ import annotations

from collections.abc import Mapping
from typing import Any

LANGFUSE_PROMPT_CONTEXT_KEY = "langfuse.prompt"


def langfuse_prompt_context(prompt: Any) -> dict[str, Any]:
    return {LANGFUSE_PROMPT_CONTEXT_KEY: prompt}


def langfuse_prompt_from_context(context: Mapping[str, Any] | None) -> Any | None:
    if context is None:
        return None
    return context.get(LANGFUSE_PROMPT_CONTEXT_KEY)


__all__ = [
    "LANGFUSE_PROMPT_CONTEXT_KEY",
    "langfuse_prompt_context",
    "langfuse_prompt_from_context",
]
