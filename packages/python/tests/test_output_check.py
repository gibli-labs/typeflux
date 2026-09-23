"""Input-aware, pre-acceptance output check joining the repair loop (#745).

Mirrors the TypeScript ``executeActivity outputCheck`` coverage: a cross-field
contract that needs the INPUT (schema-inexpressible in pydantic) gets the model's
``validation_retries`` chances to self-correct, and an output-check/hook-rejected
output is never written to the cross-run cache.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from typeflux.core import (
    AIActivity,
    CacheConfig,
    ChatMessage,
    OutputCheckViolation,
    PromptRef,
    ResolvedPrompt,
)
from typeflux.execution.cache import InMemoryCacheStore
from typeflux.execution.executor import (
    AIActivityOutputValidationError,
    execute_ai_activity,
    execute_ai_activity_async,
)
from typeflux.prompts import InlinePromptRegistry
from typeflux.testing import FakeProvider


class _In(BaseModel):
    text: str
    threshold: int = 5  # a default, to prove the check sees materialized input


class _Out(BaseModel):
    label: str
    score: int


def _registry() -> InlinePromptRegistry:
    return InlinePromptRegistry(
        {
            "p/classify": ResolvedPrompt(
                ref=PromptRef("p/classify"),
                messages=(ChatMessage(role="user", content="Classify {{ text }}"),),
                resolved_version="v1",
                model="fake-model",
            )
        }
    )


def _grounded_check(input_value: _In, output: _Out) -> list[OutputCheckViolation]:
    # The score must equal the input text's length — a contract that needs the INPUT.
    if output.score == len(input_value.text):
        return []
    return [OutputCheckViolation(message="score must equal input.text length", path=("score",))]


def _activity(
    *,
    output_check=None,
    hook=None,
    cache: CacheConfig | None = None,
    validation_retries: int = 1,
) -> AIActivity:
    return AIActivity(
        name="classify",
        input_type=_In,
        output_type=_Out,
        prompt_ref=PromptRef("p/classify"),
        validation_retries=validation_retries,
        output_check=output_check,
        hook=hook,
        cache=cache,
    )


class _AsyncFakeProvider:
    provider_name = "fake"
    default_model = "fake-model"

    def __init__(self, responses: list[BaseModel]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def async_structured_call(self, *, messages, output_schema, **kwargs):
        self.calls.append({"messages": list(messages)})
        if not self.responses:
            raise AssertionError("no async fake responses left")
        return self.responses.pop(0)


# --- repair-loop participation ---


def test_violation_feeds_repair_loop_and_model_self_corrects() -> None:
    provider = FakeProvider([_Out(label="support", score=99), _Out(label="support", score=2)])
    result = execute_ai_activity(
        activity=_activity(output_check=_grounded_check),
        input_value=_In(text="hi"),  # len == 2
        registry=_registry(),
        provider=provider,
    )
    assert result == _Out(label="support", score=2)
    assert len(provider.calls) == 2
    # The repair message presented the violation legibly to the model.
    repair = provider.calls[1]["messages"][-1].content
    assert "failed output validation" in repair
    assert "score must equal input.text length" in repair


def test_exhausted_retries_raise_terminal_naming_violations() -> None:
    provider = FakeProvider([_Out(label="support", score=99), _Out(label="support", score=98)])
    with pytest.raises(AIActivityOutputValidationError) as excinfo:
        execute_ai_activity(
            activity=_activity(output_check=_grounded_check),
            input_value=_In(text="hi"),
            registry=_registry(),
            provider=provider,
        )
    assert "score must equal input.text length" in str(excinfo.value)
    assert len(provider.calls) == 2  # validation_retries=1 → 2 attempts


def test_output_check_sees_input_defaults_materialized() -> None:
    seen: dict[str, int] = {}

    def capture(input_value: _In, output: _Out) -> list[OutputCheckViolation]:
        seen["threshold"] = input_value.threshold
        return []

    execute_ai_activity(
        activity=_activity(output_check=capture),
        input_value=_In(text="hi"),  # threshold omitted → default 5
        registry=_registry(),
        provider=FakeProvider([_Out(label="support", score=1)]),
    )
    assert seen["threshold"] == 5


# --- cache ordering (the #745 hazard) ---


def test_rejected_output_is_not_cached() -> None:
    store = InMemoryCacheStore()
    provider = FakeProvider([_Out(label="a", score=1), _Out(label="b", score=1)])
    activity = _activity(
        output_check=lambda _i, _o: [OutputCheckViolation(message="always rejected")],
        cache=CacheConfig(),
        validation_retries=0,  # reject terminally on the first attempt
    )
    with pytest.raises(AIActivityOutputValidationError):
        execute_ai_activity(
            activity=activity,
            input_value=_In(text="hi"),
            registry=_registry(),
            provider=provider,
            cache_store=store,
        )
    assert len(provider.calls) == 1
    # Nothing cached → the next execution must call the provider again.
    with pytest.raises(AIActivityOutputValidationError):
        execute_ai_activity(
            activity=activity,
            input_value=_In(text="hi"),
            registry=_registry(),
            provider=provider,
            cache_store=store,
        )
    assert len(provider.calls) == 2


def test_accepted_output_is_cached_once() -> None:
    store = InMemoryCacheStore()
    provider = FakeProvider([_Out(label="support", score=2)])  # one response only
    activity = _activity(output_check=_grounded_check, cache=CacheConfig())
    first = execute_ai_activity(
        activity=activity,
        input_value=_In(text="hi"),
        registry=_registry(),
        provider=provider,
        cache_store=store,
    )
    second = execute_ai_activity(
        activity=activity,
        input_value=_In(text="hi"),
        registry=_registry(),
        provider=provider,
        cache_store=store,
    )
    assert first == second
    assert len(provider.calls) == 1  # second served from cache


def test_output_check_runs_before_hook_and_hook_rejection_not_cached() -> None:
    store = InMemoryCacheStore()
    order: list[str] = []

    def check(_input: _In, _output: _Out) -> list[OutputCheckViolation]:
        order.append("check")
        return []

    def hook(_input: _In, output: _Out) -> _Out:
        order.append("hook")
        raise RuntimeError("hook rejects")

    provider = FakeProvider([_Out(label="s", score=1), _Out(label="s", score=1)])
    activity = _activity(output_check=check, hook=hook, cache=CacheConfig())
    with pytest.raises(RuntimeError, match="hook rejects"):
        execute_ai_activity(
            activity=activity,
            input_value=_In(text="hi"),
            registry=_registry(),
            provider=provider,
            cache_store=store,
        )
    # outputCheck (pre-acceptance) ran before the hook (post-acceptance).
    assert order == ["check", "hook"]
    # The hook rejected AFTER the old cache-write site — the fix withholds the write,
    # so the next execution calls the provider again.
    with pytest.raises(RuntimeError, match="hook rejects"):
        execute_ai_activity(
            activity=activity,
            input_value=_In(text="hi"),
            registry=_registry(),
            provider=provider,
            cache_store=store,
        )
    assert len(provider.calls) == 2


def test_tightened_output_check_invalidates_stale_hit() -> None:
    """#745 review: a hit is re-checked; a failing hit regenerates and re-caches."""
    store = InMemoryCacheStore()
    provider = FakeProvider([_Out(label="support", score=99), _Out(label="support", score=2)])
    # Cache under NO check: score=99 is stored.
    execute_ai_activity(
        activity=_activity(cache=CacheConfig()),
        input_value=_In(text="hi"),
        registry=_registry(),
        provider=provider,
        cache_store=store,
    )
    assert len(provider.calls) == 1
    # "Redeploy" with a rejecting check: the hit (score=99) now violates → treated as
    # a MISS → provider called again (full repair loop); the fresh output passes.
    checked = _activity(output_check=_grounded_check, cache=CacheConfig())
    regenerated = execute_ai_activity(
        activity=checked,
        input_value=_In(text="hi"),
        registry=_registry(),
        provider=provider,
        cache_store=store,
    )
    assert regenerated == _Out(label="support", score=2)
    assert len(provider.calls) == 2
    # The corrected output re-cached over the stale entry: a third run is a passing
    # HIT (the provider has no responses left and would fail loudly on a miss).
    third = execute_ai_activity(
        activity=checked,
        input_value=_In(text="hi"),
        registry=_registry(),
        provider=provider,
        cache_store=store,
    )
    assert third == _Out(label="support", score=2)
    assert len(provider.calls) == 2


def test_mutating_hook_does_not_contaminate_cache() -> None:
    """#745 review: the cache stores a DEEP COPY of the pre-hook output, so a hook
    that mutates its argument in place cannot leak post-hook state into the store
    (which would double-transform on every hit)."""
    store = InMemoryCacheStore()
    hook_input_scores: list[int] = []

    def hook(input_value: _In, output: _Out) -> _Out:
        hook_input_scores.append(output.score)
        output.score += 1  # mutate IN PLACE (the aliasing hazard), return the same object
        return output

    activity = _activity(hook=hook, cache=CacheConfig())
    provider = FakeProvider([_Out(label="support", score=1)])  # one response only
    first = execute_ai_activity(
        activity=activity,
        input_value=_In(text="hi"),
        registry=_registry(),
        provider=provider,
        cache_store=store,
    )
    assert first.score == 2  # hook transformed the returned value
    # The HIT must re-run the hook on the PRE-hook value (1), exactly once — a
    # contaminated cache would hand the hook 2 and yield 3.
    second = execute_ai_activity(
        activity=activity,
        input_value=_In(text="hi"),
        registry=_registry(),
        provider=provider,
        cache_store=store,
    )
    assert second.score == 2
    assert hook_input_scores == [1, 1]
    assert len(provider.calls) == 1


# --- return-shape normalization (#745 review) ---


def _run_with_check_result(result: object) -> _Out:
    return execute_ai_activity(
        activity=_activity(output_check=lambda _i, _o: result, validation_retries=0),  # type: ignore[arg-type,return-value]
        input_value=_In(text="hi"),
        registry=_registry(),
        provider=FakeProvider([_Out(label="support", score=1)]),
    )


def test_output_check_none_and_empty_list_pass() -> None:
    assert _run_with_check_result(None) == _Out(label="support", score=1)
    assert _run_with_check_result([]) == _Out(label="support", score=1)


def test_output_check_single_violation_object_is_coerced() -> None:
    # A bare OutputCheckViolation (a common slip for [violation]) rejects, not passes.
    with pytest.raises(AIActivityOutputValidationError, match="bare violation"):
        _run_with_check_result(OutputCheckViolation(message="bare violation"))


def test_output_check_junk_returns_are_pointed_type_errors() -> None:
    # Pre-fix, a string return exploded into per-character "violations" and then
    # crashed the repair handler with AttributeError; now every junk shape is a
    # pointed TypeError naming the contract.
    for junk in ("looks truthy", 1, {"message": "not a violation obj"}, [object()]):
        with pytest.raises(TypeError, match="output_check must return None or a list"):
            _run_with_check_result(junk)


# --- async path parity ---


@pytest.mark.asyncio
async def test_async_violation_feeds_repair_loop_and_self_corrects() -> None:
    provider = _AsyncFakeProvider([_Out(label="support", score=99), _Out(label="support", score=2)])
    result = await execute_ai_activity_async(
        activity=_activity(output_check=_grounded_check),
        input_value=_In(text="hi"),
        registry=_registry(),
        provider=provider,
    )
    assert result == _Out(label="support", score=2)
    assert len(provider.calls) == 2
    repair = provider.calls[1]["messages"][-1].content
    assert "score must equal input.text length" in repair


@pytest.mark.asyncio
async def test_async_tightened_output_check_invalidates_stale_hit() -> None:
    """#745 review: the async executor's hit path re-checks too."""
    store = InMemoryCacheStore()
    provider = _AsyncFakeProvider([_Out(label="support", score=99), _Out(label="support", score=2)])
    await execute_ai_activity_async(
        activity=_activity(cache=CacheConfig()),
        input_value=_In(text="hi"),
        registry=_registry(),
        provider=provider,
        cache_store=store,
    )
    assert len(provider.calls) == 1
    checked = _activity(output_check=_grounded_check, cache=CacheConfig())
    regenerated = await execute_ai_activity_async(
        activity=checked,
        input_value=_In(text="hi"),
        registry=_registry(),
        provider=provider,
        cache_store=store,
    )
    assert regenerated == _Out(label="support", score=2)
    assert len(provider.calls) == 2
    third = await execute_ai_activity_async(
        activity=checked,
        input_value=_In(text="hi"),
        registry=_registry(),
        provider=provider,
        cache_store=store,
    )
    assert third == _Out(label="support", score=2)
    assert len(provider.calls) == 2
