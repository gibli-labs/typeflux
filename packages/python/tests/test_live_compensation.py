"""LIVE proof (#299 SLICE 1): first-class YAML compensation end-to-end on a real Temporal
dev server — the Python edition of the TS live coverage (live-compensation.test.ts).

A three-step saga (book_hotel -> book_flight -> charge) runs via ``build_runtime`` against
``localhost:7233``. ``charge`` fails, so the interpreter unwinds the compensation LIFO in
reverse — ``cancel_flight`` then ``cancel_hotel`` — before re-raising the ORIGINAL failure.
Covered: the failure-unwind reverse order (complete), a compensation failure (partial with the
original error preserved), and the cancellation-unwind case (D299-2a).

The compensation activity execution order is proven through the ordered
``compensation_started``/``compensation_completed``/``compensation_failed`` lifecycle events
(each is recorded immediately before/after its compensation activity runs in the sequential
LIFO loop, so the event order IS the activity order).

Requires (gated by the ``live`` marker + ``TYPEFLUX_LIVE_TEMPORAL=1``):
- a local Temporal dev server on ``localhost:7233``

Run: TYPEFLUX_LIVE_TEMPORAL=1 uv run --extra live pytest -m live -k live_compensation
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.live

LIVE = os.environ.get("TYPEFLUX_LIVE_TEMPORAL") == "1"
FIXTURES_DIR = Path(__file__).resolve().parent / "replay_fixtures"


def _error_chain(exc: BaseException) -> str:
    """The joined message chain of a WorkflowFailureError (cause -> cause), so a test can assert
    the ORIGINAL failure surfaced through the durable-execution wrapper."""
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        parts.append(str(cur))
        nxt = getattr(cur, "cause", None) or cur.__cause__
        cur = nxt if isinstance(nxt, BaseException) else None
    return " | ".join(parts)


def _compensation_events(status: dict) -> list[tuple[str, str]]:
    return [
        (event["event"], event["step_id"])
        for event in status["events"]
        if event["event"].startswith("compensation_")
    ]


def _prime_demo_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(FIXTURES_DIR))
    for name in tuple(sys.modules):
        if name == "replay_demo_project" or name.startswith("replay_demo_project."):
            del sys.modules[name]


@pytest.mark.asyncio
async def test_live_compensation_unwinds_in_reverse_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not LIVE:
        pytest.skip("live Temporal dev-server test; set TYPEFLUX_LIVE_TEMPORAL=1 and run -m live")
    _prime_demo_project(monkeypatch)
    from replay_demo_project.schemas import InputModel

    from typeflux.yaml import build_runtime, load_yaml_spec

    runtime = await build_runtime(load_yaml_spec(FIXTURES_DIR / "saga.yaml"))
    workflow_id = f"live-saga-fail-{uuid4().hex[:8]}"
    async with runtime.worker.build_worker():
        handle = await runtime.start_workflow(InputModel(value="start"), id=workflow_id)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011 - assert the ORIGINAL error below
            await handle.result()
        # The ORIGINAL failure (charge), never a compensation error, surfaces.
        assert "charge declined" in _error_chain(excinfo.value)
        status = await runtime.client.get_workflow_handle(workflow_id).query(
            "typeflux_lifecycle_status"
        )
        assert status["state"] == "failed"
        assert status["compensation_status"] == "complete"
        # Reverse step order: book_flight's compensation (cancel_flight) BEFORE book_hotel's.
        assert _compensation_events(status) == [
            ("compensation_started", "book_flight"),
            ("compensation_completed", "book_flight"),
            ("compensation_started", "book_hotel"),
            ("compensation_completed", "book_hotel"),
        ]


@pytest.mark.asyncio
async def test_live_compensation_records_partial_and_preserves_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not LIVE:
        pytest.skip("live Temporal dev-server test; set TYPEFLUX_LIVE_TEMPORAL=1 and run -m live")
    _prime_demo_project(monkeypatch)
    from replay_demo_project.schemas import InputModel

    from typeflux.yaml import build_runtime, load_yaml_spec

    runtime = await build_runtime(load_yaml_spec(FIXTURES_DIR / "saga_partial.yaml"))
    workflow_id = f"live-saga-partial-{uuid4().hex[:8]}"
    async with runtime.worker.build_worker():
        handle = await runtime.start_workflow(InputModel(value="start"), id=workflow_id)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            await handle.result()
        # A compensation failure NEVER masks the original error.
        chain = _error_chain(excinfo.value)
        assert "charge declined" in chain
        assert "cancel_flight failed" not in chain
        status = await runtime.client.get_workflow_handle(workflow_id).query(
            "typeflux_lifecycle_status"
        )
        assert status["state"] == "failed"
        assert status["compensation_status"] == "partial"
        assert _compensation_events(status) == [
            ("compensation_started", "book_flight"),
            ("compensation_failed", "book_flight"),
            ("compensation_started", "book_hotel"),
            ("compensation_completed", "book_hotel"),
        ]


@pytest.mark.asyncio
async def test_live_map_saga_compensates_item_that_succeeded_before_a_later_item_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #299 review MUST-FIX 1a (live): a map over 3 items at concurrency 1 — item 0 succeeds, item 1
    # ("boom") fails. Item 0's per-item compensation must STILL run (the pre-fix code pushed only
    # after the whole map collected, orphaning item 0 on a mid-fan-out failure).
    if not LIVE:
        pytest.skip("live Temporal dev-server test; set TYPEFLUX_LIVE_TEMPORAL=1 and run -m live")
    _prime_demo_project(monkeypatch)
    from replay_demo_project.schemas import BatchInputModel, InputModel

    from typeflux.yaml import build_runtime, load_yaml_spec

    runtime = await build_runtime(load_yaml_spec(FIXTURES_DIR / "saga_map.yaml"))
    workflow_id = f"live-mapsaga-{uuid4().hex[:8]}"
    async with runtime.worker.build_worker():
        handle = await runtime.start_workflow(
            BatchInputModel(
                items=[InputModel(value="ok0"), InputModel(value="boom"), InputModel(value="ok2")]
            ),
            id=workflow_id,
        )
        with pytest.raises(Exception):  # noqa: B017,PT011 - the map fails on item "boom"
            await handle.result()
        status = await runtime.client.get_workflow_handle(workflow_id).query(
            "typeflux_lifecycle_status"
        )
        assert status["compensation_status"] == "complete"
        # Serial: only item 0 completed before item 1 failed -> exactly one compensation (map step "process").
        assert _compensation_events(status) == [
            ("compensation_started", "process"),
            ("compensation_completed", "process"),
        ]


@pytest.mark.asyncio
async def test_live_compensation_unwinds_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not LIVE:
        pytest.skip("live Temporal dev-server test; set TYPEFLUX_LIVE_TEMPORAL=1 and run -m live")
    _prime_demo_project(monkeypatch)
    from replay_demo_project.schemas import InputModel

    from typeflux.yaml import build_runtime, load_yaml_spec

    runtime = await build_runtime(load_yaml_spec(FIXTURES_DIR / "saga.yaml"))
    workflow_id = f"live-saga-cancel-{uuid4().hex[:8]}"
    async with runtime.worker.build_worker():
        handle = await runtime.start_workflow(InputModel(value="start"), id=workflow_id)
        # Wait until book_flight (the slow step) has started, then cancel mid-flight so the
        # workflow unwinds BOTH completed bookings (D299-2a) instead of reaching `charge`.
        for _ in range(100):
            status = await handle.query("typeflux_lifecycle_status")
            started = [e["step_id"] for e in status["events"] if e["event"] == "step_started"]
            if "book_flight" in started:
                break
            await asyncio.sleep(0.1)
        await runtime.request_lifecycle_cancel(workflow_id, reason="operator stop")
        with pytest.raises(Exception):  # noqa: B017,PT011 - terminal cancellation
            await handle.result()
        status = await runtime.client.get_workflow_handle(workflow_id).query(
            "typeflux_lifecycle_status"
        )
        assert status["state"] == "cancelled"
        assert status["compensation_status"] == "complete"
        assert _compensation_events(status) == [
            ("compensation_started", "book_flight"),
            ("compensation_completed", "book_flight"),
            ("compensation_started", "book_hotel"),
            ("compensation_completed", "book_hotel"),
        ]
