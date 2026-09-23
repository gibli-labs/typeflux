"""Tests for ActivityContext injection into hooks (#397)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from typeflux.core import (
    ActivityContext,
    AIActivity,
    ChatMessage,
    PromptRef,
    ResolvedPrompt,
    ai_activity,
)
from typeflux.core.contracts import _validate_hook_signature
from typeflux.execution.executor import execute_ai_activity
from typeflux.prompts import InlinePromptRegistry
from typeflux.testing import FakeProvider


class _In(BaseModel):
    company_id: str
    subject: str


class _Out(BaseModel):
    label: str


def _registry() -> InlinePromptRegistry:
    return InlinePromptRegistry(
        {
            "p/classify": ResolvedPrompt(
                ref=PromptRef("p/classify"),
                messages=(ChatMessage(role="user", content="Classify {{ subject }}"),),
                resolved_version="v1",
                model="fake-model",
            )
        }
    )


def _activity(hook: object) -> AIActivity:
    return AIActivity(
        name="classify",
        input_type=_In,
        output_type=_Out,
        prompt_ref=PromptRef("p/classify"),
        hook=hook,  # type: ignore[arg-type]
    )


# --- arity validation (unit) ---


def test_two_arg_hook_does_not_want_context() -> None:
    def hook(i: _In, o: _Out) -> _Out:
        return o

    assert _validate_hook_signature(hook, input_type=_In, output_type=_Out) is False
    assert _activity(hook).hook_wants_context is False


def test_three_arg_hook_wants_context() -> None:
    def hook(i: _In, o: _Out, ctx: ActivityContext) -> _Out:
        return o

    assert _validate_hook_signature(hook, input_type=_In, output_type=_Out) is True
    assert _activity(hook).hook_wants_context is True


def test_one_or_four_arg_hook_rejected() -> None:
    def one_param(i: _In) -> _Out:
        return _Out(label="x")

    def four_params(a: _In, b: _Out, c: object, d: object) -> _Out:
        return b

    with pytest.raises(TypeError):
        _validate_hook_signature(one_param, input_type=_In, output_type=_Out)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _validate_hook_signature(four_params, input_type=_In, output_type=_Out)


# --- end-to-end: a context-aware hook receives a populated context + deps ---


def test_context_hook_receives_deps_and_tenant() -> None:
    captured: list[ActivityContext] = []

    class _Deps:
        def __init__(self) -> None:
            self.writes: list[str] = []

    deps = _Deps()

    def hook(i: _In, o: _Out, ctx: ActivityContext) -> _Out:
        captured.append(ctx)
        ctx.deps.writes.append(f"{ctx.tenant.get('company_id')}:{o.label}")
        return o

    result = execute_ai_activity(
        activity=_activity(hook),
        input_value=_In(company_id="co-9", subject="Invoice"),
        registry=_registry(),
        provider=FakeProvider([_Out(label="billing")]),
        deps=deps,
        tenant_resolver=lambda inp: {"company_id": inp.company_id},
    )

    assert result.label == "billing"
    assert len(captured) == 1
    ctx = captured[0]
    assert ctx.activity_name == "classify"
    assert ctx.tenant == {"company_id": "co-9"}
    assert ctx.deps is deps
    assert deps.writes == ["co-9:billing"]


def test_tenant_resolver_none_yields_empty_tenant() -> None:
    captured: list[ActivityContext] = []

    def hook(i: _In, o: _Out, ctx: ActivityContext) -> _Out:
        captured.append(ctx)
        return o

    execute_ai_activity(
        activity=_activity(hook),
        input_value=_In(company_id="c", subject="x"),
        registry=_registry(),
        provider=FakeProvider([_Out(label="ok")]),
        tenant_resolver=lambda inp: None,  # type: ignore[arg-type,return-value]
    )
    assert captured[0].tenant == {}


def test_tenant_resolver_non_mapping_raises_at_boundary() -> None:
    def hook(i: _In, o: _Out, ctx: ActivityContext) -> _Out:
        return o

    with pytest.raises(TypeError, match="tenant_resolver must return"):
        execute_ai_activity(
            activity=_activity(hook),
            input_value=_In(company_id="c", subject="x"),
            registry=_registry(),
            provider=FakeProvider([_Out(label="ok")]),
            tenant_resolver=lambda inp: "nope",  # type: ignore[arg-type,return-value]
        )


def test_decorator_accepts_context_aware_hook() -> None:
    # The primary @ai_activity.defn path must accept a 3-arg hook (#397 codex).
    @ai_activity.defn(name="classify_ctx", prompt=PromptRef("p/classify"), output=_Out)
    def classify(i: _In, o: _Out, ctx: ActivityContext) -> _Out:
        return o

    assert classify.input_type is _In
    assert classify.hook_wants_context is True


def test_decorator_still_accepts_two_arg_hook() -> None:
    @ai_activity.defn(name="classify_plain", prompt=PromptRef("p/classify"), output=_Out)
    def classify(i: _In, o: _Out) -> _Out:
        return o

    assert classify.hook_wants_context is False


def test_two_arg_hook_runs_without_context() -> None:
    def hook(i: _In, o: _Out) -> _Out:
        return _Out(label=o.label + "!")

    result = execute_ai_activity(
        activity=_activity(hook),
        input_value=_In(company_id="co-1", subject="x"),
        registry=_registry(),
        provider=FakeProvider([_Out(label="ok")]),
        deps=object(),
        tenant_resolver=lambda inp: {"company_id": "ignored"},
    )

    assert result.label == "ok!"
