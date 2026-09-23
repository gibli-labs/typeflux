"""Cooperative activity heartbeating and cancellation (#208).

A long provider call must keep a Temporal activity heartbeating (so Temporal can
deliver cancellation and the heartbeat timeout won't fire) and must abort
promptly when the workflow is cancelled instead of waiting out the full
``start_to_close_timeout``.

The executor stays decoupled from ``temporalio``: it talks only to the
:class:`ActivityLifecycle` protocol below, which the worker layer backs with the
real Temporal activity context. Non-Temporal callers (tests, ``.execute()``
paths) get :data:`NO_OP_LIFECYCLE`, which does nothing. The lifecycle also
carries its own heartbeat cadence, so the executor threads a single object
rather than a (lifecycle, interval) pair through every signature.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from typing import Protocol, runtime_checkable


class ActivityCancelled(asyncio.CancelledError):
    """Raised at a cooperative checkpoint when the activity has been cancelled.

    Subclasses :class:`asyncio.CancelledError` *only* (not ``Exception``) so it
    propagates past the executor's ``except Exception`` provider-error handlers
    — cancellation is a control-flow signal, not a provider error — and is
    recognized by the Temporal SDK as cancellation rather than a retryable
    activity failure.
    """


@runtime_checkable
class ActivityLifecycle(Protocol):
    """The activity-context surface the executor needs for cooperative cancel.

    ``heartbeat_interval_seconds`` is the cadence the background heartbeater uses
    (``None`` → no background heartbeat). Both methods are cheap and safe to call
    repeatedly. The worker provides a Temporal-backed implementation; everything
    else uses :data:`NO_OP_LIFECYCLE`.
    """

    heartbeat_interval_seconds: float | None

    def heartbeat(self) -> None:
        """Report liveness to Temporal (no-op off Temporal)."""

    def raise_if_cancelled(self) -> None:
        """Raise :class:`ActivityCancelled` if the activity has been cancelled."""


class _NoOpLifecycle:
    heartbeat_interval_seconds: float | None = None

    def heartbeat(self) -> None:
        return None

    def raise_if_cancelled(self) -> None:
        return None


NO_OP_LIFECYCLE: ActivityLifecycle = _NoOpLifecycle()


@contextmanager
def heartbeating(lifecycle: ActivityLifecycle) -> Iterator[None]:
    """Heartbeat in a background daemon thread while the body runs.

    Wraps a *blocking* provider call: the call holds the activity thread, so a
    separate thread emits the heartbeats. The thread runs under a *copy of the
    current context* so Temporal's activity contextvar (set when the executor
    runs via ``asyncio.to_thread``) is visible to ``heartbeat()`` — a plain
    ``threading.Thread`` would otherwise start with an empty context and every
    heartbeat would raise "Not in activity context". A heartbeat error never
    escapes (it must not kill the in-flight call). Disabled when the lifecycle's
    interval is falsy or non-positive.
    """
    interval_seconds = lifecycle.heartbeat_interval_seconds
    if not interval_seconds or interval_seconds <= 0:
        yield
        return

    stop = threading.Event()

    def _beat() -> None:
        # Event.wait returns True once stopped, so the loop exits promptly on
        # teardown and otherwise heartbeats once per interval.
        while not stop.wait(interval_seconds):
            try:
                lifecycle.heartbeat()
            except Exception:  # noqa: BLE001 - a heartbeat failure must not kill the call.
                pass

    context = contextvars.copy_context()
    thread = threading.Thread(
        target=context.run,
        args=(_beat,),
        name="typeflux-activity-heartbeat",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5.0)


@asynccontextmanager
async def heartbeating_async(lifecycle: ActivityLifecycle) -> AsyncIterator[None]:
    """Async counterpart of :func:`heartbeating` using a background task.

    The task runs in the same context as the awaiting coroutine, so Temporal's
    activity contextvar is already visible — no copy needed.
    """
    interval_seconds = lifecycle.heartbeat_interval_seconds
    if not interval_seconds or interval_seconds <= 0:
        yield
        return

    async def _beat() -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                lifecycle.heartbeat()
            except Exception:  # noqa: BLE001 - a heartbeat failure must not kill the call.
                pass

    task = asyncio.ensure_future(_beat())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def heartbeat_interval_for(heartbeat_timeout_seconds: float | None) -> float | None:
    """Derive a heartbeat cadence from the activity's heartbeat timeout.

    Heartbeat at roughly a third of the timeout so two consecutive misses are
    needed before Temporal fails the activity, with a 1s floor to avoid
    hammering Temporal on a short timeout. For any timeout under 2s the result
    is instead capped at half the timeout, so the FIRST beat lands strictly
    before the deadline (the bare floor would schedule it at/after a <=1s
    deadline and the activity would heartbeat-timeout despite the loop
    running). A 50ms absolute floor keeps a pathological sub-100ms timeout —
    validated only as > 0 — from spinning the loop at kHz; such an activity is
    doomed to heartbeat-timeout regardless. Identical for every timeout >= 2s;
    parity with the TS SDK's ``heartbeatIntervalMs``. ``None`` (no timeout) →
    no background heartbeat cadence (callers may still heartbeat at
    checkpoints).
    """
    if heartbeat_timeout_seconds is None or heartbeat_timeout_seconds <= 0:
        return None
    return min(
        max(1.0, heartbeat_timeout_seconds / 3.0),
        max(0.05, heartbeat_timeout_seconds / 2.0),
    )


__all__ = [
    "NO_OP_LIFECYCLE",
    "ActivityCancelled",
    "ActivityLifecycle",
    "heartbeat_interval_for",
    "heartbeating",
    "heartbeating_async",
]
