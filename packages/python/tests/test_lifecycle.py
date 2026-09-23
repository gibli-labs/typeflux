from __future__ import annotations

import asyncio
import contextvars
import threading
import time

import pytest

from typeflux.execution.lifecycle import (
    NO_OP_LIFECYCLE,
    ActivityCancelled,
    heartbeat_interval_for,
    heartbeating,
    heartbeating_async,
)


class _CountingLifecycle:
    def __init__(self, interval: float | None = 0.01) -> None:
        self.heartbeat_interval_seconds = interval
        self.beats = 0
        self.beat_event = threading.Event()

    def heartbeat(self) -> None:
        self.beats += 1
        self.beat_event.set()

    def raise_if_cancelled(self) -> None:
        return None


def test_no_op_lifecycle_does_nothing() -> None:
    NO_OP_LIFECYCLE.heartbeat()
    NO_OP_LIFECYCLE.raise_if_cancelled()  # must not raise
    assert NO_OP_LIFECYCLE.heartbeat_interval_seconds is None


def test_activity_cancelled_is_a_cancelled_error_not_an_exception() -> None:
    # Subclassing CancelledError (and NOT Exception) lets it propagate past
    # `except Exception` and read as cancellation to Temporal.
    assert issubclass(ActivityCancelled, asyncio.CancelledError)
    assert not issubclass(ActivityCancelled, Exception)


def test_heartbeat_interval_for_derives_a_third_with_a_floor() -> None:
    assert heartbeat_interval_for(None) is None
    assert heartbeat_interval_for(0) is None
    assert heartbeat_interval_for(-5) is None
    assert heartbeat_interval_for(9) == 3.0
    assert heartbeat_interval_for(2) == 1.0  # floored, not 0.67


def test_heartbeat_interval_for_caps_below_a_sub_floor_timeout() -> None:
    # For any timeout under 2s the half-timeout cap beats the floor, keeping the
    # FIRST beat strictly before the deadline (parity with the TS SDK's
    # heartbeatIntervalMs). 1.5 pins the (1, 2) region where the floor alone
    # would have returned 1.0.
    assert heartbeat_interval_for(1.5) == 0.75
    assert heartbeat_interval_for(1) == 0.5
    assert heartbeat_interval_for(0.9) == 0.45


def test_heartbeat_interval_for_never_spins_on_a_pathological_timeout() -> None:
    # A sub-100ms timeout (validated only as > 0) must not produce a kHz loop
    # cadence — the 50ms absolute floor bounds local churn; the activity is
    # doomed to heartbeat-timeout either way.
    assert heartbeat_interval_for(0.005) == 0.05


def test_heartbeating_emits_until_the_context_exits() -> None:
    lifecycle = _CountingLifecycle(0.01)
    with heartbeating(lifecycle):
        assert lifecycle.beat_event.wait(2.0), "expected at least one heartbeat"
    beats_at_exit = lifecycle.beats
    time.sleep(0.05)
    # The background thread was stopped and joined on exit, so no more beats.
    assert lifecycle.beats == beats_at_exit


def test_heartbeating_disabled_without_an_interval() -> None:
    for interval in (None, 0):
        lifecycle = _CountingLifecycle(interval)
        with heartbeating(lifecycle):
            time.sleep(0.03)
        assert lifecycle.beats == 0


def test_heartbeating_runs_under_the_callers_context() -> None:
    # The background thread must inherit the caller's contextvars (Temporal binds
    # the activity context that way) — a plain threading.Thread would not, so
    # the real heartbeat() would raise "Not in activity context".
    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="unset")
    marker.set("set-by-caller")
    seen: list[str] = []

    class _ContextReadingLifecycle:
        heartbeat_interval_seconds = 0.01

        def heartbeat(self) -> None:
            seen.append(marker.get())

        def raise_if_cancelled(self) -> None:
            return None

    with heartbeating(_ContextReadingLifecycle()):
        time.sleep(0.05)

    assert seen, "expected at least one heartbeat"
    assert all(value == "set-by-caller" for value in seen)


def test_heartbeating_swallows_heartbeat_errors() -> None:
    class _Boom:
        heartbeat_interval_seconds = 0.01

        def heartbeat(self) -> None:
            raise RuntimeError("boom")

        def raise_if_cancelled(self) -> None:
            return None

    # A heartbeat failure must never escape the background thread and kill the
    # in-flight call.
    with heartbeating(_Boom()):
        time.sleep(0.05)


@pytest.mark.asyncio
async def test_heartbeating_async_emits_until_the_context_exits() -> None:
    lifecycle = _CountingLifecycle(0.01)
    async with heartbeating_async(lifecycle):
        await asyncio.sleep(0.05)
    beats_at_exit = lifecycle.beats
    assert beats_at_exit >= 1
    await asyncio.sleep(0.05)
    # The background task was cancelled on exit.
    assert lifecycle.beats == beats_at_exit


@pytest.mark.asyncio
async def test_heartbeating_async_disabled_without_an_interval() -> None:
    lifecycle = _CountingLifecycle(None)
    async with heartbeating_async(lifecycle):
        await asyncio.sleep(0.03)
    assert lifecycle.beats == 0
