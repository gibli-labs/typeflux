from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from typeflux.core.activities import ai_activity
from typeflux.core.artifacts import (
    ArtifactAttachment,
    ArtifactGroupPart,
    ArtifactInput,
    ArtifactPolicy,
    TextPart,
)
from typeflux.core.contracts import (
    ActivityContext,
    AIActivity,
    CacheConfig,
    ChatMessage,
    ModerationConfig,
    ModerationResult,
    PromptRef,
    ProviderParams,
    ResolvedPrompt,
    SessionCacheConfig,
)
from typeflux.core.render import render_template
from typeflux.execution.controls import ProviderRetryPolicy
from typeflux.execution.executor import (
    AIActivityOutputValidationError,
    ModerationBlockedError,
    execute_ai_activity,
    execute_ai_activity_async,
)
from typeflux.execution.lifecycle import ActivityCancelled
from typeflux.manifests import (
    build_activity_execution_manifest,
    build_activity_manifest,
    schema_hash,
)
from typeflux.observability.redaction import RegexPIIRedactor
from typeflux.prompts import InlinePromptRegistry
from typeflux.prompts.context import (
    LANGFUSE_PROMPT_CONTEXT_KEY,
    langfuse_prompt_context,
)
from typeflux.providers import OpenAIProvider, ProviderUsage
from typeflux.providers.errors import ProviderRateLimitError, ProviderTransientError
from typeflux.testing import FakeProvider


def test_contract_models_are_fully_defined_at_import() -> None:
    # The Temporal workflow sandbox cannot lazily rebuild a pydantic model, so a
    # forward reference to a later-defined class fails every workflow task that
    # first validates it there (#371: MapActivityContext.cached_session referenced
    # CachedSessionHandle before it existed). Every contract model must therefore
    # be fully defined the moment the module finishes importing. Probed in a
    # fresh interpreter: in-process, any earlier successful validation of the
    # model would lazily rebuild it and mask a regression.
    probe = textwrap.dedent(
        """
        from pydantic import BaseModel

        import typeflux.core.contracts as contracts

        incomplete = [
            name
            for name, obj in vars(contracts).items()
            if isinstance(obj, type)
            and issubclass(obj, BaseModel)
            and obj is not BaseModel
            and not obj.__pydantic_complete__
        ]
        assert incomplete == [], incomplete
        """
    )
    subprocess.run([sys.executable, "-c", probe], check=True)


class Ticket(BaseModel):
    subject: str
    body: str
    nested: dict[str, str] = {}


class Classification(BaseModel):
    category: str
    urgency: str


class Score(BaseModel):
    score: int


class ClaimReviewInput(BaseModel):
    claim_id: str
    documents: list[str]


class ClaimReviewOutput(BaseModel):
    summary: str


def registry_for(
    template: str = "Classify {{ subject }} / {{ nested.value }}",
) -> InlinePromptRegistry:
    return InlinePromptRegistry(
        {
            "support/classify": ResolvedPrompt(
                ref=PromptRef("support/classify"),
                messages=(ChatMessage(role="user", content=template),),
                resolved_version="prompt-v1",
                model="fake-model",
                temperature=0.1,
                metadata={"demo": True},
            )
        }
    )


def test_ai_activity_validates_input_and_output_types() -> None:
    with pytest.raises(TypeError, match="input_type"):
        AIActivity(
            name="bad",
            input_type=dict,  # type: ignore[arg-type]
            output_type=Classification,
            prompt_ref=PromptRef("support/classify"),
        )

    with pytest.raises(TypeError, match="output_type"):
        AIActivity(
            name="bad",
            input_type=Ticket,
            output_type=dict,  # type: ignore[arg-type]
            prompt_ref=PromptRef("support/classify"),
        )

    with pytest.raises(ValueError, match="validation_retries"):
        AIActivity(
            name="bad",
            input_type=Ticket,
            output_type=Classification,
            prompt_ref=PromptRef("support/classify"),
            validation_retries=-1,
        )


def test_decorator_validates_hook_signatures() -> None:
    with pytest.raises(TypeError, match="input, output"):

        @ai_activity.defn(prompt=PromptRef("support/classify"), output=Classification)
        def missing_output(ticket: Ticket) -> Classification:
            return Classification(category="x", urgency="low")

    with pytest.raises(TypeError, match="second parameter"):

        @ai_activity.defn(prompt=PromptRef("support/classify"), output=Classification)
        def wrong_output(ticket: Ticket, output: Ticket) -> Classification:
            return Classification(category="x", urgency="low")


def test_decorator_threads_policies() -> None:
    def moderator(output: Classification) -> ModerationResult:
        return ModerationResult(flagged=False)

    @ai_activity.defn(
        prompt=PromptRef("support/classify"),
        output=Classification,
        cache=CacheConfig(bypass_reads_env="NO_CACHE"),
        moderation=ModerationConfig(moderator=moderator),
        session_cache=SessionCacheConfig(ttl_seconds=60),
    )
    def classify(ticket: Ticket, output: Classification) -> Classification:
        return output

    assert classify.cache == CacheConfig(bypass_reads_env="NO_CACHE")
    assert classify.moderation is not None and classify.moderation.moderator is moderator
    assert classify.session_cache == SessionCacheConfig(ttl_seconds=60)


def test_decorator_defaults_policies_to_none() -> None:
    @ai_activity.defn(prompt=PromptRef("support/classify"), output=Classification)
    def classify(ticket: Ticket, output: Classification) -> Classification:
        return output

    assert classify.cache is None
    assert classify.moderation is None
    assert classify.session_cache is None


def test_decorator_context_hook_with_cache() -> None:
    @ai_activity.defn(
        prompt=PromptRef("support/classify"),
        output=Classification,
        cache=CacheConfig(),
    )
    def classify(ticket: Ticket, output: Classification, ctx: ActivityContext) -> Classification:
        return output

    assert classify.hook_wants_context is True
    assert classify.cache == CacheConfig()


def _moderated_activity(moderator, *, on_violation="block") -> AIActivity:
    return AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
        moderation=ModerationConfig(moderator=moderator, on_violation=on_violation),
    )


def test_ai_activity_validates_moderator_signature() -> None:
    # #158: a moderator is (output) -> ModerationResult; bad shapes fail loud.
    def two_params(output: Classification, extra: Ticket) -> ModerationResult:
        return ModerationResult(flagged=False)

    with pytest.raises(TypeError, match="exactly one positional"):
        _moderated_activity(two_params)

    def keyword_extra(output: Classification, **kw: Any) -> ModerationResult:
        return ModerationResult(flagged=False)

    with pytest.raises(TypeError, match="exactly one positional"):
        _moderated_activity(keyword_extra)

    def star_args(*args: Any) -> ModerationResult:
        return ModerationResult(flagged=False)

    with pytest.raises(TypeError, match="must be positional"):
        _moderated_activity(star_args)

    def defaulted(output: Classification = None) -> ModerationResult:  # type: ignore[assignment]
        return ModerationResult(flagged=False)

    with pytest.raises(TypeError, match="must not define a default"):
        _moderated_activity(defaulted)

    def wrong_param(output: Ticket) -> ModerationResult:
        return ModerationResult(flagged=False)

    with pytest.raises(TypeError, match="output_type"):
        _moderated_activity(wrong_param)

    def wrong_return(output: Classification) -> Classification:  # type: ignore[misc]
        return output

    with pytest.raises(TypeError, match="ModerationResult"):
        _moderated_activity(wrong_return)

    def good(output: Classification) -> ModerationResult:
        return ModerationResult(flagged=False)

    # A well-formed moderator constructs without error.
    _moderated_activity(good)


def test_executor_moderation_passes_clean_output() -> None:
    # #158: a non-flagged verdict lets the validated output through unchanged.
    seen: list[Classification] = []

    def moderator(output: Classification) -> ModerationResult:
        seen.append(output)
        return ModerationResult(flagged=False)

    output = Classification(category="billing", urgency="medium")
    result = execute_ai_activity(
        activity=_moderated_activity(moderator),
        input_value=Ticket(subject="Invoice", body="Q", nested={"value": "g"}),
        registry=registry_for(),
        provider=FakeProvider([output]),
    )
    assert result == output
    assert seen == [output]  # the moderator inspected the validated output


def test_executor_moderation_block_raises_without_raw_output() -> None:
    # #158: flagged + block → terminal ModerationBlockedError carrying only the
    # categories, never the raw model output.
    def moderator(output: Classification) -> ModerationResult:
        return ModerationResult(flagged=True, categories=("hate", "violence"), max_score=0.97)

    output = Classification(category="SECRET-PAYLOAD", urgency="HUSH")
    with pytest.raises(ModerationBlockedError) as excinfo:
        execute_ai_activity(
            activity=_moderated_activity(moderator, on_violation="block"),
            input_value=Ticket(subject="Invoice", body="Q", nested={"value": "g"}),
            registry=registry_for(),
            provider=FakeProvider([output]),
        )
    message = str(excinfo.value)
    assert "hate" in message and "violence" in message
    assert "SECRET-PAYLOAD" not in message and "HUSH" not in message


def test_executor_moderation_flag_continues() -> None:
    # #158: flagged + flag → records the verdict but lets the output through.
    def moderator(output: Classification) -> ModerationResult:
        return ModerationResult(flagged=True, categories=("borderline",))

    output = Classification(category="billing", urgency="low")
    result = execute_ai_activity(
        activity=_moderated_activity(moderator, on_violation="flag"),
        input_value=Ticket(subject="Invoice", body="Q", nested={"value": "g"}),
        registry=registry_for(),
        provider=FakeProvider([output]),
    )
    assert result == output


@pytest.mark.asyncio
async def test_executor_async_moderation_block_and_flag() -> None:
    # #158: the async executor runs the checkpoint too (offloaded to a thread).
    def block(output: Classification) -> ModerationResult:
        return ModerationResult(flagged=True, categories=("unsafe",))

    with pytest.raises(ModerationBlockedError, match="unsafe"):
        await execute_ai_activity_async(
            activity=_moderated_activity(block, on_violation="block"),
            input_value=Ticket(subject="Invoice", body="Q", nested={"value": "g"}),
            registry=registry_for(),
            provider=_AsyncFakeProvider([Classification(category="billing", urgency="low")]),
        )

    def flag(output: Classification) -> ModerationResult:
        return ModerationResult(flagged=True, categories=("borderline",))

    output = Classification(category="billing", urgency="low")
    result = await execute_ai_activity_async(
        activity=_moderated_activity(flag, on_violation="flag"),
        input_value=Ticket(subject="Invoice", body="Q", nested={"value": "g"}),
        registry=registry_for(),
        provider=_AsyncFakeProvider([output]),
    )
    assert result == output


def test_executor_moderator_must_return_moderation_result() -> None:
    # #158: a moderator returning the wrong type fails loud (not silently ignored).
    def moderator(output: Classification) -> ModerationResult:
        return "blocked"  # type: ignore[return-value]

    with pytest.raises(TypeError, match="ModerationResult"):
        execute_ai_activity(
            activity=_moderated_activity(moderator),
            input_value=Ticket(subject="Invoice", body="Q", nested={"value": "g"}),
            registry=registry_for(),
            provider=FakeProvider([Classification(category="billing", urgency="low")]),
        )


def _semantics_guard(**semantics):
    from typeflux.project.policy import ComposedProjectPolicy
    from typeflux.project.policy_enforcement import RuntimePolicyGuard

    return RuntimePolicyGuard(
        policy=ComposedProjectPolicy(
            selected_policy_ids=("p",),
            applied_policy_ids=("p",),
            policy_names=("p",),
            policy_hash="h",
            payload={"semantics": semantics},
        )
    )


def test_executor_moderation_policy_required_blocks_unmoderated_activity() -> None:
    # #158 PR2: a policy with semantics.required rejects an activity that declares
    # no moderator — at the runtime checkpoint, as a non-retryable policy error.
    from typeflux.providers.errors import ProviderPolicyError

    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )  # no moderation configured
    with pytest.raises(ProviderPolicyError, match="must declare moderation"):
        execute_ai_activity(
            activity=activity,
            input_value=Ticket(subject="I", body="Q", nested={"value": "g"}),
            registry=registry_for(),
            provider=FakeProvider([Classification(category="billing", urgency="low")]),
            moderation_policy_guard=_semantics_guard(required=True),
        )


def test_executor_moderation_policy_escalates_flag_to_block() -> None:
    # #158 PR2: a disallowed category forces a block even though the activity only
    # flags — policy tightens, never loosens, the activity's on_violation.
    def flag(output: Classification) -> ModerationResult:
        return ModerationResult(flagged=True, categories=("violence",))

    with pytest.raises(ModerationBlockedError, match="disallowed category violence"):
        execute_ai_activity(
            activity=_moderated_activity(flag, on_violation="flag"),
            input_value=Ticket(subject="I", body="Q", nested={"value": "g"}),
            registry=registry_for(),
            provider=FakeProvider([Classification(category="billing", urgency="low")]),
            moderation_policy_guard=_semantics_guard(categories=["violence"]),
        )


def test_executor_moderation_policy_score_blocks_even_when_moderator_clears() -> None:
    # #158 PR2: the policy applies its threshold to the moderator's reported score
    # independent of the moderator's own flagged decision — a lenient moderator
    # (flagged=False) that still reports a high score is blocked by a stricter policy.
    def lenient(output: Classification) -> ModerationResult:
        return ModerationResult(flagged=False, max_score=0.95)

    with pytest.raises(ModerationBlockedError, match="score 0.95 >= threshold 0.8"):
        execute_ai_activity(
            activity=_moderated_activity(lenient, on_violation="flag"),
            input_value=Ticket(subject="I", body="Q", nested={"value": "g"}),
            registry=registry_for(),
            provider=FakeProvider([Classification(category="billing", urgency="low")]),
            moderation_policy_guard=_semantics_guard(score_threshold=0.8),
        )


@pytest.mark.asyncio
async def test_executor_async_moderation_policy_escalation_and_require_block() -> None:
    # #158 PR2: the async checkpoint enforces policy too — score escalation and
    # require_block (config gate) both fire through execute_ai_activity_async.
    from typeflux.providers.errors import ProviderPolicyError

    def lenient(output: Classification) -> ModerationResult:
        return ModerationResult(flagged=False, max_score=0.95)

    with pytest.raises(ModerationBlockedError, match="threshold 0.8"):
        await execute_ai_activity_async(
            activity=_moderated_activity(lenient, on_violation="flag"),
            input_value=Ticket(subject="I", body="Q", nested={"value": "g"}),
            registry=registry_for(),
            provider=_AsyncFakeProvider([Classification(category="billing", urgency="low")]),
            moderation_policy_guard=_semantics_guard(score_threshold=0.8),
        )

    # require_block: a flag-only moderated activity is rejected by the config gate.
    def flag(output: Classification) -> ModerationResult:
        return ModerationResult(flagged=False)

    with pytest.raises(ProviderPolicyError, match="on_violation='block'"):
        await execute_ai_activity_async(
            activity=_moderated_activity(flag, on_violation="flag"),
            input_value=Ticket(subject="I", body="Q", nested={"value": "g"}),
            registry=registry_for(),
            provider=_AsyncFakeProvider([Classification(category="billing", urgency="low")]),
            moderation_policy_guard=_semantics_guard(require_block=True),
        )


class _ModerationMetadataObservation:
    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store

    def update_output(self, output_value: BaseModel) -> None:
        return None

    def update_error(self, error: BaseException) -> None:
        return None

    def update_metadata(self, metadata: dict[str, Any]) -> None:
        # Model the backend: update_metadata REPLACES per top-level key (it does
        # not deep-merge), so this catches a regression that nests the verdict
        # under `typeflux` and clobbers the activity's base metadata.
        self._store.update(metadata)

    @contextmanager
    def observe_generation(self, **kwargs: Any):
        yield _RecordingHandle([], "generation")

    @contextmanager
    def observe_hook(self, **kwargs: Any):
        yield _RecordingHandle([], "hook")


class _ModerationMetadataObserver:
    def __init__(self) -> None:
        # Seed a base `typeflux` to stand in for the activity span's creation
        # metadata; recording the verdict must not wipe it.
        self.metadata: dict[str, Any] = {"typeflux": {"activity_name": "classify_ticket"}}

    @contextmanager
    def observe_activity(self, **kwargs: Any):
        yield _ModerationMetadataObservation(self.metadata)

    def flush(self) -> None:
        return None

    def redact_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        return metadata


def test_executor_records_moderation_verdict_on_activity_trace() -> None:
    # #158 follow-up: the verdict (decision/categories/score/moderator) is recorded
    # as activity-trace metadata — audit evidence, never the raw output — under its
    # own top-level key so it doesn't clobber the activity's base typeflux metadata.
    def flag(output: Classification) -> ModerationResult:
        return ModerationResult(flagged=True, categories=("borderline",), max_score=0.4)

    observer = _ModerationMetadataObserver()
    execute_ai_activity(
        activity=_moderated_activity(flag, on_violation="flag"),
        input_value=Ticket(subject="I", body="Q", nested={"value": "g"}),
        registry=registry_for(),
        provider=FakeProvider([Classification(category="billing", urgency="low")]),
        observer=observer,
    )
    assert observer.metadata["typeflux_moderation"] == {
        "decision": "flag",
        "categories": ["borderline"],
        "max_score": 0.4,
        "moderator": "flag",
    }
    # The base typeflux metadata survived (verdict recorded under a separate key).
    assert observer.metadata["typeflux"] == {"activity_name": "classify_ticket"}


def test_moderation_verdict_is_redaction_exempt() -> None:
    # #158 follow-up: typeflux.moderation.* survives the default PII redactor.
    redactor = RegexPIIRedactor.default()
    payload = {
        "typeflux_moderation": {
            "decision": "block",
            "categories": ["violence"],
            "max_score": 0.97,
            "moderator": "openai_moderator",
        }
    }
    assert redactor.redact(payload) == payload


def test_manifest_hash_is_stable() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    resolved = registry_for().resolve(PromptRef("support/classify"))

    first = build_activity_manifest(activity, resolved)
    second = build_activity_manifest(activity, resolved)

    assert schema_hash(Ticket) == schema_hash(Ticket)
    assert first.manifest_hash == second.manifest_hash


def test_observation_context_does_not_change_manifest_hashes() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    base_resolved = ResolvedPrompt(
        ref=PromptRef("support/classify"),
        messages=(ChatMessage(role="user", content="Classify {{ subject }}"),),
        resolved_version="prompt-v1",
        model="fake-model",
    )
    contextual_resolved = ResolvedPrompt(
        ref=base_resolved.ref,
        messages=base_resolved.messages,
        resolved_version=base_resolved.resolved_version,
        model=base_resolved.model,
        observation_context=langfuse_prompt_context(object()),
    )
    rendered_messages = (ChatMessage(role="user", content="Classify Invoice"),)

    base_manifest = build_activity_manifest(activity, base_resolved)
    contextual_manifest = build_activity_manifest(activity, contextual_resolved)
    assert contextual_manifest.manifest_hash == base_manifest.manifest_hash

    base_execution = build_activity_execution_manifest(
        activity=activity,
        activity_manifest=base_manifest,
        resolved_prompt=base_resolved,
        rendered_messages=rendered_messages,
        validation_attempt=0,
    )
    contextual_execution = build_activity_execution_manifest(
        activity=activity,
        activity_manifest=contextual_manifest,
        resolved_prompt=contextual_resolved,
        rendered_messages=rendered_messages,
        validation_attempt=0,
    )
    assert contextual_execution.manifest_hash == base_execution.manifest_hash
    assert LANGFUSE_PROMPT_CONTEXT_KEY not in json.dumps(contextual_execution.to_dict())


def test_prompt_rendering_fails_on_missing_fields() -> None:
    ticket = Ticket(subject="Hello", body="World")

    with pytest.raises(KeyError, match="missing prompt field"):
        render_template("{{ missing.field }}", ticket)


def test_prompt_rendering_uses_mustache_nested_fields() -> None:
    ticket = Ticket(subject="Hello", body="World", nested={"value": "gold"})

    assert render_template("{{subject}} / {{ nested.value }}", ticket) == "Hello / gold"


def test_prompt_rendering_preserves_special_characters_literally() -> None:
    # Contract/claim text must reach the model byte-for-byte; no HTML escaping.
    ticket = Ticket(subject="Smith & Jones <LLC>", body='say "hi" & don\'t <stop>')

    rendered = render_template("S: {{subject}} B: {{body}}", ticket)

    assert rendered == 'S: Smith & Jones <LLC> B: say "hi" & don\'t <stop>'
    assert "&amp;" not in rendered
    assert "&lt;" not in rendered
    assert "&quot;" not in rendered


def test_prompt_rendering_formats_non_string_values() -> None:
    class Payload(BaseModel):
        count: int
        ratio: float
        flag: bool
        note: str | None

    payload = Payload(count=5, ratio=1.5, flag=True, note=None)

    rendered = render_template("{{count}}|{{ratio}}|{{flag}}|{{note}}", payload)

    assert rendered == "5|1.5|True|"


def test_prompt_rendering_leaves_template_text_untouched() -> None:
    ticket = Ticket(subject="Hello", body="World")

    rendered = render_template("literal ${ENV_VAR} and {{subject}} and {not_a_var}", ticket)

    assert rendered == "literal ${ENV_VAR} and Hello and {not_a_var}"


def test_executor_calls_provider_with_rendered_messages() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    output = Classification(category="billing", urgency="medium")
    provider = FakeProvider([output])

    result = execute_ai_activity(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
        registry=registry_for(),
        provider=provider,
    )

    assert result == output
    assert provider.calls[0]["messages"][0].content == "Classify Invoice / gold"
    assert provider.calls[0]["output_schema"] is Classification
    assert provider.calls[0]["metadata"]["typeflux"]["activity_name"] == "classify_ticket"
    assert (
        provider.calls[0]["metadata"]["typeflux"]["provider"]["resolved_prompt_version"]
        == "prompt-v1"
    )
    assert provider.calls[0]["metadata"]["typeflux"]["provider"]["validation_attempt"] == 0
    assert provider.calls[0]["metadata"]["typeflux"]["provider_controls"]["execution_mode"] == (
        "sync"
    )


class _CachingFakeProvider:
    """A provider whose ``structured_call`` opts into the session-cache param,
    so the executor's capability probe threads the handle through to it."""

    provider_name = "caching-fake"
    supported_provider_params = FakeProvider.supported_provider_params

    def __init__(self, output: BaseModel) -> None:
        self._output = output
        self.default_provider_params = ProviderParams()
        self.seen_cached_session: Any = "unset"

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        cached_session: Any = None,
        **kwargs: Any,
    ) -> BaseModel:
        self.seen_cached_session = cached_session
        return self._output


def test_executor_threads_cached_session_to_capable_provider() -> None:
    # #60 (sync path): execute_ai_activity forwards the handle to a provider that
    # accepts ``cached_session``; the fail-soft probe means a non-caching provider
    # (plain FakeProvider) simply never sees it.
    from typeflux.core.contracts import CachedSessionHandle

    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    output = Classification(category="billing", urgency="medium")
    provider = _CachingFakeProvider(output)
    handle = CachedSessionHandle(
        provider="caching-fake", identity_hash="h", supported=True, style="prefix"
    )

    result = execute_ai_activity(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Q", nested={"value": "gold"}),
        registry=registry_for(),
        provider=provider,
        cached_session=handle,
    )

    assert result == output
    assert provider.seen_cached_session == handle


class _CancelImmediatelyLifecycle:
    heartbeat_interval_seconds = None

    def __init__(self) -> None:
        self.cancel_checks = 0

    def heartbeat(self) -> None:
        return None

    def raise_if_cancelled(self) -> None:
        self.cancel_checks += 1
        raise ActivityCancelled("cancelled")


def test_executor_aborts_on_cancellation_before_calling_provider() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    provider = FakeProvider([Classification(category="billing", urgency="medium")])
    lifecycle = _CancelImmediatelyLifecycle()

    with pytest.raises(ActivityCancelled):
        execute_ai_activity(
            activity=activity,
            input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
            registry=registry_for(),
            provider=provider,
            lifecycle=lifecycle,
        )

    # The cancellation checkpoint fires before any provider spend.
    assert provider.calls == []
    assert lifecycle.cancel_checks >= 1


class _SlowProvider:
    provider_name = "slow"

    def __init__(self, output: BaseModel, *, delay: float) -> None:
        self._output = output
        self._delay = delay

    def structured_call(self, **kwargs: Any) -> BaseModel:
        time.sleep(self._delay)
        return self._output


class _HeartbeatCountingLifecycle:
    def __init__(self, interval: float | None) -> None:
        self.heartbeat_interval_seconds = interval
        self.beats = 0

    def heartbeat(self) -> None:
        self.beats += 1

    def raise_if_cancelled(self) -> None:
        return None


def test_executor_heartbeats_during_a_long_provider_call() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    output = Classification(category="billing", urgency="medium")
    lifecycle = _HeartbeatCountingLifecycle(interval=0.02)

    result = execute_ai_activity(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
        registry=registry_for(),
        provider=_SlowProvider(output, delay=0.2),
        lifecycle=lifecycle,
    )

    assert result == output
    # The background heartbeater fired during the ~0.2s blocking call.
    assert lifecycle.beats >= 1


def test_executor_heartbeats_during_a_retry_backoff() -> None:
    # The provider returns instantly, so the only wall-clock time is the backoff
    # after the transient error — heartbeats here prove the heartbeater spans the
    # whole retry loop, not just the call.
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    output = Classification(category="billing", urgency="medium")
    provider = FakeProvider([ProviderTransientError("transient", provider="fake"), output])
    lifecycle = _HeartbeatCountingLifecycle(interval=0.02)

    result = execute_ai_activity(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
        registry=registry_for(),
        provider=provider,
        provider_retry_policy=ProviderRetryPolicy(
            max_attempts=2, initial_backoff_seconds=0.15, jitter_ratio=0.0
        ),
        lifecycle=lifecycle,
    )

    assert result == output
    assert lifecycle.beats >= 1


def test_executor_resolves_provider_param_precedence() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
        provider_params=ProviderParams(max_tokens=16000),
    )
    provider = FakeProvider([Classification(category="billing", urgency="medium")])
    provider.default_provider_params = ProviderParams(
        model="runtime-model",
        max_tokens=4096,
        timeout=30,
    )
    registry = InlinePromptRegistry(
        {
            "support/classify": ResolvedPrompt(
                ref=PromptRef("support/classify"),
                messages=(ChatMessage(role="user", content="Classify {{ subject }}"),),
                provider_params=ProviderParams(model="prompt-model", max_tokens=8000, top_p=0.8),
            )
        }
    )

    execute_ai_activity(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question"),
        registry=registry,
        provider=provider,
    )

    provider_params = provider.calls[0]["provider_params"]
    assert provider.calls[0]["model"] == "prompt-model"
    assert provider_params.to_dict() == {
        "model": "prompt-model",
        "max_tokens": 16000,
        "top_p": 0.8,
        "timeout": 30,
    }
    assert provider.calls[0]["metadata"]["typeflux"]["join"]["activity_execution_manifest_hash"]


def test_provider_params_reject_out_of_range_values() -> None:
    with pytest.raises(ValueError, match="temperature must be between 0 and 2"):
        ProviderParams(temperature=5)
    with pytest.raises(ValueError, match="temperature must be between 0 and 2"):
        ProviderParams(temperature=-1)
    with pytest.raises(ValueError, match="max_tokens must be >= 1"):
        ProviderParams(max_tokens=0)
    with pytest.raises(ValueError, match="top_p must be between 0 and 1"):
        ProviderParams(top_p=1.5)
    # In-range values are accepted (temperature up to the cross-provider max of 2).
    ProviderParams(temperature=2, top_p=1.0, max_tokens=1)


def test_executor_forwards_observation_context_to_opt_in_observer_and_provider() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    prompt = object()
    resolved = ResolvedPrompt(
        ref=activity.prompt_ref,
        messages=(ChatMessage(role="user", content="Classify {{ subject }}"),),
        resolved_version="prompt-v1",
        model="fake-model",
        observation_context=langfuse_prompt_context(prompt),
    )
    provider = _ObservationContextProvider(Classification(category="billing", urgency="medium"))
    observer = _ObservationContextObserver()

    result = execute_ai_activity(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question"),
        registry=InlinePromptRegistry({activity.prompt_ref: resolved}),
        provider=provider,
        observer=observer,
    )

    assert result == Classification(category="billing", urgency="medium")
    assert provider.observation_contexts[0][LANGFUSE_PROMPT_CONTEXT_KEY] is prompt
    assert observer.observation_contexts[0][LANGFUSE_PROMPT_CONTEXT_KEY] is prompt
    assert _contains_identity(provider.calls[0]["metadata"], prompt) is False
    assert _contains_identity(observer.generation_metadata[0], prompt) is False


def test_executor_forwards_provider_usage_to_generation_observation() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    provider = FakeProvider(
        [Classification(category="billing", urgency="medium")],
        usage=ProviderUsage(input_tokens=120, output_tokens=45, model="fake-model"),
    )
    observer = _UsageRecordingObserver()

    result = execute_ai_activity(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
        registry=registry_for(),
        provider=provider,
        observer=observer,
    )

    assert result == Classification(category="billing", urgency="medium")
    assert observer.usages == [
        ProviderUsage(input_tokens=120, output_tokens=45, model="fake-model")
    ]


def test_executor_forwards_usage_for_failed_validation_attempts() -> None:
    # Tokens are billed even when output validation fails; every generation
    # observation must carry its usage, not only the successful one.
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
        validation_retries=1,
    )
    try:
        Classification.model_validate({})
    except ValidationError as exc:
        validation_error = exc

    class _BilledThenFailingProvider:
        provider_name = "fake"
        default_provider_params = ProviderParams()

        def __init__(self) -> None:
            self.attempts = 0

        def structured_call(
            self,
            *,
            messages,
            output_schema,
            model=None,
            temperature=None,
            provider_params=None,
            metadata=None,
            artifacts=(),
            usage_sink=None,
        ):
            self.attempts += 1
            if usage_sink is not None:
                usage_sink(ProviderUsage(input_tokens=100, output_tokens=10 * self.attempts))
            if self.attempts == 1:
                raise validation_error
            return Classification(category="billing", urgency="medium")

    observer = _UsageRecordingObserver()

    result = execute_ai_activity(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
        registry=registry_for(),
        provider=_BilledThenFailingProvider(),
        observer=observer,
    )

    assert result == Classification(category="billing", urgency="medium")
    assert observer.usages == [
        ProviderUsage(input_tokens=100, output_tokens=10),
        ProviderUsage(input_tokens=100, output_tokens=20),
    ]


def test_executor_skips_usage_forwarding_without_reported_usage() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    provider = FakeProvider([Classification(category="billing", urgency="medium")])
    observer = _UsageRecordingObserver()

    execute_ai_activity(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
        registry=registry_for(),
        provider=provider,
        observer=observer,
    )

    assert observer.usages == []


def test_executor_does_not_pass_observation_context_to_legacy_surfaces() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    resolved = ResolvedPrompt(
        ref=activity.prompt_ref,
        messages=(ChatMessage(role="user", content="Classify {{ subject }}"),),
        observation_context=langfuse_prompt_context(object()),
    )

    result = execute_ai_activity(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question"),
        registry=InlinePromptRegistry({activity.prompt_ref: resolved}),
        provider=FakeProvider([Classification(category="billing", urgency="medium")]),
        observer=_RecordingObserver(),
    )

    assert result == Classification(category="billing", urgency="medium")


def test_executor_attaches_execution_mode_to_generation_metadata() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    output = Classification(category="billing", urgency="medium")
    observer = _MetadataRecordingObserver()

    result = execute_ai_activity(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
        registry=registry_for(),
        provider=FakeProvider([output]),
        observer=observer,
    )

    assert result == output
    controls = observer.generation_metadata[0]["typeflux"]["provider_controls"]
    assert controls["execution_mode"] == "sync"
    assert controls["retry_attempt"] == 0
    assert controls["max_attempts"] == 1


def test_executor_resolves_artifacts_and_records_safe_manifest_metadata(tmp_path: Path) -> None:
    document = tmp_path / "claim-note.txt"
    document.write_text("Claim note text", encoding="utf-8")
    activity = AIActivity(
        name="review_claim",
        input_type=ClaimReviewInput,
        output_type=ClaimReviewOutput,
        prompt_ref=PromptRef("claim/review"),
        artifact_inputs=(
            ArtifactInput(
                name="claim_documents",
                from_path="input.documents",
                kind="data",
                media_types=("text/plain",),
                attach=ArtifactAttachment(text="Use the attached claim documents."),
            ),
        ),
    )
    registry = InlinePromptRegistry(
        {
            "claim/review": ResolvedPrompt(
                ref=PromptRef("claim/review"),
                messages=(
                    ChatMessage(
                        role="user",
                        content=(
                            TextPart("Review claim {{ claim_id }}."),
                            ArtifactGroupPart(
                                group="claim_documents",
                                text="Primary evidence:",
                            ),
                        ),
                    ),
                ),
                resolved_version="prompt-v1",
            )
        }
    )
    provider = FakeProvider([ClaimReviewOutput(summary="ok")])
    observer = _MetadataRecordingObserver()

    result = execute_ai_activity(
        activity=activity,
        input_value=ClaimReviewInput(claim_id="C-100", documents=[str(document)]),
        registry=registry,
        provider=provider,
        observer=observer,
        artifact_policy=ArtifactPolicy(local_roots=(tmp_path,)),
    )

    assert result == ClaimReviewOutput(summary="ok")
    resolved = provider.calls[0]["artifacts"][0].artifacts[0]
    assert resolved.group == "claim_documents"
    assert resolved.sha256
    assert resolved.size_bytes == len("Claim note text")
    assert len(provider.calls[0]["messages"]) == 2
    activity_metadata = observer.activity_execution_manifests[0].to_dict()
    assert activity_metadata["artifact_inputs"][0]["name"] == "claim_documents"
    assert activity_metadata["artifacts"][0]["artifacts"][0]["sha256"] == resolved.sha256
    assert str(document) not in json.dumps(activity_metadata)
    assert "Use the attached claim documents" not in json.dumps(activity_metadata)


def test_executor_skips_role_only_attach_for_empty_optional_artifact_group(
    tmp_path: Path,
) -> None:
    activity = AIActivity(
        name="review_claim",
        input_type=ClaimReviewInput,
        output_type=ClaimReviewOutput,
        prompt_ref=PromptRef("claim/review"),
        artifact_inputs=(
            ArtifactInput(
                name="claim_documents",
                from_path="input.documents",
                required=False,
                attach=ArtifactAttachment(),
            ),
        ),
    )
    provider = FakeProvider([ClaimReviewOutput(summary="ok")])

    result = execute_ai_activity(
        activity=activity,
        input_value=ClaimReviewInput(claim_id="C-100", documents=[]),
        registry=InlinePromptRegistry({"claim/review": "Review {{ claim_id }}"}),
        provider=provider,
        artifact_policy=ArtifactPolicy(local_roots=(tmp_path,)),
    )

    assert result == ClaimReviewOutput(summary="ok")
    assert len(provider.calls[0]["messages"]) == 1
    assert provider.calls[0]["artifacts"][0].name == "claim_documents"
    assert provider.calls[0]["artifacts"][0].artifacts == ()


def test_executor_rejects_local_artifacts_without_allowed_roots(tmp_path: Path) -> None:
    document = tmp_path / "claim-note.txt"
    document.write_text("Claim note text", encoding="utf-8")
    activity = AIActivity(
        name="review_claim",
        input_type=ClaimReviewInput,
        output_type=ClaimReviewOutput,
        prompt_ref=PromptRef("claim/review"),
        artifact_inputs=(ArtifactInput(name="claim_documents", from_path="input.documents"),),
    )
    provider = FakeProvider([ClaimReviewOutput(summary="ok")])

    with pytest.raises(ValueError, match="local_root"):
        execute_ai_activity(
            activity=activity,
            input_value=ClaimReviewInput(claim_id="C-100", documents=[str(document)]),
            registry=InlinePromptRegistry({"claim/review": "Review {{ claim_id }}"}),
            provider=provider,
        )

    assert provider.calls == []


def test_executor_rejects_unknown_media_type_when_allow_list_configured(
    tmp_path: Path,
) -> None:
    document = tmp_path / "claim-note.unregistered"
    document.write_text("Claim note text", encoding="utf-8")
    activity = AIActivity(
        name="review_claim",
        input_type=ClaimReviewInput,
        output_type=ClaimReviewOutput,
        prompt_ref=PromptRef("claim/review"),
        artifact_inputs=(
            ArtifactInput(
                name="claim_documents",
                from_path="input.documents",
                media_types=("text/plain",),
            ),
        ),
    )
    provider = FakeProvider([ClaimReviewOutput(summary="ok")])

    with pytest.raises(ValueError, match="unknown media type"):
        execute_ai_activity(
            activity=activity,
            input_value=ClaimReviewInput(claim_id="C-100", documents=[str(document)]),
            registry=InlinePromptRegistry({"claim/review": "Review {{ claim_id }}"}),
            provider=provider,
            artifact_policy=ArtifactPolicy(local_roots=(tmp_path,)),
        )

    assert provider.calls == []


def test_executor_rejects_artifact_provider_without_artifacts_argument(tmp_path: Path) -> None:
    document = tmp_path / "claim-note.txt"
    document.write_text("Claim note text", encoding="utf-8")
    activity = AIActivity(
        name="review_claim",
        input_type=ClaimReviewInput,
        output_type=ClaimReviewOutput,
        prompt_ref=PromptRef("claim/review"),
        artifact_inputs=(ArtifactInput(name="claim_documents", from_path="input.documents"),),
    )

    class ProviderWithoutArtifacts:
        def structured_call(
            self,
            *,
            messages,
            output_schema,
            model=None,
            temperature=None,
            metadata=None,
        ):
            return output_schema(summary="ok")

    with pytest.raises(TypeError, match="must accept artifacts"):
        execute_ai_activity(
            activity=activity,
            input_value=ClaimReviewInput(claim_id="C-100", documents=[str(document)]),
            registry=InlinePromptRegistry({"claim/review": "Review {{ claim_id }}"}),
            provider=ProviderWithoutArtifacts(),
            artifact_policy=ArtifactPolicy(local_roots=(tmp_path,)),
        )


@pytest.mark.asyncio
async def test_async_executor_calls_provider_with_rendered_messages() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    output = Classification(category="billing", urgency="medium")
    provider = _AsyncFakeProvider([output])

    result = await execute_ai_activity_async(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
        registry=registry_for(),
        provider=provider,
    )

    assert result == output
    assert provider.calls[0]["messages"][0].content == "Classify Invoice / gold"
    assert provider.calls[0]["output_schema"] is Classification
    assert provider.calls[0]["metadata"]["typeflux"]["activity_name"] == "classify_ticket"
    assert (
        provider.calls[0]["metadata"]["typeflux"]["provider"]["resolved_prompt_version"]
        == "prompt-v1"
    )
    assert provider.calls[0]["metadata"]["typeflux"]["provider"]["validation_attempt"] == 0
    assert provider.calls[0]["metadata"]["typeflux"]["provider_controls"]["execution_mode"] == (
        "async"
    )


@pytest.mark.asyncio
async def test_async_executor_attaches_execution_mode_to_generation_metadata() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    output = Classification(category="billing", urgency="medium")
    observer = _MetadataRecordingObserver()

    result = await execute_ai_activity_async(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
        registry=registry_for(),
        provider=_AsyncFakeProvider([output]),
        observer=observer,
    )

    assert result == output
    controls = observer.generation_metadata[0]["typeflux"]["provider_controls"]
    assert controls["execution_mode"] == "async"
    assert controls["retry_attempt"] == 0
    assert controls["max_attempts"] == 1


def test_executor_attaches_activity_execution_manifest_without_prompt_text() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    output = Classification(category="billing", urgency="medium")
    provider = FakeProvider([output])

    execute_ai_activity(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
        registry=registry_for(),
        provider=provider,
    )

    metadata = provider.calls[0]["metadata"]
    typeflux = metadata["typeflux"]
    assert typeflux["level"] == "provider"
    assert typeflux["activity_name"] == "classify_ticket"
    assert "activity_execution_manifest" not in typeflux
    assert typeflux["join"]["activity_manifest_hash"]
    assert typeflux["join"]["activity_execution_manifest_hash"]
    assert typeflux["provider"]["resolved_prompt_version"] == "prompt-v1"
    assert typeflux["provider"]["validation_attempt"] == 0
    assert "Classify" not in json.dumps(typeflux)
    assert "Invoice" not in json.dumps(typeflux)


def test_executor_pre_redacts_provider_metadata_when_observer_has_redactor() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    registry = InlinePromptRegistry(
        {
            "support/classify": ResolvedPrompt(
                ref=PromptRef("support/classify"),
                messages=(ChatMessage(role="user", content="Classify {{ subject }}"),),
                metadata={"owner_email": "jane.doe@example.com"},
            )
        }
    )
    provider = FakeProvider([Classification(category="billing", urgency="medium")])

    execute_ai_activity(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question"),
        registry=registry,
        provider=provider,
        observer=_RedactingObserver(),
    )

    assert provider.calls[0]["metadata"]["owner_email"] == "[REDACTED_EMAIL]"


def test_validation_error_triggers_repair_retry() -> None:
    try:
        Score.model_validate({"score": "not-an-int"})
    except ValidationError as exc:
        validation_error = exc
    activity = AIActivity(
        name="score_ticket",
        input_type=Ticket,
        output_type=Score,
        prompt_ref=PromptRef("support/classify"),
        validation_retries=1,
    )
    provider = FakeProvider([validation_error, Score(score=5)])

    result = execute_ai_activity(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
        registry=registry_for(),
        provider=provider,
    )

    assert result == Score(score=5)
    assert len(provider.calls) == 2
    assert provider.calls[1]["messages"][-1].role == "system"
    assert provider.calls[1]["metadata"]["typeflux"]["provider"]["validation_attempt"] == 1


@pytest.mark.asyncio
async def test_async_validation_error_triggers_repair_retry() -> None:
    try:
        Score.model_validate({"score": "not-an-int"})
    except ValidationError as exc:
        validation_error = exc
    activity = AIActivity(
        name="score_ticket",
        input_type=Ticket,
        output_type=Score,
        prompt_ref=PromptRef("support/classify"),
        validation_retries=1,
    )
    provider = _AsyncFakeProvider([validation_error, Score(score=5)])

    result = await execute_ai_activity_async(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
        registry=registry_for(),
        provider=provider,
    )

    assert result == Score(score=5)
    assert len(provider.calls) == 2
    assert provider.calls[1]["messages"][-1].role == "system"
    assert provider.calls[1]["metadata"]["typeflux"]["provider"]["validation_attempt"] == 1


def test_always_invalid_provider_attempts_are_bounded_by_validation_retries() -> None:
    try:
        Score.model_validate({"score": "not-an-int"})
    except ValidationError as exc:
        validation_error = exc
    activity = AIActivity(
        name="score_ticket",
        input_type=Ticket,
        output_type=Score,
        prompt_ref=PromptRef("support/classify"),
        validation_retries=2,
    )
    provider = FakeProvider([validation_error, validation_error, validation_error])

    with pytest.raises(AIActivityOutputValidationError) as exc_info:
        execute_ai_activity(
            activity=activity,
            input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
            registry=registry_for(),
            provider=provider,
        )

    # validation_retries bounds the repair loop: initial call plus exactly
    # two repair attempts, then the terminal error.
    assert len(provider.calls) == 3
    assert "3 attempt(s)" in str(exc_info.value)


def test_validation_error_exhaustion_raises_safe_summary() -> None:
    try:
        Score.model_validate({"score": "secret-model-output-XYZ"})
    except ValidationError as exc:
        validation_error = exc
    activity = AIActivity(
        name="score_ticket",
        input_type=Ticket,
        output_type=Score,
        prompt_ref=PromptRef("support/classify"),
        validation_retries=0,
    )
    provider = FakeProvider([validation_error])

    with pytest.raises(AIActivityOutputValidationError) as exc_info:
        execute_ai_activity(
            activity=activity,
            input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
            registry=registry_for(),
            provider=provider,
        )

    message = str(exc_info.value)
    assert "score_ticket" in message
    assert "1 attempt(s)" in message
    assert "1 validation error(s) for Score" in message
    assert "secret-model-output-XYZ" not in message
    # The raw validation error must not ride into the serialized Temporal
    # failure chain through cause or context.
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_async_validation_error_exhaustion_raises_safe_summary() -> None:
    try:
        Score.model_validate({"score": "secret-model-output-XYZ"})
    except ValidationError as exc:
        validation_error = exc
    activity = AIActivity(
        name="score_ticket",
        input_type=Ticket,
        output_type=Score,
        prompt_ref=PromptRef("support/classify"),
        validation_retries=0,
    )
    provider = _AsyncFakeProvider([validation_error])

    with pytest.raises(AIActivityOutputValidationError) as exc_info:
        await execute_ai_activity_async(
            activity=activity,
            input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
            registry=registry_for(),
            provider=provider,
        )

    assert "secret-model-output-XYZ" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_openai_provider_default_leaves_validation_repair_to_typeflux() -> None:
    try:
        Score.model_validate({"score": "not-an-int"})
    except ValidationError as exc:
        validation_error = exc
    activity = AIActivity(
        name="score_ticket",
        input_type=Ticket,
        output_type=Score,
        prompt_ref=PromptRef("support/classify"),
        validation_retries=1,
    )
    completions = _SequentialCompletions([validation_error, Score(score=5)])
    provider = OpenAIProvider(
        enable_langfuse=True,
        instructor_client=_SequentialInstructorClient(completions),
    )

    result = execute_ai_activity(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
        registry=registry_for(),
        provider=provider,
    )

    assert result == Score(score=5)
    assert len(completions.calls) == 2
    assert [call["max_retries"] for call in completions.calls] == [0, 0]
    assert completions.calls[1]["messages"][-1]["role"] == "system"
    assert (
        "Previous response failed output validation"
        in completions.calls[1]["messages"][-1]["content"]
    )
    assert completions.calls[1]["metadata"]["typeflux"]["provider"]["validation_attempt"] == 1


def test_provider_retry_delay_honors_retry_after_and_jitter() -> None:
    policy = ProviderRetryPolicy(max_attempts=3, initial_backoff_seconds=1.0, jitter_ratio=0.5)
    assert policy.retry_delay_seconds(0, rng=lambda low, high: 0.0) == 1.0
    # The provider's Retry-After hint floors the configured backoff.
    assert policy.retry_delay_seconds(0, retry_after_seconds=5.0, rng=lambda low, high: 0.0) == 5.0
    # Jitter is bounded by jitter_ratio, proportional to the delay.
    assert policy.retry_delay_seconds(0, rng=lambda low, high: high) == 1.5
    zero_backoff = ProviderRetryPolicy(max_attempts=2)
    assert zero_backoff.retry_delay_seconds(0) == 0.0
    # Retry-After is honored even with no configured backoff.
    assert (
        zero_backoff.retry_delay_seconds(0, retry_after_seconds=2.0, rng=lambda low, high: 0.0)
        == 2.0
    )
    with pytest.raises(ValueError, match="jitter_ratio"):
        ProviderRetryPolicy(jitter_ratio=1.5)


def test_rate_limit_retry_sleeps_for_retry_after_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("typeflux.execution.executor.sleep", sleeps.append)
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    provider = FakeProvider(
        [
            ProviderRateLimitError(
                "rate limited",
                provider="fake",
                status_code=429,
                retry_after_seconds=7.5,
            ),
            Classification(category="billing", urgency="medium"),
        ]
    )

    result = execute_ai_activity(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
        registry=registry_for(),
        provider=provider,
        provider_retry_policy=ProviderRetryPolicy(max_attempts=2, jitter_ratio=0.0),
    )

    assert result == Classification(category="billing", urgency="medium")
    assert sleeps == [7.5]


def test_rate_limit_error_uses_provider_retry_policy() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    provider = FakeProvider(
        [
            ProviderRateLimitError("rate limited", provider="fake", status_code=429),
            Classification(category="billing", urgency="medium"),
        ]
    )

    result = execute_ai_activity(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
        registry=registry_for(),
        provider=provider,
        provider_retry_policy=ProviderRetryPolicy(max_attempts=2),
    )

    assert result == Classification(category="billing", urgency="medium")
    assert len(provider.calls) == 2
    first_controls = provider.calls[0]["metadata"]["typeflux"]["provider_controls"]
    second_controls = provider.calls[1]["metadata"]["typeflux"]["provider_controls"]
    assert first_controls["retry_attempt"] == 0
    assert second_controls["retry_attempt"] == 1
    assert second_controls["previous_error_type"] == "ProviderRateLimitError"
    assert second_controls["previous_error_status_code"] == 429
    assert second_controls["rate_limited"] is True


def test_rate_limit_error_reraises_after_exhausting_max_attempts() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    provider = FakeProvider(
        [
            ProviderRateLimitError("rate limited", provider="fake", status_code=429),
            ProviderRateLimitError("rate limited", provider="fake", status_code=429),
        ]
    )

    with pytest.raises(ProviderRateLimitError):
        execute_ai_activity(
            activity=activity,
            input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
            registry=registry_for(),
            provider=provider,
            provider_retry_policy=ProviderRetryPolicy(max_attempts=2),
        )

    assert len(provider.calls) == 2


def test_rate_limit_error_not_retried_when_policy_disables_rate_limit_retry() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    provider = FakeProvider(
        [
            ProviderRateLimitError("rate limited", provider="fake", status_code=429),
            Classification(category="billing", urgency="medium"),
        ]
    )

    with pytest.raises(ProviderRateLimitError):
        execute_ai_activity(
            activity=activity,
            input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
            registry=registry_for(),
            provider=provider,
            provider_retry_policy=ProviderRetryPolicy(max_attempts=3, retry_rate_limits=False),
        )

    assert len(provider.calls) == 1


def test_transient_error_not_retried_when_policy_disables_transient_retry() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    provider = FakeProvider(
        [
            ProviderTransientError("upstream blip", provider="fake", status_code=503),
            Classification(category="billing", urgency="medium"),
        ]
    )

    with pytest.raises(ProviderTransientError):
        execute_ai_activity(
            activity=activity,
            input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
            registry=registry_for(),
            provider=provider,
            provider_retry_policy=ProviderRetryPolicy(max_attempts=3, retry_transient_errors=False),
        )

    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_async_rate_limit_error_uses_provider_retry_policy() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )
    provider = _AsyncFakeProvider(
        [
            ProviderRateLimitError("rate limited", provider="fake", status_code=429),
            Classification(category="billing", urgency="medium"),
        ]
    )

    result = await execute_ai_activity_async(
        activity=activity,
        input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
        registry=registry_for(),
        provider=provider,
        provider_retry_policy=ProviderRetryPolicy(max_attempts=2),
    )

    assert result == Classification(category="billing", urgency="medium")
    assert len(provider.calls) == 2
    first_controls = provider.calls[0]["metadata"]["typeflux"]["provider_controls"]
    second_controls = provider.calls[1]["metadata"]["typeflux"]["provider_controls"]
    assert first_controls["retry_attempt"] == 0
    assert second_controls["retry_attempt"] == 1
    assert second_controls["previous_error_type"] == "ProviderRateLimitError"
    assert second_controls["previous_error_status_code"] == 429
    assert second_controls["rate_limited"] is True


def test_oserror_escapes_for_temporal_retry() -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=Ticket,
        output_type=Classification,
        prompt_ref=PromptRef("support/classify"),
    )

    with pytest.raises(OSError):
        execute_ai_activity(
            activity=activity,
            input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
            registry=registry_for(),
            provider=FakeProvider([OSError("transport failed")]),
        )


def test_hook_runs_after_validated_output() -> None:
    @ai_activity.defn(prompt=PromptRef("support/classify"), output=Classification)
    def classify(ticket: Ticket, output: Classification) -> Classification:
        return output.model_copy(update={"urgency": "high"})

    provider = FakeProvider([Classification(category="billing", urgency="low")])

    result = execute_ai_activity(
        activity=classify,
        input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
        registry=registry_for(),
        provider=provider,
    )

    assert result == Classification(category="billing", urgency="high")


@pytest.mark.asyncio
async def test_async_executor_runs_sync_hook_in_thread() -> None:
    event_loop_thread = threading.get_ident()
    hook_threads: list[int] = []

    @ai_activity.defn(prompt=PromptRef("support/classify"), output=Classification)
    def classify(ticket: Ticket, output: Classification) -> Classification:
        hook_threads.append(threading.get_ident())
        return output.model_copy(update={"urgency": "high"})

    provider = _AsyncFakeProvider([Classification(category="billing", urgency="low")])

    result = await execute_ai_activity_async(
        activity=classify,
        input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
        registry=registry_for(),
        provider=provider,
    )

    assert result == Classification(category="billing", urgency="high")
    assert hook_threads
    assert hook_threads[0] != event_loop_thread


def test_hook_returning_wrong_type_fails() -> None:
    @ai_activity.defn(prompt=PromptRef("support/classify"), output=Classification)
    def classify(ticket: Ticket, output: Classification) -> Classification:
        return ticket  # type: ignore[return-value]

    with pytest.raises(TypeError, match="hook must return"):
        execute_ai_activity(
            activity=classify,
            input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
            registry=registry_for(),
            provider=FakeProvider([Classification(category="billing", urgency="low")]),
        )


def test_observer_records_activity_generation_and_hook_io() -> None:
    @ai_activity.defn(prompt=PromptRef("support/classify"), output=Classification)
    def classify(ticket: Ticket, output: Classification) -> Classification:
        return output.model_copy(update={"urgency": "high"})

    observer = _RecordingObserver()

    result = execute_ai_activity(
        activity=classify,
        input_value=Ticket(subject="Invoice", body="Question", nested={"value": "gold"}),
        registry=registry_for(),
        provider=FakeProvider([Classification(category="billing", urgency="low")]),
        observer=observer,
    )

    assert result == Classification(category="billing", urgency="high")
    assert observer.events == [
        (
            "activity_start",
            "classify",
            {"subject": "Invoice", "body": "Question", "nested": {"value": "gold"}},
        ),
        ("generation_start", ["Classify Invoice / gold"], Classification),
        ("generation_output", {"category": "billing", "urgency": "low"}),
        (
            "hook_start",
            {
                "activity_input": {
                    "subject": "Invoice",
                    "body": "Question",
                    "nested": {"value": "gold"},
                },
                "llm_output": {"category": "billing", "urgency": "low"},
            },
        ),
        ("hook_output", {"category": "billing", "urgency": "high"}),
        ("activity_output", {"category": "billing", "urgency": "high"}),
    ]


class _RecordingObserver:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    @contextmanager
    def observe_activity(
        self, *, activity, input_value, manifest, execution_manifest, invocation_context
    ):
        self.events.append(("activity_start", activity.name, input_value.model_dump(mode="json")))
        yield _RecordingActivityObservation(self.events)

    def flush(self) -> None:
        return None

    def redact_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        return metadata


class _ObservationContextProvider:
    provider_name = "fake"
    default_model = "fake-model"

    def __init__(self, output: Classification) -> None:
        self.output = output
        self.calls: list[dict[str, Any]] = []
        self.observation_contexts: list[dict[str, Any] | None] = []

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
        observation_context: dict[str, Any] | None = None,
    ) -> BaseModel:
        self.calls.append(
            {
                "messages": list(messages),
                "output_schema": output_schema,
                "model": model,
                "temperature": temperature,
                "metadata": metadata,
            }
        )
        self.observation_contexts.append(observation_context)
        return self.output


class _ObservationContextObserver:
    def __init__(self) -> None:
        self.generation_metadata: list[dict[str, Any]] = []
        self.observation_contexts: list[dict[str, Any] | None] = []

    @contextmanager
    def observe_activity(
        self, *, activity, input_value, manifest, execution_manifest, invocation_context
    ):
        yield _ObservationContextActivityObservation(self)

    def flush(self) -> None:
        return None

    def redact_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        return metadata


class _ObservationContextActivityObservation:
    def __init__(self, observer: _ObservationContextObserver) -> None:
        self.observer = observer

    def update_output(self, output_value: BaseModel) -> None:
        return None

    def update_error(self, error: BaseException) -> None:
        return None

    @contextmanager
    def observe_generation(
        self,
        *,
        messages,
        output_schema,
        metadata,
        validation_attempt,
        model,
        temperature,
        observation_context=None,
    ):
        self.observer.generation_metadata.append(metadata)
        self.observer.observation_contexts.append(observation_context)
        yield _RecordingHandle([], "generation")

    @contextmanager
    def observe_hook(self, *, activity_input, llm_output, metadata):
        yield _RecordingHandle([], "hook")


class _UsageRecordingObserver:
    def __init__(self) -> None:
        self.usages: list[Any] = []

    @contextmanager
    def observe_activity(
        self, *, activity, input_value, manifest, execution_manifest, invocation_context
    ):
        yield _UsageRecordingActivityObservation(self)

    def flush(self) -> None:
        return None

    def redact_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        return metadata


class _UsageRecordingActivityObservation:
    def __init__(self, observer: _UsageRecordingObserver) -> None:
        self.observer = observer

    def update_output(self, output_value: BaseModel) -> None:
        return None

    def update_error(self, error: BaseException) -> None:
        return None

    @contextmanager
    def observe_generation(self, **kwargs):
        yield _UsageRecordingGenerationHandle(self.observer)

    @contextmanager
    def observe_hook(self, *, activity_input, llm_output, metadata):
        yield _RecordingHandle([], "hook")


class _UsageRecordingGenerationHandle:
    def __init__(self, observer: _UsageRecordingObserver) -> None:
        self.observer = observer

    def update_output(self, output_value: BaseModel) -> None:
        return None

    def update_error(self, error: BaseException) -> None:
        return None

    def update_usage(self, usage: Any) -> None:
        self.observer.usages.append(usage)


class _MetadataRecordingObserver:
    def __init__(self) -> None:
        self.generation_metadata: list[dict[str, Any]] = []
        self.activity_execution_manifests: list[Any] = []

    @contextmanager
    def observe_activity(
        self, *, activity, input_value, manifest, execution_manifest, invocation_context
    ):
        self.activity_execution_manifests.append(execution_manifest)
        yield _MetadataRecordingActivityObservation(self.generation_metadata)

    def flush(self) -> None:
        return None

    def redact_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        return metadata


class _MetadataRecordingActivityObservation:
    def __init__(self, generation_metadata: list[dict[str, Any]]) -> None:
        self.generation_metadata = generation_metadata

    def update_output(self, output_value: BaseModel) -> None:
        return None

    def update_error(self, error: BaseException) -> None:
        return None

    @contextmanager
    def observe_generation(
        self, *, messages, output_schema, metadata, validation_attempt, model, temperature
    ):
        self.generation_metadata.append(metadata)
        yield _RecordingHandle([], "generation")

    @contextmanager
    def observe_hook(self, *, activity_input, llm_output, metadata):
        yield _RecordingHandle([], "hook")


class _RecordingHandle:
    def __init__(self, events: list[tuple[Any, ...]], prefix: str) -> None:
        self.events = events
        self.prefix = prefix

    def update_output(self, output_value: BaseModel) -> None:
        self.events.append((f"{self.prefix}_output", output_value.model_dump(mode="json")))

    def update_error(self, error: BaseException) -> None:
        self.events.append((f"{self.prefix}_error", type(error).__name__))


class _RecordingActivityObservation(_RecordingHandle):
    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        super().__init__(events, "activity")

    @contextmanager
    def observe_generation(
        self, *, messages, output_schema, metadata, validation_attempt, model, temperature
    ):
        self.events.append(
            ("generation_start", [message.content for message in messages], output_schema)
        )
        yield _RecordingHandle(self.events, "generation")

    @contextmanager
    def observe_hook(self, *, activity_input, llm_output, metadata):
        self.events.append(
            (
                "hook_start",
                {
                    "activity_input": activity_input.model_dump(mode="json"),
                    "llm_output": llm_output.model_dump(mode="json"),
                },
            )
        )
        yield _RecordingHandle(self.events, "hook")


class _SequentialCompletions:
    def __init__(self, responses: list[BaseModel | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> BaseModel:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("no fake OpenAI responses left")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def create_with_completion(self, **kwargs: Any) -> tuple[BaseModel, Any]:
        model = self.create(**kwargs)
        completion = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop")])
        return model, completion


class _SequentialInstructorClient:
    def __init__(self, completions: _SequentialCompletions) -> None:
        self.chat = type("Chat", (), {"completions": completions})()


def _contains_identity(value: Any, target: object) -> bool:
    if value is target:
        return True
    if isinstance(value, dict):
        return any(_contains_identity(item, target) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_identity(item, target) for item in value)
    return False


class _AsyncFakeProvider:
    provider_name = "fake"
    default_model = "fake-model"

    def __init__(self, responses: list[BaseModel | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def async_structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BaseModel:
        self.calls.append(
            {
                "messages": list(messages),
                "output_schema": output_schema,
                "model": model,
                "temperature": temperature,
                "metadata": metadata,
            }
        )
        if not self.responses:
            raise AssertionError("no fake async provider responses left")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _RedactingObserver:
    redactor = RegexPIIRedactor.default()

    @contextmanager
    def observe_activity(
        self, *, activity, input_value, manifest, execution_manifest, invocation_context
    ):
        yield _RecordingActivityObservation([])

    def flush(self) -> None:
        return None

    def redact_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        redacted = self.redactor.redact(metadata)
        return redacted if isinstance(redacted, dict) else metadata
