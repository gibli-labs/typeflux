from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from typeflux import ChatMessage
from typeflux.core.contracts import CachedSessionHandle, ProviderParams


class ReplayFixtureProvider:
    provider_name = "fake"
    default_model = "fake-replay-fixture"

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BaseModel:
        del messages, model, temperature, metadata
        return output_schema(value="replay-fixture")


class ReplayFixtureReferenceProvider:
    """Deterministic reference-style caching provider for the cached-map fixture.

    Prep returns an engaged handle with a STABLE cache id and no network, so the
    recorded history carries the full map cache bracket (#363/#368): the
    ``__prepare_cache__`` command, the per-item calls threading the handle, and —
    because the handle has a ``cache_id`` — the ``__release_cache__`` command.
    """

    provider_name = "fake-reference"
    default_model = "fake-replay-fixture"
    supports_session_cache = True
    session_cache_style = "reference"

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
        cached_session: CachedSessionHandle | None = None,
    ) -> BaseModel:
        del messages, model, temperature, metadata, cached_session
        return output_schema(value="replay-fixture")

    def prepare_cached_session(
        self,
        *,
        messages: list[ChatMessage],
        artifacts: tuple[Any, ...] = (),
        model: str | None = None,
        provider_params: ProviderParams | None = None,
        identity_hash: str,
        ttl_seconds: int | None = None,
    ) -> CachedSessionHandle:
        del messages, artifacts, provider_params
        return CachedSessionHandle(
            provider=self.provider_name,
            identity_hash=identity_hash,
            supported=True,
            style="reference",
            cache_id="cachedContents/replay-fixture",
            model=model,
            ttl_seconds=ttl_seconds,
        )

    def release_cached_session(self, handle: CachedSessionHandle) -> None:
        del handle


class ReplaySagaProvider:
    """Live-saga provider (#299): fails for ``FailModel`` (the ``charge`` step), sleeps on
    ``book_flight`` so a mid-run cancel signal can land before the last step, and returns
    normally otherwise (``book_hotel`` and the compensating ``cancel_*`` activities)."""

    provider_name = "fake-saga"
    default_model = "fake-replay-fixture"
    fail_cancel_flight = False

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BaseModel:
        del model, temperature, metadata
        text = " ".join(
            m.content if isinstance(getattr(m, "content", None), str) else "" for m in messages
        )
        if output_schema.__name__ == "FailModel":
            raise RuntimeError("charge declined (saga fixture)")
        if "book_flight" in text:
            import time

            # Slow enough that a cancel signal sent after `book_flight` starts lands before
            # the workflow reaches the last step (the cancellation-unwind case).
            time.sleep(2.0)
        if "cancel_flight" in text and self.fail_cancel_flight:
            raise RuntimeError("cancel_flight failed (saga fixture)")
        return output_schema(value="replay-fixture")


class ReplaySagaPartialProvider(ReplaySagaProvider):
    """Compensation-failure (partial) variant (#299): ``cancel_flight`` raises, so the unwind
    records ``compensation_failed`` for book_flight and terminal ``compensation_status`` = partial."""

    provider_name = "fake-saga-partial"
    fail_cancel_flight = True


class ReplayMapSagaProvider:
    """Map-saga provider (#299 review): fails a map item whose interpolated value contains
    ``boom`` (so a mid-fan-out failure can be provoked deterministically), succeeds otherwise."""

    provider_name = "fake-map-saga"
    default_model = "fake-replay-fixture"

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BaseModel:
        del output_schema, model, temperature, metadata
        text = " ".join(
            m.content if isinstance(getattr(m, "content", None), str) else "" for m in messages
        )
        if "boom" in text:
            raise RuntimeError("item boom failed (map saga)")
        from replay_demo_project.schemas import MiddleModel

        return MiddleModel(value="processed")


class ReplayFixtureCompensationProvider:
    """Deterministic provider for the compensation fixture (#299): raises for the ``FailModel``
    output schema (the ``charge`` step fails, triggering the compensation unwind) and returns
    normally for every other schema (``book`` and the compensating ``cancel_book`` succeed)."""

    provider_name = "fake-compensation"
    default_model = "fake-replay-fixture"

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BaseModel:
        del messages, model, temperature, metadata
        if output_schema.__name__ == "FailModel":
            raise RuntimeError("charge failed (replay compensation fixture)")
        return output_schema(value="replay-fixture")
