from __future__ import annotations

from typing import Any


def provider_identifier(provider: Any) -> str:
    provider_name = getattr(provider, "provider_name", None)
    if isinstance(provider_name, str) and provider_name:
        return provider_name
    name = type(provider).__name__.removesuffix("Provider")
    parts: list[str] = []
    for index, char in enumerate(name):
        if index > 0 and char.isupper():
            parts.append("-")
        parts.append(char.lower())
    return "".join(parts) or "provider"


__all__ = ["provider_identifier"]
