from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

from pydantic import BaseModel

from typeflux.core.contracts import AIActivity, ChatMessage, ProviderParams
from typeflux.manifests import (
    ActivityExecutionManifest,
    AIActivityManifest,
    AIInvocationContext,
)


class ObservationHandle(Protocol):
    def update_output(self, output_value: BaseModel) -> None: ...

    def update_error(self, error: BaseException) -> None: ...

    def update_metadata(self, metadata: dict[str, Any]) -> None: ...


class ActivityObservation(ObservationHandle, Protocol):
    @contextmanager
    def observe_generation(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        metadata: dict[str, Any],
        validation_attempt: int,
        model: str | None,
        temperature: float | None,
        provider_params: ProviderParams | None = None,
    ) -> Iterator[ObservationHandle]: ...

    @contextmanager
    def observe_hook(
        self,
        *,
        activity_input: BaseModel,
        llm_output: BaseModel,
        metadata: dict[str, Any],
    ) -> Iterator[ObservationHandle]: ...


class AIActivityObserver(Protocol):
    @contextmanager
    def observe_activity(
        self,
        *,
        activity: AIActivity,
        input_value: BaseModel,
        manifest: AIActivityManifest,
        execution_manifest: ActivityExecutionManifest,
        invocation_context: AIInvocationContext | None,
    ) -> Iterator[ActivityObservation]: ...

    def flush(self) -> None: ...

    def redact_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]: ...


class NoOpObservation:
    def update_output(self, output_value: BaseModel) -> None:
        return None

    def update_error(self, error: BaseException) -> None:
        return None

    def update_metadata(self, metadata: dict[str, Any]) -> None:
        return None


class NoOpActivityObservation(NoOpObservation):
    @contextmanager
    def observe_generation(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        metadata: dict[str, Any],
        validation_attempt: int,
        model: str | None,
        temperature: float | None,
        provider_params: ProviderParams | None = None,
    ) -> Iterator[ObservationHandle]:
        yield NoOpObservation()

    @contextmanager
    def observe_hook(
        self,
        *,
        activity_input: BaseModel,
        llm_output: BaseModel,
        metadata: dict[str, Any],
    ) -> Iterator[ObservationHandle]:
        yield NoOpObservation()


class NoOpObserver:
    @contextmanager
    def observe_activity(
        self,
        *,
        activity: AIActivity,
        input_value: BaseModel,
        manifest: AIActivityManifest,
        execution_manifest: ActivityExecutionManifest,
        invocation_context: AIInvocationContext | None,
    ) -> Iterator[ActivityObservation]:
        del execution_manifest
        yield NoOpActivityObservation()

    def flush(self) -> None:
        return None

    def redact_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        return metadata


__all__ = [
    "AIActivityObserver",
    "ActivityObservation",
    "NoOpObserver",
    "ObservationHandle",
]
