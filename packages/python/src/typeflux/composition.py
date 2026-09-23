"""Composition primitives for code-defined orchestration (#385).

Typed async helpers for DAG-shaped multi-agent pipelines, usable inside a
hand-written ``@workflow.defn`` (or anywhere async) so fan-out and fallback do
not require hand-rolled ``asyncio`` plumbing. Code-first: plain typed helpers,
not a new DSL.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

__all__ = ["fan_out", "ground_with_search", "with_fallback"]

_T = TypeVar("_T")
_R = TypeVar("_R")
_G = TypeVar("_G")


async def fan_out(
    items: Sequence[_T],
    fn: Callable[[_T], Awaitable[_R]],
    *,
    concurrency: int,
) -> list[_R]:
    """Run ``fn`` over ``items`` with at most ``concurrency`` calls in flight.

    The bounded parallel map (a production adopter's bounded per-claim pattern). Results are
    returned in the same order as ``items``. An exception from any item
    propagates (wrap ``fn`` with :func:`with_fallback` for per-item resilience);
    when it does, the remaining (in-flight or queued) items are cancelled so a
    failed fan-out does not keep scheduling work or running side effects.
    """

    if concurrency < 1:
        raise ValueError("fan_out concurrency must be >= 1")
    semaphore = asyncio.Semaphore(concurrency)

    async def _run(item: _T) -> _R:
        async with semaphore:
            return await fn(item)

    tasks = [asyncio.ensure_future(_run(item)) for item in items]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        # gather propagates the first exception but leaves siblings running;
        # cancel and drain them so no extra item runs after the failure.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def ground_with_search(
    input_value: _T,
    *,
    search: Callable[[_T], Sequence[_R] | Awaitable[Sequence[_R]]],
    ground: Callable[[_T, list[_R]], _G],
) -> _G:
    """Retrieve context for ``input_value``, then fold it into a grounded input.

    The search->prompt pattern (#399): run ``search(input_value)`` to retrieve
    supporting context (references, documents, prior results), then
    ``ground(input_value, results)`` to build the input an activity actually runs
    on - so the activity's prompt renders the retrieved context instead of relying
    on the model's own recall (an adopter's reference-mapping retrieval feeding the
    substantiation prompt). ``search`` may be sync or async; its results are
    materialized to a list before grounding.

    Keeping retrieval a separate, typed step (rather than hiding it inside a hook)
    makes the grounded input - and therefore exactly what the model saw - explicit
    and auditable in the activity manifest.
    """

    results = search(input_value)
    if inspect.isawaitable(results):
        results = await results
    return ground(input_value, list(results))


async def with_fallback(
    fn: Callable[[], Awaitable[_R]],
    fallback: Callable[[BaseException], Awaitable[_R]],
    *,
    exceptions: type[BaseException] | tuple[type[BaseException], ...] = Exception,
) -> _R:
    """Run ``fn()``; if it raises one of ``exceptions``, run ``fallback(exc)``.

    Degrades a failing step to a fallback value (an empty result, a prior result,
    …) instead of aborting the pipeline; an exception outside ``exceptions``
    re-raises unchanged.
    """

    try:
        return await fn()
    except exceptions as exc:
        return await fallback(exc)
