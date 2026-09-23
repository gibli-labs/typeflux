from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Literal, TypeVar, cast

_WAIT_METADATA_THRESHOLD_SECONDS = 0.001
_T = TypeVar("_T")
_ItemT = TypeVar("_ItemT")
StageWorkStatus = Literal["queued", "started", "cancelled", "completed", "failed"]
StageCancelPhase = Literal["queued", "active"]
StageClock = Callable[[], float]
ProviderPolicySource = Literal["model", "provider", "default", "legacy_default", "none"]


@dataclass(frozen=True)
class ProviderCallLimits:
    max_concurrent: int | None = None
    min_interval_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.max_concurrent is not None and self.max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        if self.min_interval_seconds is not None and self.min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be >= 0")

    @property
    def enabled(self) -> bool:
        return self.max_concurrent is not None or bool(self.min_interval_seconds)


@dataclass(frozen=True)
class ProviderRetryPolicy:
    max_attempts: int = 1
    initial_backoff_seconds: float = 0.0
    max_backoff_seconds: float | None = None
    backoff_multiplier: float = 2.0
    jitter_ratio: float = 0.1
    retry_rate_limits: bool = True
    retry_transient_errors: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.initial_backoff_seconds < 0:
            raise ValueError("initial_backoff_seconds must be >= 0")
        if self.max_backoff_seconds is not None and self.max_backoff_seconds < 0:
            raise ValueError("max_backoff_seconds must be >= 0")
        if self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier must be >= 1")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")

    def backoff_seconds(self, attempt_index: int) -> float:
        delay = self.initial_backoff_seconds * (self.backoff_multiplier**attempt_index)
        if self.max_backoff_seconds is not None:
            return min(delay, self.max_backoff_seconds)
        return delay

    def retry_delay_seconds(
        self,
        attempt_index: int,
        *,
        retry_after_seconds: float | None = None,
        rng: Callable[[float, float], float] | None = None,
    ) -> float:
        # The provider's Retry-After hint floors the configured backoff (it
        # is server truth, never capped down); proportional jitter on top
        # de-synchronizes workers retrying against the same rate limit.
        delay = self.backoff_seconds(attempt_index)
        if retry_after_seconds is not None and retry_after_seconds > delay:
            delay = retry_after_seconds
        if delay and self.jitter_ratio:
            uniform = rng if rng is not None else random.uniform
            delay += delay * uniform(0.0, self.jitter_ratio)
        return delay


@dataclass(frozen=True)
class ProviderCallWait:
    queued_seconds: float = 0.0
    throttled_seconds: float = 0.0
    max_concurrent: int | None = None
    min_interval_seconds: float | None = None

    @property
    def queued(self) -> bool:
        return self.queued_seconds >= _WAIT_METADATA_THRESHOLD_SECONDS

    @property
    def throttled(self) -> bool:
        return self.throttled_seconds >= _WAIT_METADATA_THRESHOLD_SECONDS

    def to_metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "queued": self.queued,
            "throttled": self.throttled,
            "queued_seconds": self.queued_seconds,
            "throttled_seconds": self.throttled_seconds,
        }
        if self.max_concurrent is not None:
            payload["max_concurrent"] = self.max_concurrent
        if self.min_interval_seconds is not None:
            payload["min_interval_seconds"] = self.min_interval_seconds
        return payload


class ProviderCallLimiter:
    def __init__(self, limits: ProviderCallLimits | None = None) -> None:
        self.limits = limits or ProviderCallLimits()
        self._semaphore = (
            asyncio.Semaphore(self.limits.max_concurrent)
            if self.limits.max_concurrent is not None
            else None
        )
        self._rate_lock = asyncio.Lock()
        self._next_call_at = 0.0

    @asynccontextmanager
    async def limit(self) -> AsyncIterator[ProviderCallWait]:
        queued_start = monotonic()
        acquired = False
        if self._semaphore is not None:
            await self._semaphore.acquire()
            acquired = True

        queued_seconds = monotonic() - queued_start
        throttled_seconds = 0.0
        try:
            if self.limits.min_interval_seconds:
                throttled_seconds = await self._reserve_rate_slot()
            yield ProviderCallWait(
                queued_seconds=queued_seconds,
                throttled_seconds=throttled_seconds,
                max_concurrent=self.limits.max_concurrent,
                min_interval_seconds=self.limits.min_interval_seconds,
            )
        finally:
            if acquired and self._semaphore is not None:
                self._semaphore.release()

    async def _reserve_rate_slot(self) -> float:
        assert self.limits.min_interval_seconds is not None
        async with self._rate_lock:
            now = monotonic()
            wait_seconds = max(0.0, self._next_call_at - now)
            self._next_call_at = max(now, self._next_call_at) + (self.limits.min_interval_seconds)
        if wait_seconds:
            await asyncio.sleep(wait_seconds)
        return wait_seconds


@dataclass(frozen=True)
class ProviderRateLimitProviderPolicy:
    limits: ProviderCallLimits | None = None
    models: dict[str, ProviderCallLimits] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderPolicySelection:
    provider_name: str
    provider_model: str | None
    policy_source: ProviderPolicySource
    policy_key: str
    limits: ProviderCallLimits | None = None

    def to_metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider_name": self.provider_name,
            "policy_source": self.policy_source,
            "policy_key": self.policy_key,
        }
        if self.provider_model is not None:
            payload["provider_model"] = self.provider_model
        if self.limits is not None:
            if self.limits.max_concurrent is not None:
                payload["max_concurrent"] = self.limits.max_concurrent
            if self.limits.min_interval_seconds is not None:
                payload["min_interval_seconds"] = self.limits.min_interval_seconds
        return payload


@dataclass(frozen=True)
class ProviderRateLimitPolicy:
    default: ProviderCallLimits | None = None
    providers: dict[str, ProviderRateLimitProviderPolicy] = field(default_factory=dict)

    def select(
        self,
        *,
        provider_name: str,
        provider_model: str | None,
        legacy_limits: ProviderCallLimits | None = None,
    ) -> ProviderPolicySelection:
        provider_policy = self.providers.get(provider_name)
        if (
            provider_policy is not None
            and provider_model is not None
            and provider_model in provider_policy.models
        ):
            return ProviderPolicySelection(
                provider_name=provider_name,
                provider_model=provider_model,
                policy_source="model",
                policy_key=f"provider:{provider_name}/model:{provider_model}",
                limits=provider_policy.models[provider_model],
            )
        if provider_policy is not None and provider_policy.limits is not None:
            return ProviderPolicySelection(
                provider_name=provider_name,
                provider_model=provider_model,
                policy_source="provider",
                policy_key=f"provider:{provider_name}",
                limits=provider_policy.limits,
            )
        if self.default is not None:
            return ProviderPolicySelection(
                provider_name=provider_name,
                provider_model=provider_model,
                policy_source="default",
                policy_key="default",
                limits=self.default,
            )
        if legacy_limits is not None and legacy_limits.enabled:
            return ProviderPolicySelection(
                provider_name=provider_name,
                provider_model=provider_model,
                policy_source="legacy_default",
                policy_key="legacy_default",
                limits=legacy_limits,
            )
        return ProviderPolicySelection(
            provider_name=provider_name,
            provider_model=provider_model,
            policy_source="none",
            policy_key=f"none:{provider_name}:{provider_model or '*'}",
        )


class ProviderRateLimitController:
    def __init__(
        self,
        *,
        policy: ProviderRateLimitPolicy,
        legacy_limits: ProviderCallLimits | None = None,
    ) -> None:
        self.policy = policy
        self.legacy_limits = legacy_limits
        self._limiters: dict[str, ProviderCallLimiter] = {}

    def select(self, *, provider_name: str, provider_model: str | None) -> ProviderPolicySelection:
        return self.policy.select(
            provider_name=provider_name,
            provider_model=provider_model,
            legacy_limits=self.legacy_limits,
        )

    def limiter_for(self, selection: ProviderPolicySelection) -> ProviderCallLimiter | None:
        if selection.limits is None or not selection.limits.enabled:
            return None
        limiter = self._limiters.get(selection.policy_key)
        if limiter is None:
            limiter = ProviderCallLimiter(selection.limits)
            self._limiters[selection.policy_key] = limiter
        return limiter


@dataclass(frozen=True)
class WorkflowStageLimits:
    max_concurrent: int | None = None

    def __post_init__(self) -> None:
        if self.max_concurrent is not None and self.max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")

    @property
    def enabled(self) -> bool:
        return self.max_concurrent is not None


@dataclass(frozen=True)
class WorkflowStageEvent:
    stage: str
    unit_id: str
    status: StageWorkStatus
    max_concurrent: int | None = None
    queued_seconds: float = 0.0
    active_seconds: float = 0.0
    cancel_phase: StageCancelPhase | None = None
    error_type: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "stage": self.stage,
            "unit_id": self.unit_id,
            "status": self.status,
            "queued": self.status == "queued",
            "active": self.status == "started",
            "cancelled": self.status == "cancelled",
            "completed": self.status == "completed",
            "failed": self.status == "failed",
            "queued_seconds": self.queued_seconds,
            "active_seconds": self.active_seconds,
        }
        if self.max_concurrent is not None:
            payload["max_concurrent"] = self.max_concurrent
        if self.cancel_phase is not None:
            payload["cancel_phase"] = self.cancel_phase
        if self.error_type is not None:
            payload["error_type"] = self.error_type
        return payload


StageEventHandler = Callable[[WorkflowStageEvent], None]


class WorkflowStageController:
    def __init__(
        self,
        *,
        stage: str,
        limits: WorkflowStageLimits | None = None,
        on_event: StageEventHandler | None = None,
        clock: StageClock = monotonic,
    ) -> None:
        self.stage = stage
        self.limits = limits or WorkflowStageLimits()
        self._on_event = on_event
        self._clock = clock
        self._semaphore = (
            asyncio.Semaphore(self.limits.max_concurrent)
            if self.limits.max_concurrent is not None
            else None
        )

    async def run(self, unit_id: str, work: Callable[[], Awaitable[_T]]) -> _T:
        queued_start = self._clock()
        self._emit(unit_id=unit_id, status="queued")
        acquired = False
        queued_seconds = 0.0
        try:
            try:
                if self._semaphore is not None:
                    await self._semaphore.acquire()
                    acquired = True
                queued_seconds = self._clock() - queued_start
            except asyncio.CancelledError:
                queued_seconds = self._clock() - queued_start
                self._emit(
                    unit_id=unit_id,
                    status="cancelled",
                    queued_seconds=queued_seconds,
                    cancel_phase="queued",
                )
                raise

            active_start = self._clock()
            self._emit(unit_id=unit_id, status="started", queued_seconds=queued_seconds)
            try:
                result = await work()
            except asyncio.CancelledError:
                self._emit(
                    unit_id=unit_id,
                    status="cancelled",
                    queued_seconds=queued_seconds,
                    active_seconds=self._clock() - active_start,
                    cancel_phase="active",
                )
                raise
            except BaseException as exc:
                self._emit(
                    unit_id=unit_id,
                    status="failed",
                    queued_seconds=queued_seconds,
                    active_seconds=self._clock() - active_start,
                    error_type=type(exc).__name__,
                )
                raise
            else:
                self._emit(
                    unit_id=unit_id,
                    status="completed",
                    queued_seconds=queued_seconds,
                    active_seconds=self._clock() - active_start,
                )
                return result
        finally:
            if acquired and self._semaphore is not None:
                self._semaphore.release()

    async def map_ordered(
        self,
        items: Sequence[_ItemT],
        work: Callable[[_ItemT, int], Awaitable[_T]],
        *,
        unit_id: Callable[[_ItemT, int], str] | None = None,
    ) -> list[_T]:
        async def run_one(index: int, item: _ItemT) -> _T:
            resolved_unit_id = unit_id(item, index) if unit_id is not None else str(index)
            return await self.run(resolved_unit_id, lambda: work(item, index))

        results: list[Any] = [None] * len(items)
        next_index = 0
        running: dict[asyncio.Task[_T], int] = {}
        max_running = self.limits.max_concurrent or len(items) or 1
        try:
            while next_index < len(items) or running:
                while next_index < len(items) and len(running) < max_running:
                    task = asyncio.create_task(run_one(next_index, items[next_index]))
                    running[task] = next_index
                    next_index += 1
                done, _pending = await asyncio.wait(
                    running.keys(),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    index = running.pop(task)
                    results[index] = await task
        except BaseException:
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            raise
        return cast(list[_T], results)

    def _emit(
        self,
        *,
        unit_id: str,
        status: StageWorkStatus,
        queued_seconds: float = 0.0,
        active_seconds: float = 0.0,
        cancel_phase: StageCancelPhase | None = None,
        error_type: str | None = None,
    ) -> None:
        if self._on_event is None:
            return
        self._on_event(
            WorkflowStageEvent(
                stage=self.stage,
                unit_id=unit_id,
                status=status,
                max_concurrent=self.limits.max_concurrent,
                queued_seconds=queued_seconds,
                active_seconds=active_seconds,
                cancel_phase=cancel_phase,
                error_type=error_type,
            )
        )


__all__ = [
    "ProviderCallLimiter",
    "ProviderCallLimits",
    "ProviderCallWait",
    "ProviderPolicySelection",
    "ProviderPolicySource",
    "ProviderRateLimitController",
    "ProviderRateLimitPolicy",
    "ProviderRateLimitProviderPolicy",
    "ProviderRetryPolicy",
    "StageCancelPhase",
    "StageClock",
    "StageEventHandler",
    "StageWorkStatus",
    "WorkflowStageController",
    "WorkflowStageEvent",
    "WorkflowStageLimits",
]
