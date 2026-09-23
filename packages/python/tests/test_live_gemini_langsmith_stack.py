"""Combined live smoke for the adopter reference stack (#408).

One ``execute_ai_activity`` invocation exercising all three integrations
TOGETHER: the prompt is resolved from a real LangSmith prompt, the structured
call runs on Gemini, and the activity trace is written through the LangSmith
observability backend. Each piece is individually live-verified elsewhere
(``test_live_providers`` / ``test_live_observers`` /
``test_langsmith_prompt_registry``); the COMBINATION is what this smoke pins —
a registry-resolved prompt rendering into a Gemini request while its
observation flows to LangSmith in the same execution.

Opt-in exactly like the sibling live suites: run with ``-m live`` and
``TYPEFLUX_RUN_LIVE=1``, plus ``GEMINI_API_KEY``, ``LANGSMITH_API_KEY``, and
``TYPEFLUX_LANGSMITH_TEST_PROMPT`` (a prompt name, or ``name:tag``). The Vertex
transport variant is deliberately absent — no credentials are provisioned (see
the issue's scope comment).
"""

from __future__ import annotations

import os
import re
from uuid import uuid4

import pytest
from pydantic import BaseModel, create_model

from typeflux.core.contracts import AIActivity, PromptRef, ProviderParams
from typeflux.env import load_env
from typeflux.execution.controls import ProviderRetryPolicy
from typeflux.execution.executor import execute_ai_activity
from typeflux.manifests import AIInvocationContext


class StackAnswer(BaseModel):
    text: str


def _missing_live_env() -> list[str]:
    required = ["GEMINI_API_KEY", "LANGSMITH_API_KEY", "TYPEFLUX_LANGSMITH_TEST_PROMPT"]
    return [name for name in required if not os.getenv(name)]


@pytest.mark.live
def test_live_gemini_langsmith_stack(request: pytest.FixtureRequest) -> None:
    load_env()
    if request.config.option.markexpr != "live":
        pytest.skip("live tests run only when explicitly selected with -m live")
    missing = _missing_live_env()
    if os.getenv("TYPEFLUX_RUN_LIVE") != "1" or missing:
        pytest.skip(
            "set TYPEFLUX_RUN_LIVE=1 plus GEMINI_API_KEY, LANGSMITH_API_KEY, "
            "and TYPEFLUX_LANGSMITH_TEST_PROMPT to run"
        )
    from typeflux.observability.langsmith import LangSmithObservabilityBackend
    from typeflux.prompts import LangSmithPromptRegistry
    from typeflux.providers import GeminiProvider

    # Pin a dedicated project so the trace is findable and isolated.
    os.environ.setdefault("LANGSMITH_PROJECT", "typeflux-live-stack-smoke")

    # Leg 1 — the REAL LangSmith prompt (the registry also enforces the stack's
    # provider-ownership invariant: a model-bound prompt is rejected loudly, so
    # nothing here can shadow the Gemini model selection).
    name, _, tag = os.environ["TYPEFLUX_LANGSMITH_TEST_PROMPT"].partition(":")
    prompt_ref = PromptRef(name=name, label=tag or None)
    registry = LangSmithPromptRegistry()
    resolved = registry.resolve(prompt_ref)
    assert resolved.messages, "LangSmith prompt resolved to no messages"

    # The real prompt's variables are whatever it declares — synthesize a
    # matching input model so the render engine sees every field. The renderer's
    # placeholder grammar includes dotted paths ({{customer.name}}), which
    # resolve through dict fields — build the nested value for each root (codex).
    placeholders = {
        var
        for message in resolved.messages
        for var in re.findall(
            r"{{\s*([\w.]+)\s*}}",
            message.content if isinstance(message.content, str) else "",
        )
    }
    roots: dict[str, set[str]] = {}
    for var in placeholders:
        root, _, rest = var.partition(".")
        roots.setdefault(root, set())
        if rest:
            roots[root].add(rest)
    field_defs: dict[str, tuple[type, object]] = {}
    for root, children in sorted(roots.items()):
        if children:
            nested: dict[str, object] = {}
            for child in sorted(children):
                node = nested
                *parents, leaf = child.split(".")
                for part in parents:
                    # Overlapping placeholders ({{r.a}} + {{r.a.b}}): the dict
                    # wins over a shorter path's string — the shorter placeholder
                    # then renders the dict's repr, fine for a smoke value (Bugbot).
                    part_node = node.get(part)
                    if not isinstance(part_node, dict):
                        part_node = {}
                        node[part] = part_node
                    node = part_node
                if not isinstance(node.get(leaf), dict):
                    node[leaf] = f"live stack smoke value for {root}.{child}"
            field_defs[root] = (dict, nested)
        else:
            field_defs[root] = (str, f"live stack smoke value for {root}")
    if not field_defs:
        field_defs = {"probe": (str, "live stack smoke")}
    smoke_input_model = create_model("StackSmokeInput", **field_defs)  # type: ignore[call-overload]

    activity = AIActivity(
        name="live_stack_smoke",
        input_type=smoke_input_model,
        output_type=StackAnswer,
        prompt_ref=prompt_ref,
        validation_retries=1,
    )

    # Leg 3 — the activity observation flows to LangSmith in the SAME execution.
    backend = LangSmithObservabilityBackend.from_env()
    observer = backend.writer.create_activity_observer()

    model = os.getenv("TYPEFLUX_GEMINI_MODEL", "gemini-2.5-flash")
    workflow_id = f"live-stack-smoke-{uuid4().hex[:12]}"
    try:
        result = execute_ai_activity(
            activity=activity,
            input_value=smoke_input_model(),
            registry=registry,
            # Leg 2 — Gemini runs the registry-resolved prompt (thinking disabled
            # like the provider live suite: deterministic, small output).
            provider=GeminiProvider(
                default_model=model,
                default_provider_params=ProviderParams(thinking_budget=0),
            ),
            observer=observer,
            # Absorb transient Gemini blips (5xx/rate limits) like the sibling
            # provider live suite does, so flakiness can't mask the integration
            # under test (codex).
            provider_retry_policy=ProviderRetryPolicy(max_attempts=3, initial_backoff_seconds=2),
            invocation_context=AIInvocationContext(
                temporal_namespace="live-test",
                temporal_workflow_type="LiveStackSmokeWorkflow",
                temporal_workflow_id=workflow_id,
                temporal_run_id="manual-run",
                temporal_activity_type="live_stack_smoke",
                temporal_activity_id="activity-live-stack-smoke",
                temporal_activity_attempt=1,
                typeflux_activity_name="live_stack_smoke",
                typeflux_manifest_hash="filled-by-executor-metadata",
            ),
        )
    finally:
        backend.writer.flush()
        backend.writer.shutdown()

    assert isinstance(result, StackAnswer)
    assert result.text.strip(), "Gemini returned an empty structured answer"
    print(f"\n[stack-smoke] prompt={name!r} version={resolved.resolved_version!r} model={model}")
    print(f"[stack-smoke] workflow_id={workflow_id} answer={result.text[:120]!r}")
