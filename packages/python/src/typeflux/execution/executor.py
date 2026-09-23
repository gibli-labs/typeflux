from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from inspect import Parameter, signature
from time import sleep
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

from typeflux.core.artifacts import (
    ArtifactInput,
    ArtifactPolicy,
    ResolvedArtifactGroup,
    attach_artifact_messages,
    resolve_artifact_inputs,
)
from typeflux.core.contracts import (
    ActivityContext,
    AIActivity,
    CachedSessionHandle,
    ChatMessage,
    ModerationResult,
    OutputCheckViolation,
    ProviderParams,
)
from typeflux.core.errors import TypefluxError
from typeflux.core.render import render_messages
from typeflux.execution.cache import (
    CacheStore,
    activity_cache_key,
    build_cache_record,
    cache_reads_bypassed,
)
from typeflux.execution.controls import ProviderRetryPolicy
from typeflux.execution.lifecycle import (
    NO_OP_LIFECYCLE,
    ActivityLifecycle,
    heartbeating,
    heartbeating_async,
)
from typeflux.execution.observer import (
    ActivityObservation,
    AIActivityObserver,
    NoOpObserver,
)
from typeflux.execution.provider_identity import provider_identifier
from typeflux.execution.session_cache import (
    assert_stable_system_prefix,
    session_cache_identity,
)
from typeflux.manifests import (
    AIInvocationContext,
    build_activity_execution_manifest,
    build_activity_manifest,
)
from typeflux.observability.semantic import invocation_metadata
from typeflux.prompts import PromptRegistry
from typeflux.providers import (
    AsyncModelProvider,
    ModelProvider,
    ProviderUsage,
    provider_default_params,
    validate_provider_params_supported,
)
from typeflux.providers.base import (
    no_session_cache_handle,
    supports_session_cache,
)
from typeflux.providers.errors import (
    ProviderPolicyError,
    ProviderRateLimitError,
    ProviderTransientError,
)

logger = logging.getLogger(__name__)


class AIActivityOutputValidationError(TypefluxError):
    """Terminal output-validation failure with a safe, non-input-bearing message.

    Raised in place of the raw Pydantic ValidationError when validation
    retries are exhausted, so model output previews never reach Temporal
    workflow history or trace status messages.
    """


class ModerationBlockedError(TypefluxError):
    """A moderation checkpoint blocked the activity's output (#158).

    Terminal and non-retryable (the same output reproduces the verdict). The
    message carries only the semantic classification (categories), never the
    raw output, so it is safe for workflow history / trace status.
    """


def _moderation_verdict(activity: AIActivity, output: BaseModel) -> ModerationResult:
    """Run the moderator and validate its return type. Raises on a bad return."""
    assert activity.moderation is not None  # caller guards
    result = activity.moderation.moderator(output)
    if not isinstance(result, ModerationResult):
        raise TypeError("moderator must return ModerationResult")
    return result


def _apply_moderation_verdict(activity: AIActivity, result: ModerationResult) -> None:
    """Apply ``on_violation`` to a verdict: ``block`` raises, ``flag`` logs through.

    Records only the classification verdict (no raw output; #158/#325).
    """
    moderation = activity.moderation
    assert moderation is not None  # caller guards
    verdict = {
        "activity": activity.name,
        "flagged": result.flagged,
        "categories": list(result.categories),
        "max_score": result.max_score,
        "on_violation": moderation.on_violation,
    }
    if not result.flagged:
        logger.debug("moderation passed for %r: %s", activity.name, verdict)
        return
    if moderation.on_violation == "flag":
        logger.warning("moderation flagged output for %r: %s", activity.name, verdict)
        return
    categories = ", ".join(result.categories) or "unspecified"
    raise ModerationBlockedError(
        f"moderation blocked output for activity {activity.name!r}: {categories}"
    )


def _moderation_config_precheck(activity: AIActivity, guard: ModerationPolicyGuard | None) -> None:
    # Policy config gate (#158 PR2): runs even when the activity has no moderator,
    # so a policy `required`/`require_block` rejects an unguarded regulated
    # activity. Wrapped like the provider-model guard → non-retryable policy error.
    if guard is None:
        return
    moderation = activity.moderation
    try:
        guard.enforce_moderation_config(
            activity_name=activity.name,
            moderation_configured=moderation is not None,
            on_violation=moderation.on_violation if moderation is not None else None,
        )
    except ProviderPolicyError:
        raise
    except Exception as exc:
        raise ProviderPolicyError(str(exc), original=exc) from exc


def _moderator_name(moderation: Any) -> str:
    moderator = moderation.moderator
    return getattr(moderator, "__name__", type(moderator).__name__)


def _record_moderation_verdict(
    activity: AIActivity, result: ModerationResult, decision: str, activity_observation: Any
) -> None:
    # Record the verdict as redaction-exempt audit evidence on the activity trace
    # (#158): the classification only — decision/categories/score/moderator — never
    # the raw output. Uses a dedicated top-level key (not nested under typeflux)
    # because backend update_metadata REPLACES a top-level key instead of
    # deep-merging, so nesting under typeflux would clobber the activity's base
    # metadata. typeflux_moderation.* is preserved by DEFAULT_EXCLUDED_PATHS.
    activity_observation.update_metadata(
        {
            "typeflux_moderation": {
                "decision": decision,
                "categories": list(result.categories),
                "max_score": result.max_score,
                "moderator": _moderator_name(activity.moderation),
            }
        }
    )


def _enforce_moderation_result(
    activity: AIActivity,
    result: ModerationResult,
    guard: ModerationPolicyGuard | None,
    activity_observation: Any,
) -> None:
    # Policy can only tighten the activity's own on_violation (#158 PR2): a
    # disallowed category / at-threshold score forces a block even for `flag`.
    reason = None
    if guard is not None:
        reason = guard.moderation_policy_block(
            activity_name=activity.name,
            categories=result.categories,
            max_score=result.max_score,
        )
    on_violation = activity.moderation.on_violation if activity.moderation is not None else "block"
    if reason is not None or (result.flagged and on_violation == "block"):
        decision = "block"
    elif result.flagged:
        decision = "flag"
    else:
        decision = "allow"
    # Record before enforcing, so a blocked verdict still lands on the trace.
    _record_moderation_verdict(activity, result, decision, activity_observation)
    if reason is not None:
        raise ModerationBlockedError(reason)
    _apply_moderation_verdict(activity, result)


def _run_moderation_checkpoint(
    activity: AIActivity,
    output: BaseModel,
    activity_observation: Any,
    guard: ModerationPolicyGuard | None = None,
) -> None:
    """Synchronous moderation checkpoint on validated output (#158).

    Runs after any ``hook`` (so it sees the final output) and never mutates it.
    The policy config gate runs first (it can require moderation be present), then
    the moderator's verdict is applied and the policy can escalate it to a block.
    """
    try:
        _moderation_config_precheck(activity, guard)
        if activity.moderation is None:
            return
        result = _moderation_verdict(activity, output)
        _enforce_moderation_result(activity, result, guard, activity_observation)
    except BaseException as exc:
        activity_observation.update_error(exc)
        raise


async def _run_moderation_checkpoint_async(
    activity: AIActivity,
    output: BaseModel,
    activity_observation: Any,
    guard: ModerationPolicyGuard | None = None,
) -> None:
    """Async checkpoint — offloads the (possibly blocking) moderator to a thread,
    mirroring the hook, so a moderator doing I/O can't stall the event loop."""
    try:
        _moderation_config_precheck(activity, guard)
        if activity.moderation is None:
            return
        result = await asyncio.to_thread(_moderation_verdict, activity, output)
        _enforce_moderation_result(activity, result, guard, activity_observation)
    except BaseException as exc:
        activity_observation.update_error(exc)
        raise


def _terminal_validation_error(
    activity: AIActivity,
    exc: ValidationError,
    *,
    attempts: int,
) -> AIActivityOutputValidationError:
    logger.debug("terminal output validation failure for %r: %s", activity.name, exc)
    return AIActivityOutputValidationError(
        f"output validation failed for activity {activity.name!r} after "
        f"{attempts} attempt(s): {exc.error_count()} validation error(s) "
        f"for {activity.output_type.__name__}"
    )


class _OutputCheckViolations(Exception):
    """Internal control-flow signal (#745): an ``output_check`` rejected the
    candidate output. Routed to the repair loop exactly like a provider
    :class:`pydantic.ValidationError`, so the model gets its ``validation_retries``
    chances to self-correct an input-aware contract miss (a hallucinated citation,
    a non-verbatim quote — the class zod/pydantic cannot express)."""

    def __init__(self, violations: list[OutputCheckViolation]) -> None:
        super().__init__("output_check violations")
        self.violations = violations


def _render_output_check_violations(violations: Sequence[OutputCheckViolation]) -> str:
    """Serialize violations legibly for the repair prompt + terminal error (#745):
    one line per violation, ``path: message`` when a dotted path is present —
    mirroring how a schema-parse miss serializes its pydantic errors."""

    lines: list[str] = []
    for violation in violations:
        path = ".".join(str(part) for part in violation.path)
        lines.append(f"- {path}: {violation.message}" if path else f"- {violation.message}")
    return "\n".join(lines)


def _run_output_check(
    activity: AIActivity,
    input_value: BaseModel,
    output: BaseModel,
) -> list[OutputCheckViolation]:
    """Run the activity's optional ``output_check`` (#745). Returns a non-empty list
    of violations to REJECT (a raised exception is normalized to a single violation,
    the ergonomic "reject" signal), or ``[]`` to accept / when no check is defined.
    The ``input_value`` is the PARSED input, so a check sees materialized defaults."""

    if activity.output_check is None:
        return []
    try:
        result = activity.output_check(input_value, output)
    except Exception as exc:  # noqa: BLE001 - any raise is a rejection signal
        return [OutputCheckViolation(message=str(exc))]
    # Normalization runs OUTSIDE the try: a contract-shape TypeError below is an
    # author bug and must fail loud, never be swallowed into a "violation" repair turn.
    return _normalize_output_check_result(result)


def _normalize_output_check_result(result: object) -> list[OutputCheckViolation]:
    """Normalize an output_check's return value (#745 review): ``None``/``[]`` is a
    PASS; a list/tuple of :class:`OutputCheckViolation` rejects; a SINGLE violation
    object (a common slip for ``[violation]``) is coerced to a one-element list;
    anything else — a string (which ``list()`` would explode into per-character
    garbage), a number, truthy junk — is a pointed TypeError naming the contract."""

    if result is None:
        return []
    if isinstance(result, OutputCheckViolation):
        return [result]
    if isinstance(result, (list, tuple)):
        violations = list(result)
        if all(isinstance(violation, OutputCheckViolation) for violation in violations):
            return violations
    raise TypeError("output_check must return None or a list of OutputCheckViolation")


def _terminal_output_check_error(
    activity: AIActivity,
    violations: Sequence[OutputCheckViolation],
    *,
    attempts: int,
) -> AIActivityOutputValidationError:
    """The terminal error naming the outputCheck violations once repair retries are
    exhausted (#745). Reuses the output-validation taxonomy (the worker already
    treats it as terminal / non-retryable)."""

    return AIActivityOutputValidationError(
        f"output validation failed for activity {activity.name!r} after "
        f"{attempts} attempt(s): outputCheck violations:\n"
        f"{_render_output_check_violations(violations)}"
    )


@dataclass(frozen=True)
class PreparedAIActivityExecution:
    activity: AIActivity
    input_value: BaseModel
    resolved_prompt: Any
    manifest: Any
    messages: list[ChatMessage]
    artifacts: tuple[ResolvedArtifactGroup, ...]
    observer: AIActivityObserver
    invocation_context: AIInvocationContext | None
    provider_model: str | None
    provider_params: ProviderParams
    initial_execution_manifest: Any
    max_attempts: int
    retry_policy: ProviderRetryPolicy


class ProviderModelPolicyGuard(Protocol):
    def enforce_provider_model(
        self,
        *,
        provider_name: str,
        provider_model: str | None,
        activity_name: str | None = None,
        prompt_name: str | None = None,
    ) -> None: ...


class ModerationPolicyGuard(Protocol):
    """Policy-governed moderation enforcement at the output checkpoint (#158 PR2)."""

    def enforce_moderation_config(
        self,
        *,
        activity_name: str,
        moderation_configured: bool,
        on_violation: str | None,
    ) -> None: ...

    def moderation_policy_block(
        self,
        *,
        activity_name: str,
        categories: Sequence[str],
        max_score: float | None,
    ) -> str | None: ...


class PolicyGuard(ProviderModelPolicyGuard, ModerationPolicyGuard, Protocol):
    """A guard enforcing both provider/model and moderation policy (RuntimePolicyGuard)."""


def execute_ai_activity(
    *,
    activity: AIActivity,
    input_value: BaseModel,
    registry: PromptRegistry,
    provider: ModelProvider,
    invocation_context: AIInvocationContext | None = None,
    observer: AIActivityObserver | None = None,
    provider_retry_policy: ProviderRetryPolicy | None = None,
    provider_call_metadata: dict[str, Any] | None = None,
    artifact_policy: ArtifactPolicy | None = None,
    provider_model_policy_guard: ProviderModelPolicyGuard | None = None,
    moderation_policy_guard: ModerationPolicyGuard | None = None,
    lifecycle: ActivityLifecycle = NO_OP_LIFECYCLE,
    cached_session: CachedSessionHandle | None = None,
    deps: Any = None,
    tenant_resolver: Callable[[BaseModel], Mapping[str, str]] | None = None,
    cache_store: CacheStore | None = None,
) -> BaseModel:
    prepared = prepare_ai_activity_execution(
        activity=activity,
        input_value=input_value,
        registry=registry,
        provider=provider,
        invocation_context=invocation_context,
        observer=observer,
        provider_retry_policy=provider_retry_policy,
        artifact_policy=artifact_policy,
        provider_model_policy_guard=provider_model_policy_guard,
        cached_session=cached_session,
    )
    return execute_prepared_ai_activity(
        prepared,
        provider=provider,
        provider_call_metadata=provider_call_metadata,
        moderation_policy_guard=moderation_policy_guard,
        lifecycle=lifecycle,
        cached_session=cached_session,
        deps=deps,
        tenant_resolver=tenant_resolver,
        cache_store=cache_store,
    )


async def execute_ai_activity_async(
    *,
    activity: AIActivity,
    input_value: BaseModel,
    registry: PromptRegistry,
    provider: AsyncModelProvider,
    invocation_context: AIInvocationContext | None = None,
    observer: AIActivityObserver | None = None,
    provider_retry_policy: ProviderRetryPolicy | None = None,
    provider_call_metadata: dict[str, Any] | None = None,
    artifact_policy: ArtifactPolicy | None = None,
    provider_model_policy_guard: ProviderModelPolicyGuard | None = None,
    moderation_policy_guard: ModerationPolicyGuard | None = None,
    lifecycle: ActivityLifecycle = NO_OP_LIFECYCLE,
    cached_session: CachedSessionHandle | None = None,
    deps: Any = None,
    tenant_resolver: Callable[[BaseModel], Mapping[str, str]] | None = None,
    cache_store: CacheStore | None = None,
) -> BaseModel:
    prepared = await asyncio.to_thread(
        prepare_ai_activity_execution,
        activity=activity,
        input_value=input_value,
        registry=registry,
        provider=provider,
        invocation_context=invocation_context,
        observer=observer,
        provider_retry_policy=provider_retry_policy,
        artifact_policy=artifact_policy,
        provider_model_policy_guard=provider_model_policy_guard,
        cached_session=cached_session,
    )
    return await execute_prepared_ai_activity_async(
        prepared,
        provider=provider,
        provider_call_metadata=provider_call_metadata,
        moderation_policy_guard=moderation_policy_guard,
        lifecycle=lifecycle,
        cached_session=cached_session,
        deps=deps,
        tenant_resolver=tenant_resolver,
        cache_store=cache_store,
    )


def _surface_provider_hint_mismatch(
    resolved_prompt: Any,
    *,
    provider_name: str,
    activity_name: str,
) -> None:
    # Prompt config may carry a descriptive provider hint; a family mismatch
    # with the configured provider is surfaced (names only, no prompt content)
    # instead of silently ignored.
    config = resolved_prompt.metadata.get("langfuse.prompt_config")
    if not isinstance(config, Mapping):
        return
    typeflux_config = config.get("typeflux")
    if not isinstance(typeflux_config, Mapping):
        return
    hint = typeflux_config.get("provider_hint")
    if not isinstance(hint, Mapping):
        return
    hint_name = hint.get("name")
    if not isinstance(hint_name, str) or not hint_name or hint_name == provider_name:
        return
    resolved_prompt.metadata["langfuse.provider_hint_mismatch"] = {
        "hint": hint_name,
        "provider": provider_name,
    }
    logger.warning(
        "prompt %r provider hint %r does not match configured provider %r (activity %r)",
        resolved_prompt.ref.name,
        hint_name,
        provider_name,
        activity_name,
    )


def prepare_ai_activity_execution(
    *,
    activity: AIActivity,
    input_value: BaseModel,
    registry: PromptRegistry,
    provider: Any,
    invocation_context: AIInvocationContext | None = None,
    observer: AIActivityObserver | None = None,
    provider_retry_policy: ProviderRetryPolicy | None = None,
    artifact_policy: ArtifactPolicy | None = None,
    provider_model_policy_guard: ProviderModelPolicyGuard | None = None,
    cached_session: CachedSessionHandle | None = None,
) -> PreparedAIActivityExecution:
    if not isinstance(input_value, activity.input_type):
        raise TypeError(f"input_value must be an instance of {activity.input_type.__name__}")

    resolved_prompt = registry.resolve(activity.prompt_ref)
    effective_provider_params = provider_default_params(provider).merge(
        resolved_prompt.provider_params,
        activity.provider_params,
    )
    provider_model = effective_provider_params.model
    provider_name = provider_identifier(provider)
    _surface_provider_hint_mismatch(
        resolved_prompt,
        provider_name=provider_name,
        activity_name=activity.name,
    )
    validate_provider_params_supported(
        provider,
        effective_provider_params,
        activity_name=activity.name,
        prompt_name=activity.prompt_ref.name,
    )
    manifest = build_activity_manifest(
        activity,
        resolved_prompt,
        provider_params=effective_provider_params,
    )
    rendered_messages = render_messages(resolved_prompt.messages, input_value)
    # Reference artifacts are part of the cached prefix, so when the prep step
    # actually cached them they are NOT re-sent per item (they live in the
    # provider cache). Gated on the handle's reference_cached flag — NOT the
    # provider style — so a supported reference handle that cached only the system
    # prefix (representative item missing the artifact, empty resolution) keeps
    # sending the document instead of silently dropping it. Prefix-style re-sends
    # the whole prefix per item, and a fail-soft (uncached) handle needs full
    # context, so neither skips.
    artifact_inputs = activity.artifact_inputs
    if cached_session is not None and cached_session.reference_cached:
        artifact_inputs = tuple(ai for ai in artifact_inputs if ai.cache_role != "reference")
    artifacts = resolve_artifact_inputs(
        input_value,
        artifact_inputs,
        policy=artifact_policy,
    )
    if _prefix_reference_composition_applies(cached_session, artifact_inputs):
        # Prefix-style cache + reference artifacts still in play: lift the stable
        # reference documents to the FRONT of the conversation (right after the
        # rendered system prefix) so they join the cached prefix, instead of
        # trailing the varying per-item turn where they can never be cached (#362).
        messages = _compose_prefix_stable_messages(rendered_messages, artifact_inputs, artifacts)
    else:
        # Every other path (uncached, reference-style, prefix-style with no
        # reference inputs) keeps today's append-last order byte-for-byte.
        messages = attach_artifact_messages(rendered_messages, artifact_inputs, artifacts)
    observer = observer or NoOpObserver()
    if provider_model_policy_guard is not None:
        try:
            provider_model_policy_guard.enforce_provider_model(
                provider_name=provider_name,
                provider_model=provider_model,
                activity_name=activity.name,
                prompt_name=activity.prompt_ref.name,
            )
        except ProviderPolicyError:
            raise
        except Exception as exc:
            raise ProviderPolicyError(
                str(exc),
                provider=provider_name,
                original=exc,
            ) from exc
    initial_execution_manifest = build_activity_execution_manifest(
        activity=activity,
        activity_manifest=manifest,
        resolved_prompt=resolved_prompt,
        rendered_messages=messages,
        artifact_groups=artifacts,
        validation_attempt=0,
        provider_model=provider_model,
        provider_params=effective_provider_params,
    )

    return PreparedAIActivityExecution(
        activity=activity,
        input_value=input_value,
        resolved_prompt=resolved_prompt,
        manifest=manifest,
        messages=messages,
        artifacts=artifacts,
        observer=observer,
        invocation_context=invocation_context,
        provider_model=provider_model,
        provider_params=effective_provider_params,
        initial_execution_manifest=initial_execution_manifest,
        max_attempts=activity.validation_retries + 1,
        retry_policy=provider_retry_policy or ProviderRetryPolicy(),
    )


def _reference_artifact_inputs(activity: AIActivity) -> tuple[Any, ...]:
    return tuple(ai for ai in activity.artifact_inputs if ai.cache_role == "reference")


def _prefix_reference_composition_applies(
    cached_session: CachedSessionHandle | None,
    artifact_inputs: Sequence[ArtifactInput],
) -> bool:
    """True when the per-item message assembly must promote reference artifacts
    into the cached prefix (#362): the session cache is engaged, prefix-style, and
    at least one ``cache: reference`` input is still present (a reference-style hit
    has already dropped them, so this is only ever the prefix lane)."""
    return (
        cached_session is not None
        and cached_session.supported
        and cached_session.style == "prefix"
        and any(ai.cache_role == "reference" for ai in artifact_inputs)
    )


def _compose_prefix_stable_messages(
    rendered_messages: Sequence[ChatMessage],
    artifact_inputs: Sequence[ArtifactInput],
    artifacts: Sequence[ResolvedArtifactGroup],
) -> list[ChatMessage]:
    """Assemble the prefix-style per-item message list (#362, D362-2):
    ``[rendered system…, reference attach…, rendered non-system…, per-item attach…]``.

    Reference attach messages insert immediately after the rendered system block so
    they sit at the front of the conversation (the cacheable prefix); the varying
    per-item turn and any per-item artifact attachments stay behind them, exactly
    where append-last would have put them today."""
    reference_inputs = [ai for ai in artifact_inputs if ai.cache_role == "reference"]
    per_item_inputs = [ai for ai in artifact_inputs if ai.cache_role != "reference"]
    system_messages = [m for m in rendered_messages if m.role == "system"]
    non_system_messages = [m for m in rendered_messages if m.role != "system"]
    # attach_artifact_messages appends onto its base; an empty base yields just the
    # attach messages for the requested inputs (group lookup spans all resolved
    # artifacts, so passing the shared ``artifacts`` for both slices is correct).
    reference_attach = attach_artifact_messages([], reference_inputs, artifacts)
    per_item_attach = attach_artifact_messages([], per_item_inputs, artifacts)
    return [*system_messages, *reference_attach, *non_system_messages, *per_item_attach]


def _prefix_stable_message_count(activity: AIActivity) -> int:
    """Static count of the reference-artifact turns that will lead the prefix-style
    per-item conversation (#362, D362-3). Only reference inputs that attach as
    user/assistant turns count — a system-role reference rides in the (already
    cached) system block, not the conversation, so it must not shift the Anthropic
    breakpoint index. Needs no artifact resolution: attach roles are static."""
    return sum(
        1
        for ai in activity.artifact_inputs
        if ai.cache_role == "reference" and ai.attach is not None and ai.attach.role != "system"
    )


def _prefix_has_per_item_artifacts(activity: AIActivity) -> bool:
    """Whether the prefix-style per-item conversation will carry trailing per-item
    (non-``cache: reference``) artifact attach turns after the varying per-item input
    (#698). Those make the legacy ``conversation[-2]`` breakpoint unsound: the last
    turn is a per-item artifact, so ``-2`` lands on the varying query rather than a
    stable instructions turn. Only non-system attaches count — a system-role artifact
    folds into the (cached) system block, not the conversation. Static (attach roles
    are declared, no resolution needed); conservatively True even for an optional
    per-item artifact, which at worst forgoes conversation caching for an item where
    it resolves empty (the system block still caches)."""
    return any(
        ai.cache_role != "reference" and ai.attach is not None and ai.attach.role != "system"
        for ai in activity.artifact_inputs
    )


def _repair_message_role(cached_session: CachedSessionHandle | None) -> Literal["system", "user"]:
    # On a reference-style cache hit the provider serves the cached prefix and
    # ignores per-call system content (e.g. Gemini drops system_instruction when
    # cached_content is set), so a validation-repair message appended as `system`
    # would be silently dropped — the retry would lose its correction guidance.
    # Send it as a user turn there so it reaches the model. Prefix-style and
    # uncached keep `system` (unchanged behavior). #368
    if (
        cached_session is not None
        and cached_session.supported
        and cached_session.style == "reference"
    ):
        return "user"
    return "system"


def prepare_session_cache(
    *,
    activity: AIActivity,
    registry: PromptRegistry,
    provider: Any,
    created_at: str,
    input_value: BaseModel | None = None,
    artifact_policy: ArtifactPolicy | None = None,
    provider_model_policy_guard: ProviderModelPolicyGuard | None = None,
) -> CachedSessionHandle:
    """Prepare a provider-side cached session for a map step's stable prefix (#60).

    Run once before a map fan-out (as its own activity, so the resulting handle
    is recorded in history and replay-safe). The stable prefix is the activity's
    **system** messages plus, for reference-style providers, its
    ``cache: reference`` artifacts (resolved from ``input_value`` — a
    representative item; reference artifacts must be identical across items).
    Computes the identity hash and asks the provider to prepare the session.

    Fail-soft: if caching is not requested/enabled, the provider has no
    session-cache capability, there is nothing stable to cache, or the provider's
    own preparation fails (e.g. a quota/tier limit such as Gemini's free-tier
    explicit-cache cap, or a transient API error), returns the content-free
    fallback handle (``supported=False``) so the per-item calls behave exactly as
    today — caching is an optimization and must never fail the workflow. A
    statically unstable prefix (``{{var}}`` in a cached system message) is the one
    loud failure: a developer misconfiguration, not a runtime condition. The
    caller stamps ``created_at`` (activity wall-clock, not workflow time).
    """
    resolved_prompt = registry.resolve(activity.prompt_ref)
    effective_provider_params = provider_default_params(provider).merge(
        resolved_prompt.provider_params,
        activity.provider_params,
    )
    provider_model = effective_provider_params.model
    provider_name = provider_identifier(provider)
    system_messages = [m for m in resolved_prompt.messages if m.role == "system"]

    def _identity(
        messages: list[ChatMessage],
        reference_artifacts: tuple[ResolvedArtifactGroup, ...] = (),
    ) -> str:
        return session_cache_identity(
            provider_name=provider_name,
            model=provider_model,
            provider_params=effective_provider_params,
            system_messages=messages,
            reference_artifacts=reference_artifacts,
            output_schema=activity.output_type,
        )

    config = activity.session_cache
    if config is None or not config.enabled or not supports_session_cache(provider):
        # Cheap fail-soft: don't resolve artifacts when caching is off/unavailable.
        return no_session_cache_handle(
            provider, identity_hash=_identity(system_messages), model=provider_model
        )

    # Reference artifacts join the cached prefix only for reference-style
    # providers (they upload it once); prefix-style re-sends the prefix per item
    # and caches it server-side, so resolving the doc here would just be a wasted
    # read. Resolved from a representative item — they must be identical across
    # items for the cache to be valid (#363).
    reference_inputs = _reference_artifact_inputs(activity)
    is_reference_style = getattr(provider, "session_cache_style", None) == "reference"
    reference_groups: tuple[ResolvedArtifactGroup, ...] = ()
    prefix_messages: list[ChatMessage] = system_messages
    if is_reference_style and reference_inputs and input_value is not None:
        try:
            reference_groups = resolve_artifact_inputs(
                input_value, reference_inputs, policy=artifact_policy
            )
            prefix_messages = attach_artifact_messages(
                system_messages, reference_inputs, reference_groups
            )
        except Exception as exc:  # noqa: BLE001 - caching must degrade, not crash
            # Resolving the reference artifact from the representative item failed
            # (missing path, unreadable file, policy violation). Degrade to
            # uncached: the per-item path will send full context (its own
            # resolution surfaces a real, per-item error if one exists).
            logger.warning(
                "reference-artifact resolution for cache prep failed for %r (%s); "
                "proceeding uncached",
                activity.name,
                type(exc).__name__,
            )
            return no_session_cache_handle(
                provider, identity_hash=_identity(system_messages), model=provider_model
            )

    # Identity covers the assembled prefix (system + how reference artifacts are
    # attached — role/text) plus the artifact bytes, so changing an attachment
    # prompt or moving it produces a distinct identity (#363 review).
    identity = _identity(prefix_messages, reference_groups)

    # Nothing stable to cache: a "supported" handle would be a silent no-op (the
    # prefix breakpoint never lands), so report no caching honestly. Prefix-style
    # deliberately leaves reference_groups empty (the doc is re-sent per item, not
    # uploaded at prep), so its declared reference INPUTS count as stable content
    # here (#362 review): a no-system activity whose only stable content is the
    # reference document must still engage the cache.
    prefix_reference_inputs = () if is_reference_style else reference_inputs
    if not system_messages and not reference_groups and not prefix_reference_inputs:
        return no_session_cache_handle(provider, identity_hash=identity, model=provider_model)

    # A per-item template variable in the system prefix means it is not stable
    # across items — refuse loudly rather than cache one item's rendered values.
    assert_stable_system_prefix(system_messages)

    # Enforce the model policy *before* preparing, so a rejected model is never
    # sent to the provider (reference-style prep uploads it to caches.create);
    # the per-item path enforces the same guard, so this keeps the prep path from
    # being a policy bypass (#60 review).
    if provider_model_policy_guard is not None:
        try:
            provider_model_policy_guard.enforce_provider_model(
                provider_name=provider_name,
                provider_model=provider_model,
                activity_name=activity.name,
                prompt_name=activity.prompt_ref.name,
            )
        except ProviderPolicyError:
            raise
        except Exception as exc:
            raise ProviderPolicyError(str(exc), provider=provider_name, original=exc) from exc

    try:
        handle = provider.prepare_cached_session(
            messages=prefix_messages,
            artifacts=reference_groups,
            model=provider_model,
            provider_params=effective_provider_params,
            identity_hash=identity,
            ttl_seconds=config.ttl_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - caching must degrade, not crash
        # A provider that *claims* the capability can still fail to prepare a
        # session at runtime (quota/tier caps, transient API errors). Degrade to
        # uncached rather than failing the whole map: the per-item calls just
        # send full context, as without caching. Retrying via Temporal would not
        # help a permanent cap and would only storm the provider.
        logger.warning(
            "session cache preparation failed for activity %r (%s); proceeding uncached",
            activity.name,
            type(exc).__name__,
        )
        return no_session_cache_handle(provider, identity_hash=identity, model=provider_model)
    # Stamp creation time here (in the prep activity), never in workflow code, so
    # the value is recorded once in history and stays constant on replay. Record
    # whether reference artifacts were actually cached — the per-item path keys
    # its skip on this, so it must reflect what was truly cached, not intent. An
    # optional reference artifact missing on the representative item yields an
    # empty group, which must NOT flip the flag (items that DO carry it would then
    # silently lose it), so require a group with real artifacts. It also requires
    # the returned handle to be ENGAGED: a provider may decline politely
    # (supported=False without raising), and flipping the flag for an unengaged
    # handle would drop documents that were never cached (#516, TS PR #515 parity).
    reference_cached = handle.supported and any(group.artifacts for group in reference_groups)
    # Prefix-style caches don't upload reference artifacts at prep (they re-send the
    # prefix per item); instead the handle carries the static count of stable
    # reference turns so the per-item Anthropic breakpoint can extend the cache over
    # them (#362). Reference-style and fail-soft handles leave it None. 0 ⇒ None so
    # the provider keeps its legacy conversation[-2] contract.
    prefix_stable_messages: int | None = None
    # Prefix-style also flags whether per-item (non-reference) artifact turns will
    # trail the varying input (#698): with no leading reference span to mark, that
    # shape has no stable conversation span, so the per-item Anthropic breakpoint
    # must skip the conversation rather than mis-mark the varying query at [-2].
    # Reference-style and fail-soft handles leave it False.
    per_item_artifact_messages = False
    if handle.supported and handle.style == "prefix":
        count = _prefix_stable_message_count(activity)
        prefix_stable_messages = count if count > 0 else None
        per_item_artifact_messages = _prefix_has_per_item_artifacts(activity)
    return handle.model_copy(
        update={
            "created_at": created_at,
            "reference_cached": reference_cached,
            "prefix_stable_messages": prefix_stable_messages,
            "per_item_artifact_messages": per_item_artifact_messages,
        }
    )


def _resolve_tenant(
    tenant_resolver: Callable[[BaseModel], Mapping[str, str]] | None,
    input_value: BaseModel,
) -> Mapping[str, str]:
    if tenant_resolver is None:
        return {}
    resolved = tenant_resolver(input_value)
    if resolved is None:
        return {}
    if isinstance(resolved, Mapping):
        return resolved
    # Validate at the boundary so a bad resolver fails here with a clear message,
    # not later inside the user's hook (AttributeError) or the cache scope.
    raise TypeError("tenant_resolver must return a Mapping[str, str] or None")


def _build_activity_context(
    activity: AIActivity,
    invocation_context: AIInvocationContext | None,
    input_value: BaseModel,
    deps: Any,
    tenant_resolver: Callable[[BaseModel], Mapping[str, str]] | None,
) -> ActivityContext:
    tenant = _resolve_tenant(tenant_resolver, input_value)
    ic = invocation_context
    return ActivityContext(
        activity_name=activity.name,
        namespace=ic.temporal_namespace if ic else None,
        workflow_id=ic.temporal_workflow_id if ic else None,
        run_id=ic.temporal_run_id if ic else None,
        activity_id=ic.temporal_activity_id if ic else None,
        attempt=ic.temporal_activity_attempt if ic else None,
        task_queue=ic.temporal_task_queue if ic else None,
        tenant=tenant,
        deps=deps,
    )


def _cache_lookup(
    activity: AIActivity,
    cache_store: CacheStore | None,
    prepared: PreparedAIActivityExecution,
    tenant_resolver: Callable[[BaseModel], Mapping[str, str]] | None,
    cached_session: Any,
) -> tuple[Any, BaseModel | None]:
    """Return ``(cache_key, cached_output_or_None)`` for a cache-enabled activity.

    ``cache_key`` is None when caching is not active (no policy / disabled / no
    store). A non-None cached output means a hit — the provider call is skipped.
    Pure (no observation side effects); the executor records hit/miss metadata.
    """

    if activity.cache is None or not activity.cache.enabled or cache_store is None:
        return None, None
    cache_key = activity_cache_key(
        prepared, _resolve_tenant(tenant_resolver, prepared.input_value), cached_session
    )
    if cache_reads_bypassed(activity.cache):
        return cache_key, None
    cached = cache_store.get(cache_key)
    if cached is None:
        return cache_key, None
    # Ignore a stale entry whose output schema has since evolved (the key tracks
    # input + prompt + params, not output shape): treat it as a miss so the
    # activity regenerates and overwrites rather than failing validation.
    if cached.get("output_schema_hash") != prepared.initial_execution_manifest.output_schema_hash:
        return cache_key, None
    return cache_key, activity.output_type.model_validate(cached["output"])


def _cache_store_output(
    cache_store: CacheStore | None,
    cache_key: Any,
    output: BaseModel,
    prepared: PreparedAIActivityExecution,
) -> None:
    """Write a freshly produced (cache-miss) output to the store (idempotent)."""

    if cache_key is None or cache_store is None:
        return
    cache_store.set(cache_key, build_cache_record(cache_key, output, prepared))


def execute_prepared_ai_activity(
    prepared: PreparedAIActivityExecution,
    *,
    provider: ModelProvider,
    provider_call_metadata: dict[str, Any] | None = None,
    moderation_policy_guard: ModerationPolicyGuard | None = None,
    cached_session: CachedSessionHandle | None = None,
    lifecycle: ActivityLifecycle = NO_OP_LIFECYCLE,
    deps: Any = None,
    tenant_resolver: Callable[[BaseModel], Mapping[str, str]] | None = None,
    cache_store: CacheStore | None = None,
) -> BaseModel:
    activity = prepared.activity
    resolved_prompt = prepared.resolved_prompt
    manifest = prepared.manifest
    messages = list(prepared.messages)
    artifacts = prepared.artifacts
    observer = prepared.observer
    invocation_context = prepared.invocation_context
    provider_model = prepared.provider_model
    provider_params = prepared.provider_params
    provider_call_metadata = _provider_call_metadata_with_execution_mode(
        provider_call_metadata,
        execution_mode="sync",
    )
    output: BaseModel | None = None
    successful_validation_attempt = 0

    with observer.observe_activity(
        activity=activity,
        input_value=prepared.input_value,
        manifest=manifest,
        execution_manifest=prepared.initial_execution_manifest,
        invocation_context=invocation_context,
    ) as activity_observation:
        cache_key, output = _cache_lookup(
            activity, cache_store, prepared, tenant_resolver, cached_session
        )
        if output is not None:
            # A hit skips the loop's raise_if_cancelled checkpoint, so honor
            # cooperative cancellation here before running hooks/moderation.
            lifecycle.raise_if_cancelled()
            # #745 review: re-run the output check on the HIT path. The check is
            # pure over (parsed input, output) — both available here — and nothing
            # check-related folds into the cache key, so an entry cached before a
            # check was added/tightened would otherwise be served forever despite
            # now violating it. A failing hit is treated as a MISS: fall through
            # to the generation loop (full repair retries), and the accepted
            # fresh output re-caches over the stale entry.
            if _run_output_check(activity, prepared.input_value, output):
                output = None
        cache_hit = output is not None
        if cache_hit:
            activity_observation.update_metadata({"typeflux_cache": {"hit": True}})
        for validation_attempt in range(prepared.max_attempts):
            if cache_hit:
                break
            # Abort promptly if the workflow was cancelled rather than burning
            # the next attempt (and its provider spend).
            lifecycle.raise_if_cancelled()
            execution_manifest = build_activity_execution_manifest(
                activity=activity,
                activity_manifest=manifest,
                resolved_prompt=resolved_prompt,
                rendered_messages=messages,
                artifact_groups=artifacts,
                validation_attempt=validation_attempt,
                provider_model=provider_model,
                provider_params=provider_params,
            )
            metadata = invocation_metadata(
                manifest=manifest,
                activity_execution_manifest=execution_manifest,
                invocation_context=invocation_context,
                validation_attempt=validation_attempt,
                extra=resolved_prompt.metadata,
            )
            generation_metadata = _metadata_with_provider_controls(
                metadata,
                provider_attempt=0,
                retry_policy=prepared.retry_policy,
                provider_call_metadata=provider_call_metadata,
                previous_error=None,
            )
            try:
                with activity_observation.observe_generation(
                    **_generation_observation_kwargs(
                        activity_observation,
                        messages=messages,
                        output_schema=activity.output_type,
                        metadata=generation_metadata,
                        validation_attempt=validation_attempt,
                        model=provider_params.model,
                        temperature=provider_params.temperature,
                        provider_params=provider_params,
                        observation_context=resolved_prompt.observation_context,
                    )
                ) as generation_observation:
                    reported_usage: list[ProviderUsage] = []
                    try:
                        output = _provider_structured_call_with_retries(
                            provider=provider,
                            messages=messages,
                            output_schema=activity.output_type,
                            model=provider_params.model,
                            temperature=provider_params.temperature,
                            provider_params=provider_params,
                            metadata=metadata,
                            artifacts=artifacts,
                            observer=observer,
                            retry_policy=prepared.retry_policy,
                            provider_call_metadata=provider_call_metadata,
                            observation_context=resolved_prompt.observation_context,
                            usage_sink=reported_usage.append,
                            cached_session=cached_session,
                            lifecycle=lifecycle,
                        )
                        # #745: input-aware, pre-acceptance output check, evaluated
                        # within the attempt (alongside the provider's schema parse)
                        # and BEFORE the output is accepted (cached / hooked). A
                        # rejection raises _OutputCheckViolations, caught below and
                        # routed to the SAME repair path a ValidationError uses.
                        _violations = _run_output_check(activity, prepared.input_value, output)
                        if _violations:
                            raise _OutputCheckViolations(_violations)
                    except Exception as exc:
                        generation_observation.update_error(exc)
                        raise
                    finally:
                        # Tokens are billed even when output validation fails;
                        # usage must land on every generation, not only
                        # successful ones.
                        _forward_generation_usage(generation_observation, reported_usage)
                    generation_observation.update_output(output)
                successful_validation_attempt = validation_attempt
                break
            except ValidationError as exc:
                if validation_attempt >= activity.validation_retries:
                    terminal = _terminal_validation_error(
                        activity, exc, attempts=validation_attempt + 1
                    )
                    activity_observation.update_error(terminal)
                    # Raise without a cause so the raw validation message
                    # (which can include model output previews) never enters
                    # the serialized Temporal failure chain.
                    raise terminal from None
                messages.append(
                    ChatMessage(
                        role=_repair_message_role(cached_session),
                        content=(
                            "Previous response failed output validation. "
                            "Return a corrected response matching the requested schema. "
                            f"Validation error: {exc}"
                        ),
                    )
                )
            except _OutputCheckViolations as exc:
                # #745: same repair machinery as a schema-parse miss — exhaust the
                # retries into a terminal error naming the violations, else append
                # the violations legibly as a repair turn.
                if validation_attempt >= activity.validation_retries:
                    terminal = _terminal_output_check_error(
                        activity, exc.violations, attempts=validation_attempt + 1
                    )
                    activity_observation.update_error(terminal)
                    raise terminal from None
                messages.append(
                    ChatMessage(
                        role=_repair_message_role(cached_session),
                        content=(
                            "Previous response failed output validation. "
                            "Return a corrected response matching the requested schema. "
                            f"Validation error: {_render_output_check_violations(exc.violations)}"
                        ),
                    )
                )
            except Exception as exc:
                activity_observation.update_error(exc)
                raise

        if not isinstance(output, activity.output_type):
            error = TypeError(f"provider must return {activity.output_type.__name__}")
            activity_observation.update_error(error)
            raise error

        if not cache_hit and cache_key is not None:
            activity_observation.update_metadata({"typeflux_cache": {"hit": False}})

        # #745: capture the accepted, PRE-hook output for the cache write. The hook
        # re-runs on every cache hit, so the cache must store the pre-hook value.
        # DEEP-COPIED (#745 review): a hook that mutates its `output` argument in
        # place would otherwise contaminate the cached value through the shared
        # reference (the store would hold post-hook state; every hit would
        # double-transform). Copied only when a write will actually happen. The
        # write itself runs only AFTER full acceptance (hook + moderation did not
        # raise), below.
        pre_hook_output = (
            output.model_copy(deep=True) if not cache_hit and cache_key is not None else output
        )

        if activity.hook is not None:
            hook_metadata = invocation_metadata(
                manifest=manifest,
                activity_execution_manifest=build_activity_execution_manifest(
                    activity=activity,
                    activity_manifest=manifest,
                    resolved_prompt=resolved_prompt,
                    rendered_messages=messages,
                    artifact_groups=artifacts,
                    validation_attempt=successful_validation_attempt,
                    provider_model=provider_model,
                    provider_params=provider_params,
                ),
                invocation_context=invocation_context,
                validation_attempt=successful_validation_attempt,
                extra=resolved_prompt.metadata,
            )
            with activity_observation.observe_hook(
                activity_input=prepared.input_value,
                llm_output=output,
                metadata=hook_metadata,
            ) as hook_observation:
                try:
                    if activity.hook_wants_context:
                        output = activity.hook(
                            prepared.input_value,
                            output,
                            _build_activity_context(
                                activity,
                                invocation_context,
                                prepared.input_value,
                                deps,
                                tenant_resolver,
                            ),
                        )
                    else:
                        output = activity.hook(prepared.input_value, output)
                except BaseException as exc:
                    hook_observation.update_error(exc)
                    activity_observation.update_error(exc)
                    raise
                if not isinstance(output, activity.output_type):
                    error = TypeError(f"hook must return {activity.output_type.__name__}")
                    hook_observation.update_error(error)
                    activity_observation.update_error(error)
                    raise error
                hook_observation.update_output(output)

        _run_moderation_checkpoint(activity, output, activity_observation, moderation_policy_guard)

        # #745: write the cross-run cache ONLY after full acceptance — the hook
        # (post-acceptance) and the moderation checkpoint both ran without raising.
        # A hook/outputCheck/moderation-rejected output must never be cached and
        # served on the next run (the pre-fix bug: the write preceded the hook).
        if not cache_hit:
            _cache_store_output(cache_store, cache_key, pre_hook_output, prepared)

        activity_observation.update_output(output)

    return output


async def execute_prepared_ai_activity_async(
    prepared: PreparedAIActivityExecution,
    *,
    provider: AsyncModelProvider,
    provider_call_metadata: dict[str, Any] | None = None,
    moderation_policy_guard: ModerationPolicyGuard | None = None,
    cached_session: CachedSessionHandle | None = None,
    lifecycle: ActivityLifecycle = NO_OP_LIFECYCLE,
    deps: Any = None,
    tenant_resolver: Callable[[BaseModel], Mapping[str, str]] | None = None,
    cache_store: CacheStore | None = None,
) -> BaseModel:
    activity = prepared.activity
    resolved_prompt = prepared.resolved_prompt
    manifest = prepared.manifest
    messages = list(prepared.messages)
    artifacts = prepared.artifacts
    observer = prepared.observer
    invocation_context = prepared.invocation_context
    provider_model = prepared.provider_model
    provider_params = prepared.provider_params
    provider_call_metadata = _provider_call_metadata_with_execution_mode(
        provider_call_metadata,
        execution_mode="async",
    )
    output: BaseModel | None = None
    successful_validation_attempt = 0

    with observer.observe_activity(
        activity=activity,
        input_value=prepared.input_value,
        manifest=manifest,
        execution_manifest=prepared.initial_execution_manifest,
        invocation_context=invocation_context,
    ) as activity_observation:
        cache_key, output = _cache_lookup(
            activity, cache_store, prepared, tenant_resolver, cached_session
        )
        if output is not None:
            # A hit skips the loop's raise_if_cancelled checkpoint, so honor
            # cooperative cancellation here before running hooks/moderation.
            lifecycle.raise_if_cancelled()
            # #745 review: re-run the output check on the HIT path (mirrors the sync
            # executor; off the event loop like the in-loop site). A failing hit is
            # treated as a MISS and falls through to the generation loop.
            if await asyncio.to_thread(_run_output_check, activity, prepared.input_value, output):
                output = None
        cache_hit = output is not None
        if cache_hit:
            activity_observation.update_metadata({"typeflux_cache": {"hit": True}})
        for validation_attempt in range(prepared.max_attempts):
            if cache_hit:
                break
            # Abort promptly if the workflow was cancelled rather than burning
            # the next attempt (and its provider spend).
            lifecycle.raise_if_cancelled()
            execution_manifest = build_activity_execution_manifest(
                activity=activity,
                activity_manifest=manifest,
                resolved_prompt=resolved_prompt,
                rendered_messages=messages,
                artifact_groups=artifacts,
                validation_attempt=validation_attempt,
                provider_model=provider_model,
                provider_params=provider_params,
            )
            metadata = invocation_metadata(
                manifest=manifest,
                activity_execution_manifest=execution_manifest,
                invocation_context=invocation_context,
                validation_attempt=validation_attempt,
                extra=resolved_prompt.metadata,
            )
            generation_metadata = _metadata_with_provider_controls(
                metadata,
                provider_attempt=0,
                retry_policy=prepared.retry_policy,
                provider_call_metadata=provider_call_metadata,
                previous_error=None,
            )
            try:
                with activity_observation.observe_generation(
                    **_generation_observation_kwargs(
                        activity_observation,
                        messages=messages,
                        output_schema=activity.output_type,
                        metadata=generation_metadata,
                        validation_attempt=validation_attempt,
                        model=provider_params.model,
                        temperature=provider_params.temperature,
                        provider_params=provider_params,
                        observation_context=resolved_prompt.observation_context,
                    )
                ) as generation_observation:
                    reported_usage: list[ProviderUsage] = []
                    try:
                        output = await _provider_structured_call_with_retries_async(
                            provider=provider,
                            messages=messages,
                            output_schema=activity.output_type,
                            model=provider_params.model,
                            temperature=provider_params.temperature,
                            provider_params=provider_params,
                            metadata=metadata,
                            artifacts=artifacts,
                            observer=observer,
                            retry_policy=prepared.retry_policy,
                            provider_call_metadata=provider_call_metadata,
                            observation_context=resolved_prompt.observation_context,
                            usage_sink=reported_usage.append,
                            cached_session=cached_session,
                            lifecycle=lifecycle,
                        )
                        # #745: input-aware, pre-acceptance output check (mirrors the
                        # sync path). The user check is sync; run it off the event loop
                        # so a check doing light CPU/lookup work can't stall it, then a
                        # rejection routes to the SAME repair path as a ValidationError.
                        _violations = await asyncio.to_thread(
                            _run_output_check, activity, prepared.input_value, output
                        )
                        if _violations:
                            raise _OutputCheckViolations(_violations)
                    except Exception as exc:
                        generation_observation.update_error(exc)
                        raise
                    finally:
                        # Tokens are billed even when output validation fails;
                        # usage must land on every generation, not only
                        # successful ones.
                        _forward_generation_usage(generation_observation, reported_usage)
                    generation_observation.update_output(output)
                successful_validation_attempt = validation_attempt
                break
            except ValidationError as exc:
                if validation_attempt >= activity.validation_retries:
                    terminal = _terminal_validation_error(
                        activity, exc, attempts=validation_attempt + 1
                    )
                    activity_observation.update_error(terminal)
                    # Raise without a cause so the raw validation message
                    # (which can include model output previews) never enters
                    # the serialized Temporal failure chain.
                    raise terminal from None
                messages.append(
                    ChatMessage(
                        role=_repair_message_role(cached_session),
                        content=(
                            "Previous response failed output validation. "
                            "Return a corrected response matching the requested schema. "
                            f"Validation error: {exc}"
                        ),
                    )
                )
            except _OutputCheckViolations as exc:
                # #745: same repair machinery as a schema-parse miss — exhaust the
                # retries into a terminal error naming the violations, else append
                # the violations legibly as a repair turn.
                if validation_attempt >= activity.validation_retries:
                    terminal = _terminal_output_check_error(
                        activity, exc.violations, attempts=validation_attempt + 1
                    )
                    activity_observation.update_error(terminal)
                    raise terminal from None
                messages.append(
                    ChatMessage(
                        role=_repair_message_role(cached_session),
                        content=(
                            "Previous response failed output validation. "
                            "Return a corrected response matching the requested schema. "
                            f"Validation error: {_render_output_check_violations(exc.violations)}"
                        ),
                    )
                )
            except Exception as exc:
                activity_observation.update_error(exc)
                raise

        if not isinstance(output, activity.output_type):
            error = TypeError(f"provider must return {activity.output_type.__name__}")
            activity_observation.update_error(error)
            raise error

        if not cache_hit and cache_key is not None:
            activity_observation.update_metadata({"typeflux_cache": {"hit": False}})

        # #745: capture the accepted, PRE-hook output for the cache write. The hook
        # re-runs on every cache hit, so the cache must store the pre-hook value.
        # DEEP-COPIED (#745 review): a hook that mutates its `output` argument in
        # place would otherwise contaminate the cached value through the shared
        # reference (the store would hold post-hook state; every hit would
        # double-transform). Copied only when a write will actually happen. The
        # write itself runs only AFTER full acceptance (hook + moderation did not
        # raise), below.
        pre_hook_output = (
            output.model_copy(deep=True) if not cache_hit and cache_key is not None else output
        )

        if activity.hook is not None:
            hook_metadata = invocation_metadata(
                manifest=manifest,
                activity_execution_manifest=build_activity_execution_manifest(
                    activity=activity,
                    activity_manifest=manifest,
                    resolved_prompt=resolved_prompt,
                    rendered_messages=messages,
                    artifact_groups=artifacts,
                    validation_attempt=successful_validation_attempt,
                    provider_model=provider_model,
                    provider_params=provider_params,
                ),
                invocation_context=invocation_context,
                validation_attempt=successful_validation_attempt,
                extra=resolved_prompt.metadata,
            )
            with activity_observation.observe_hook(
                activity_input=prepared.input_value,
                llm_output=output,
                metadata=hook_metadata,
            ) as hook_observation:
                try:
                    if activity.hook_wants_context:
                        context = _build_activity_context(
                            activity,
                            invocation_context,
                            prepared.input_value,
                            deps,
                            tenant_resolver,
                        )
                        output = await asyncio.to_thread(
                            activity.hook, prepared.input_value, output, context
                        )
                    else:
                        output = await asyncio.to_thread(
                            activity.hook, prepared.input_value, output
                        )
                except BaseException as exc:
                    hook_observation.update_error(exc)
                    activity_observation.update_error(exc)
                    raise
                if not isinstance(output, activity.output_type):
                    error = TypeError(f"hook must return {activity.output_type.__name__}")
                    hook_observation.update_error(error)
                    activity_observation.update_error(error)
                    raise error
                hook_observation.update_output(output)

        await _run_moderation_checkpoint_async(
            activity, output, activity_observation, moderation_policy_guard
        )

        # #745: write the cross-run cache ONLY after full acceptance — the hook
        # (post-acceptance) and the moderation checkpoint both ran without raising.
        # A hook/outputCheck/moderation-rejected output must never be cached and
        # served on the next run (the pre-fix bug: the write preceded the hook).
        if not cache_hit:
            _cache_store_output(cache_store, cache_key, pre_hook_output, prepared)

        activity_observation.update_output(output)

    return output


def _provider_structured_call_with_retries(
    *,
    provider: ModelProvider,
    messages: list[ChatMessage],
    output_schema: type[BaseModel],
    model: str | None,
    temperature: float | None,
    provider_params: ProviderParams,
    metadata: dict[str, Any],
    observer: AIActivityObserver,
    retry_policy: ProviderRetryPolicy,
    provider_call_metadata: dict[str, Any] | None,
    artifacts: tuple[ResolvedArtifactGroup, ...],
    observation_context: dict[str, Any],
    usage_sink: Callable[[ProviderUsage], None] | None = None,
    cached_session: CachedSessionHandle | None = None,
    lifecycle: ActivityLifecycle = NO_OP_LIFECYCLE,
) -> BaseModel:
    last_error: ProviderRateLimitError | ProviderTransientError | None = None
    # Heartbeat across the whole loop, not just the call: a rate-limit/transient
    # backoff sleep can be long (e.g. a retry-after), and Temporal must keep
    # seeing heartbeats during it or it fails the attempt for the timeout.
    with heartbeating(lifecycle):
        for provider_attempt in range(retry_policy.max_attempts):
            lifecycle.raise_if_cancelled()
            call_metadata = _metadata_with_provider_controls(
                metadata,
                provider_attempt=provider_attempt,
                retry_policy=retry_policy,
                provider_call_metadata=provider_call_metadata,
                previous_error=last_error,
            )
            try:
                return _provider_structured_call(
                    provider,
                    messages=messages,
                    output_schema=output_schema,
                    model=model,
                    temperature=temperature,
                    provider_params=provider_params,
                    metadata=observer.redact_metadata(call_metadata),
                    artifacts=artifacts,
                    observation_context=observation_context,
                    usage_sink=usage_sink,
                    cached_session=cached_session,
                )
            except (ProviderRateLimitError, ProviderTransientError) as exc:
                last_error = exc
                if provider_attempt >= retry_policy.max_attempts - 1 or not _should_retry_provider(
                    exc, retry_policy
                ):
                    raise
                delay = retry_policy.retry_delay_seconds(
                    provider_attempt,
                    retry_after_seconds=exc.retry_after_seconds,
                )
                if delay:
                    sleep(delay)
                # A cancel that landed during the backoff aborts now, before the
                # next attempt spends again.
                lifecycle.raise_if_cancelled()

    raise AssertionError("unreachable provider retry state")


async def _provider_structured_call_with_retries_async(
    *,
    provider: AsyncModelProvider,
    messages: list[ChatMessage],
    output_schema: type[BaseModel],
    model: str | None,
    temperature: float | None,
    provider_params: ProviderParams,
    metadata: dict[str, Any],
    observer: AIActivityObserver,
    retry_policy: ProviderRetryPolicy,
    provider_call_metadata: dict[str, Any] | None,
    artifacts: tuple[ResolvedArtifactGroup, ...],
    observation_context: dict[str, Any],
    usage_sink: Callable[[ProviderUsage], None] | None = None,
    cached_session: CachedSessionHandle | None = None,
    lifecycle: ActivityLifecycle = NO_OP_LIFECYCLE,
) -> BaseModel:
    last_error: ProviderRateLimitError | ProviderTransientError | None = None
    # Heartbeat across the whole loop (see the sync twin): a long backoff sleep
    # must keep heartbeating or Temporal fails the attempt for the timeout.
    async with heartbeating_async(lifecycle):
        for provider_attempt in range(retry_policy.max_attempts):
            lifecycle.raise_if_cancelled()
            call_metadata = _metadata_with_provider_controls(
                metadata,
                provider_attempt=provider_attempt,
                retry_policy=retry_policy,
                provider_call_metadata=provider_call_metadata,
                previous_error=last_error,
            )
            try:
                return await _provider_structured_call_async(
                    provider,
                    messages=messages,
                    output_schema=output_schema,
                    model=model,
                    temperature=temperature,
                    provider_params=provider_params,
                    metadata=observer.redact_metadata(call_metadata),
                    artifacts=artifacts,
                    observation_context=observation_context,
                    usage_sink=usage_sink,
                    cached_session=cached_session,
                )
            except (ProviderRateLimitError, ProviderTransientError) as exc:
                last_error = exc
                if provider_attempt >= retry_policy.max_attempts - 1 or not _should_retry_provider(
                    exc, retry_policy
                ):
                    raise
                delay = retry_policy.retry_delay_seconds(
                    provider_attempt,
                    retry_after_seconds=exc.retry_after_seconds,
                )
                if delay:
                    await asyncio.sleep(delay)
                # A cancel that landed during the backoff aborts now, before the
                # next attempt spends again.
                lifecycle.raise_if_cancelled()

    raise AssertionError("unreachable provider retry state")


def _should_retry_provider(
    error: ProviderRateLimitError | ProviderTransientError,
    retry_policy: ProviderRetryPolicy,
) -> bool:
    if isinstance(error, ProviderRateLimitError):
        return retry_policy.retry_rate_limits
    return retry_policy.retry_transient_errors


def _metadata_with_provider_controls(
    metadata: dict[str, Any],
    *,
    provider_attempt: int,
    retry_policy: ProviderRetryPolicy,
    provider_call_metadata: dict[str, Any] | None,
    previous_error: ProviderRateLimitError | ProviderTransientError | None,
) -> dict[str, Any]:
    call_metadata: dict[str, Any] = dict(metadata)
    typeflux = dict(call_metadata.get("typeflux") or {})
    provider_controls: dict[str, Any] = dict(provider_call_metadata or {})
    provider_controls.update(
        {
            "retry_attempt": provider_attempt,
            "max_attempts": retry_policy.max_attempts,
        }
    )
    if previous_error is not None:
        provider_controls["previous_error_type"] = type(previous_error).__name__
        provider_controls["previous_error_retryable"] = previous_error.retryable
        provider_controls["previous_error_status_code"] = previous_error.status_code
        provider_controls["rate_limited"] = isinstance(previous_error, ProviderRateLimitError)
    typeflux["provider_controls"] = {
        key: value for key, value in provider_controls.items() if value is not None
    }
    call_metadata["typeflux"] = typeflux
    return call_metadata


def _provider_call_metadata_with_execution_mode(
    provider_call_metadata: dict[str, Any] | None,
    *,
    execution_mode: str,
) -> dict[str, Any]:
    metadata = dict(provider_call_metadata or {})
    metadata.setdefault("execution_mode", execution_mode)
    return metadata


def _forward_generation_usage(
    generation_observation: Any,
    reported_usage: list[ProviderUsage],
) -> None:
    # Usage lands on the Typeflux generation observation when the backend
    # supports it; observers without update_usage simply skip it. The last
    # reported usage wins across provider retries.
    if not reported_usage:
        return
    update_usage = getattr(generation_observation, "update_usage", None)
    if callable(update_usage):
        update_usage(reported_usage[-1])


def _provider_structured_call(
    provider: ModelProvider,
    *,
    messages: list[ChatMessage],
    output_schema: type[BaseModel],
    model: str | None,
    temperature: float | None,
    provider_params: ProviderParams,
    metadata: dict[str, Any],
    artifacts: tuple[ResolvedArtifactGroup, ...],
    observation_context: dict[str, Any],
    usage_sink: Callable[[ProviderUsage], None] | None = None,
    cached_session: CachedSessionHandle | None = None,
) -> BaseModel:
    kwargs: dict[str, Any] = {
        "messages": messages,
        "output_schema": output_schema,
        "model": model,
        "temperature": temperature,
        "metadata": metadata,
    }
    if usage_sink is not None and _call_accepts_parameter(
        provider.structured_call, "usage_sink", default=False
    ):
        kwargs["usage_sink"] = usage_sink
    if _call_accepts_parameter(provider.structured_call, "provider_params", default=False):
        kwargs["provider_params"] = provider_params
    elif _non_legacy_provider_params(provider_params):
        raise TypeError(
            "provider.structured_call must accept provider_params when non-legacy "
            "provider params are configured"
        )
    if _call_accepts_artifacts(provider.structured_call):
        kwargs["artifacts"] = artifacts
    elif artifacts:
        raise TypeError(
            "provider.structured_call must accept artifacts when activity artifacts are resolved"
        )
    if observation_context and _call_accepts_parameter(
        provider.structured_call, "observation_context", default=False
    ):
        kwargs["observation_context"] = observation_context
    if cached_session is not None and _call_accepts_parameter(
        provider.structured_call, "cached_session", default=False
    ):
        kwargs["cached_session"] = cached_session
    return provider.structured_call(**kwargs)


async def _provider_structured_call_async(
    provider: AsyncModelProvider,
    *,
    messages: list[ChatMessage],
    output_schema: type[BaseModel],
    model: str | None,
    temperature: float | None,
    provider_params: ProviderParams,
    metadata: dict[str, Any],
    artifacts: tuple[ResolvedArtifactGroup, ...],
    observation_context: dict[str, Any],
    usage_sink: Callable[[ProviderUsage], None] | None = None,
    cached_session: CachedSessionHandle | None = None,
) -> BaseModel:
    kwargs: dict[str, Any] = {
        "messages": messages,
        "output_schema": output_schema,
        "model": model,
        "temperature": temperature,
        "metadata": metadata,
    }
    if usage_sink is not None and _call_accepts_parameter(
        provider.async_structured_call, "usage_sink", default=False
    ):
        kwargs["usage_sink"] = usage_sink
    if _call_accepts_parameter(provider.async_structured_call, "provider_params", default=False):
        kwargs["provider_params"] = provider_params
    elif _non_legacy_provider_params(provider_params):
        raise TypeError(
            "provider.async_structured_call must accept provider_params when non-legacy "
            "provider params are configured"
        )
    if _call_accepts_artifacts(provider.async_structured_call):
        kwargs["artifacts"] = artifacts
    elif artifacts:
        raise TypeError(
            "provider.async_structured_call must accept artifacts when activity artifacts are resolved"
        )
    if observation_context and _call_accepts_parameter(
        provider.async_structured_call, "observation_context", default=False
    ):
        kwargs["observation_context"] = observation_context
    if cached_session is not None and _call_accepts_parameter(
        provider.async_structured_call, "cached_session", default=False
    ):
        kwargs["cached_session"] = cached_session
    return await provider.async_structured_call(**kwargs)


def _call_accepts_artifacts(callable_obj: Any) -> bool:
    return _call_accepts_parameter(callable_obj, "artifacts", default=True)


def _call_accepts_parameter(callable_obj: Any, parameter_name: str, *, default: bool) -> bool:
    try:
        params = signature(callable_obj).parameters.values()
    except (TypeError, ValueError):
        return default
    for param in params:
        if param.kind is Parameter.VAR_KEYWORD:
            return True
        if param.name == parameter_name:
            return True
    return False


def _generation_observation_kwargs(
    activity_observation: ActivityObservation,
    *,
    messages: list[ChatMessage],
    output_schema: type[BaseModel],
    metadata: dict[str, Any],
    validation_attempt: int,
    model: str | None,
    temperature: float | None,
    provider_params: ProviderParams,
    observation_context: dict[str, Any],
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "messages": messages,
        "output_schema": output_schema,
        "metadata": metadata,
        "validation_attempt": validation_attempt,
        "model": model,
        "temperature": temperature,
    }
    if _call_accepts_parameter(
        activity_observation.observe_generation,
        "provider_params",
        default=False,
    ):
        kwargs["provider_params"] = provider_params
    if observation_context and _call_accepts_parameter(
        activity_observation.observe_generation,
        "observation_context",
        default=False,
    ):
        kwargs["observation_context"] = observation_context
    return kwargs


def _non_legacy_provider_params(provider_params: ProviderParams) -> bool:
    payload = provider_params.to_dict()
    return any(key not in {"model", "temperature"} for key in payload)


__all__ = [
    "PreparedAIActivityExecution",
    "execute_ai_activity",
    "execute_ai_activity_async",
    "execute_prepared_ai_activity",
    "execute_prepared_ai_activity_async",
    "prepare_ai_activity_execution",
]
