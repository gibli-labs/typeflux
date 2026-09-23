from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from pydantic import BaseModel

from typeflux.core.artifacts import ResolvedArtifactGroup
from typeflux.core.contracts import ChatMessage, ProviderParams
from typeflux.providers.base import ProviderUsage


class FakeProvider:
    provider_name = "fake"
    supported_provider_params = frozenset(ProviderParams.empty().to_dict(include_empty=True))

    def __init__(
        self,
        responses: Iterable[BaseModel | Exception],
        *,
        usage: ProviderUsage | None = None,
    ) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.default_provider_params = ProviderParams()
        self.usage = usage

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
    ) -> BaseModel:
        self.calls.append(
            {
                "messages": messages,
                "output_schema": output_schema,
                "model": model,
                "temperature": temperature,
                "provider_params": provider_params,
                "metadata": metadata,
                "artifacts": artifacts,
            }
        )
        if not self._responses:
            raise AssertionError("FakeProvider has no responses left")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if usage_sink is not None and self.usage is not None:
            usage_sink(self.usage)
        return response


__all__ = ["FakeProvider"]
