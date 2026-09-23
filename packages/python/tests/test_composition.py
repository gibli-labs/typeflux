"""Tests for the code-defined orchestration composition primitives (#385)."""

from __future__ import annotations

import asyncio

import pytest

from typeflux.composition import fan_out, ground_with_search, with_fallback


@pytest.mark.asyncio
async def test_fan_out_preserves_input_order() -> None:
    async def double(value: int) -> int:
        # Later items sleep less, so completion order != input order.
        await asyncio.sleep((10 - value) * 0.001)
        return value * 2

    assert await fan_out([1, 2, 3, 4], double, concurrency=4) == [2, 4, 6, 8]


@pytest.mark.asyncio
async def test_fan_out_caps_concurrency() -> None:
    in_flight = 0
    peak = 0

    async def track(value: int) -> int:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.005)
        in_flight -= 1
        return value

    await fan_out(list(range(12)), track, concurrency=3)
    assert peak <= 3


@pytest.mark.asyncio
async def test_fan_out_rejects_non_positive_concurrency() -> None:
    async def identity(value: int) -> int:
        return value

    with pytest.raises(ValueError, match="concurrency must be >= 1"):
        await fan_out([1], identity, concurrency=0)


@pytest.mark.asyncio
async def test_fan_out_propagates_item_exception() -> None:
    async def boom(value: int) -> int:
        if value == 2:
            raise RuntimeError("item 2 failed")
        return value

    with pytest.raises(RuntimeError, match="item 2 failed"):
        await fan_out([1, 2, 3], boom, concurrency=2)


@pytest.mark.asyncio
async def test_fan_out_cancels_pending_siblings_on_failure() -> None:
    completed: list[int] = []

    async def item(value: int) -> int:
        if value == 0:
            raise RuntimeError("first item failed")
        await asyncio.sleep(0.05)
        completed.append(value)
        return value

    # concurrency=1: item 0 fails before 1/2 finish; they must be cancelled.
    with pytest.raises(RuntimeError, match="first item failed"):
        await fan_out([0, 1, 2], item, concurrency=1)

    await asyncio.sleep(0.1)  # any leaked sibling would have completed by now
    assert completed == []


@pytest.mark.asyncio
async def test_with_fallback_returns_primary_when_it_succeeds() -> None:
    async def primary() -> str:
        return "primary"

    async def fallback(exc: BaseException) -> str:
        return "fallback"

    assert await with_fallback(primary, fallback) == "primary"


@pytest.mark.asyncio
async def test_with_fallback_degrades_on_configured_exception() -> None:
    async def primary() -> list[int]:
        raise ValueError("boom")

    async def fallback(exc: BaseException) -> list[int]:
        assert isinstance(exc, ValueError)
        return []

    assert await with_fallback(primary, fallback, exceptions=ValueError) == []


@pytest.mark.asyncio
async def test_with_fallback_reraises_unconfigured_exception() -> None:
    async def primary() -> str:
        raise RuntimeError("nope")

    async def fallback(exc: BaseException) -> str:
        return "fallback"

    with pytest.raises(RuntimeError, match="nope"):
        await with_fallback(primary, fallback, exceptions=ValueError)


@pytest.mark.asyncio
async def test_fan_out_composed_with_fallback_for_per_item_resilience() -> None:
    async def resilient(value: int) -> int:
        async def primary() -> int:
            if value == 2:
                raise RuntimeError("item 2 failed")
            return value * 10

        async def degrade(exc: BaseException) -> int:
            return -1

        return await with_fallback(primary, degrade)

    assert await fan_out([1, 2, 3], resilient, concurrency=2) == [10, -1, 30]


@pytest.mark.asyncio
async def test_ground_with_search_folds_sync_search_results_into_input() -> None:
    def search(query: str) -> list[str]:
        return [f"doc:{query}:1", f"doc:{query}:2"]

    def ground(query: str, results: list[str]) -> dict[str, object]:
        return {"query": query, "context": results}

    grounded = await ground_with_search("claim-a", search=search, ground=ground)
    assert grounded == {"query": "claim-a", "context": ["doc:claim-a:1", "doc:claim-a:2"]}


@pytest.mark.asyncio
async def test_ground_with_search_awaits_an_async_search() -> None:
    async def search(query: str) -> list[str]:
        await asyncio.sleep(0)
        return [f"hit:{query}"]

    grounded = await ground_with_search("q", search=search, ground=lambda q, results: (q, results))
    assert grounded == ("q", ["hit:q"])


@pytest.mark.asyncio
async def test_ground_with_search_handles_empty_results() -> None:
    grounded = await ground_with_search(
        "q", search=lambda _q: [], ground=lambda q, results: {"q": q, "n": len(results)}
    )
    assert grounded == {"q": "q", "n": 0}
