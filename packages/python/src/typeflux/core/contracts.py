from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from inspect import Parameter, Signature, signature
from typing import Any, Literal, TypeAlias, get_type_hints

from pydantic import BaseModel, ConfigDict

from typeflux.core.artifacts import (
    ArtifactInput,
    ChatContent,
    normalize_content_parts,
)

try:  # pragma: no cover - exercised when temporalio is installed.
    from temporalio.common import RetryPolicy
except ModuleNotFoundError:  # pragma: no cover - local fallback for core tests.

    @dataclass(frozen=True)
    class RetryPolicy:  # type: ignore[no-redef]
        """Placeholder used only when temporalio is not installed."""


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: ChatContent
    name: str | None = None

    def __post_init__(self) -> None:
        if self.role not in ("system", "user", "assistant"):
            raise ValueError("ChatMessage.role must be system, user, or assistant")
        object.__setattr__(self, "content", normalize_content_parts(self.content))
        if self.name is not None and not isinstance(self.name, str):
            raise TypeError("ChatMessage.name must be a string when provided")


@dataclass(frozen=True)
class PromptRef:
    """Reference to a registry prompt.

    ``version`` pins an immutable registry version (a Langfuse version
    number); ``label`` selects a mutable label such as ``production`` or
    ``canary``. They are mutually exclusive. With neither set, resolution
    uses the registry default label (``runtime.registry.label`` when
    configured, otherwise ``production``).
    """

    name: str
    version: int | None = None
    label: str | None = None
    prompt_type: Literal["auto", "text", "chat"] = "auto"

    def __post_init__(self) -> None:
        if self.prompt_type not in ("auto", "text", "chat"):
            raise ValueError("PromptRef.prompt_type must be auto, text, or chat")
        if self.version is not None and self.label is not None:
            raise ValueError(
                "PromptRef.version and PromptRef.label are mutually exclusive; "
                "pin an immutable registry version or select a mutable label, not both"
            )

    @property
    def selector(self) -> str:
        """Human-readable resolution selector, e.g. ``v7``, ``production``."""
        if self.version is not None:
            return f"v{self.version}"
        if self.label is not None:
            return self.label
        return "default-label"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "label": self.label}


@dataclass(frozen=True)
class ProviderParams:
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    top_k: int | None = None
    stop: tuple[str, ...] = ()
    seed: int | None = None
    timeout: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    thinking_budget: int | None = None

    def __post_init__(self) -> None:
        if self.model is not None and (not self.model or self.model.strip() != self.model):
            raise ValueError("provider params model must be non-empty and trimmed")
        if self.temperature is not None:
            object.__setattr__(self, "temperature", float(self.temperature))
        if self.top_p is not None:
            object.__setattr__(self, "top_p", float(self.top_p))
        if self.timeout is not None:
            object.__setattr__(self, "timeout", float(self.timeout))
        if self.frequency_penalty is not None:
            object.__setattr__(self, "frequency_penalty", float(self.frequency_penalty))
        if self.presence_penalty is not None:
            object.__setattr__(self, "presence_penalty", float(self.presence_penalty))
        object.__setattr__(self, "stop", tuple(self.stop))
        for stop in self.stop:
            if not isinstance(stop, str) or not stop or stop.strip() != stop:
                raise ValueError("provider params stop entries must be non-empty and trimmed")
        _validate_provider_params(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> ProviderParams:
        if value is None:
            return cls()
        allowed = set(cls.empty().to_dict(include_empty=True))
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"unknown provider param(s): {', '.join(unknown)}")
        payload = dict(value)
        stop = payload.get("stop")
        if isinstance(stop, str):
            payload["stop"] = (stop,)
        elif stop is not None:
            try:
                payload["stop"] = tuple(stop)
            except TypeError as exc:
                raise ValueError(
                    "provider params stop must be a string or sequence of strings"
                ) from exc
        return cls(**payload)

    @classmethod
    def empty(cls) -> ProviderParams:
        return cls()

    @property
    def configured_keys(self) -> tuple[str, ...]:
        return tuple(self.to_dict().keys())

    def merge(self, *overrides: ProviderParams | None) -> ProviderParams:
        merged = self.to_dict(include_empty=True)
        for override in overrides:
            if override is None:
                continue
            for key, value in override.to_dict(include_empty=True).items():
                if key == "stop":
                    if value:
                        merged[key] = tuple(value)
                    continue
                if value is not None:
                    merged[key] = value
        return ProviderParams.from_mapping(merged)

    def with_legacy(self, *, model: str | None, temperature: float | None) -> ProviderParams:
        updates: dict[str, Any] = {}
        if model is not None:
            updates["model"] = model
        if temperature is not None:
            updates["temperature"] = temperature
        return self.merge(ProviderParams.from_mapping(updates))

    def to_dict(
        self,
        *,
        include_empty: bool = False,
        include_operational: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "stop": list(self.stop),
            "seed": self.seed,
            "timeout": self.timeout,
            "frequency_penalty": self.frequency_penalty,
            "presence_penalty": self.presence_penalty,
            "thinking_budget": self.thinking_budget,
        }
        if not include_operational:
            payload.pop("timeout", None)
        if include_empty:
            return payload
        return {
            key: value
            for key, value in payload.items()
            if value is not None and (key != "stop" or value)
        }

    def behavior_dict(self, *, include_legacy: bool = False) -> dict[str, Any]:
        payload = self.to_dict(include_operational=False)
        if not include_legacy:
            payload.pop("model", None)
            payload.pop("temperature", None)
        return payload


def _validate_provider_params(params: ProviderParams) -> None:
    if params.max_tokens is not None and params.max_tokens < 1:
        raise ValueError("provider params max_tokens must be >= 1")
    if params.top_k is not None and params.top_k < 1:
        raise ValueError("provider params top_k must be >= 1")
    # thinking_budget == 0 is valid: it disables thinking (Gemini 2.5 flash).
    if params.thinking_budget is not None and params.thinking_budget < 0:
        raise ValueError("provider params thinking_budget must be >= 0")
    if params.timeout is not None and params.timeout <= 0:
        raise ValueError("provider params timeout must be > 0")
    if params.temperature is not None and not 0 <= params.temperature <= 2:
        raise ValueError("provider params temperature must be between 0 and 2")
    if params.top_p is not None and not 0 <= params.top_p <= 1:
        raise ValueError("provider params top_p must be between 0 and 1")
    for field_name in ("frequency_penalty", "presence_penalty"):
        value = getattr(params, field_name)
        if value is not None and not -2 <= value <= 2:
            raise ValueError(f"provider params {field_name} must be between -2 and 2")


@dataclass(frozen=True)
class ResolvedPrompt:
    ref: PromptRef
    messages: tuple[ChatMessage, ...]
    resolved_version: str | None = None
    model: str | None = None
    temperature: float | None = None
    provider_params: ProviderParams = field(default_factory=ProviderParams)
    metadata: dict[str, Any] = field(default_factory=dict)
    observation_context: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        provider_params = (
            self.provider_params
            if isinstance(self.provider_params, ProviderParams)
            else ProviderParams.from_mapping(self.provider_params)
        )
        params = provider_params.with_legacy(
            model=self.model,
            temperature=self.temperature,
        )
        object.__setattr__(self, "provider_params", params)
        object.__setattr__(self, "model", params.model)
        object.__setattr__(self, "temperature", params.temperature)


# Must stay defined BEFORE MapActivityContext: the workflow sandbox cannot
# lazily rebuild a pydantic model, so the cached_session annotation has to
# resolve at class-creation time, not on first validation (#371).
class CachedSessionHandle(BaseModel):
    """Reference to a provider-side cached prefix prepared once and reused across
    many small per-item calls that share a large stable context (#60).

    Serializable and manifest-safe: it carries only an **identity hash** of the
    stable cached content (system prompt + reference artifacts + model/params)
    plus the provider-native cache id — **never the cached content itself** — so
    it is safe to pass through Temporal history and to record in manifests and
    traces.

    ``supported=False`` is the **fail-soft fallback** handle, returned for
    providers without a session-cache capability (or when caching is disabled):
    callers still receive a handle and the workflow shape is identical, but each
    call sends the full context — today's behavior. Only cost/latency differ.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: ``provider_name`` of the provider that prepared (or declined) the session.
    provider: str
    #: Deterministic hash over the stable cached content; ties a per-item call to
    #: the prefix it reused and lets manifests/traces show cache identity safely.
    identity_hash: str
    #: True when caching is **engaged** for this session — i.e. the provider will
    #: cache the stable prefix. This does NOT imply a server-side cache *object*:
    #: prefix-style providers (Anthropic/OpenAI) cache implicitly with no object
    #: (``cache_id is None``), while reference-style (Gemini) sets ``cache_id``.
    #: False ⇒ fail-soft fallback (full context sent per call).
    supported: bool = False
    #: The provider's session-cache mechanism, carried on the handle so it stays
    #: self-describing in Temporal history/manifests: ``"reference"`` (a cache
    #: object referenced by ``cache_id``) or ``"prefix"`` (prefix re-sent + cached
    #: server-side). None for the fail-soft fallback.
    style: Literal["reference", "prefix"] | None = None
    #: Provider-native cache reference (e.g. a Gemini ``cachedContent`` name);
    #: None for fallbacks and for providers whose caching is implicit (OpenAI).
    cache_id: str | None = None
    model: str | None = None
    #: ISO-8601 creation time; set by the preparing activity (not at construction,
    #: so the value stays deterministic for replay).
    created_at: str | None = None
    ttl_seconds: int | None = None
    #: True when the prep step actually included the activity's ``cache: reference``
    #: artifacts in this cached session (#363). The per-item path uses THIS as the
    #: authoritative signal to drop those artifacts from per-item calls — never the
    #: provider style alone — so a supported reference handle that cached only the
    #: system prefix (e.g. the representative item was missing the artifact) does
    #: not cause the document to be silently dropped from every item.
    reference_cached: bool = False
    #: Prefix-style only (#362): the number of stable ``cache: reference`` artifact
    #: turns that lead the per-item conversation — inserted after the rendered
    #: system prefix and before the varying per-item turn. A positive int tells the
    #: prefix-style provider (Anthropic) to mark the cache breakpoint on
    #: ``conversation[prefix_stable_messages - 1]`` (the last stable turn), so the
    #: reference documents join the cached prefix instead of trailing the variable
    #: input. None (or 0) keeps the legacy ``conversation[-2]`` "stable instructions
    #: turn" contract byte-for-byte. Reference-style leaves this None — its
    #: artifacts live in the provider cache object, not the per-item conversation.
    #: A content-free count, so the handle stays manifest-/history-safe.
    prefix_stable_messages: int | None = None
    #: Prefix-style only (#698): True when the activity declares per-item (i.e. NOT
    #: ``cache: reference``) artifact attach turns that trail the varying per-item
    #: input — the shape ``[system, per-item query, per-item artifact attach]``.
    #: There the legacy ``conversation[-2]`` breakpoint would land on the varying
    #: query (a mis-mark: the last turn is the artifact, so ``-2`` is not a stable
    #: instructions turn), keying the conversation cache on content that differs per
    #: item. When this is True AND ``prefix_stable_messages`` is None/0 (no leading
    #: reference span to mark instead), the provider marks NOTHING in the
    #: conversation — the shape has no stable conversation span, only the system
    #: block is cached. Ignored when ``prefix_stable_messages`` > 0 (the reference
    #: span is the authoritative breakpoint). Reference-style and fail-soft handles
    #: leave it False. A content-free boolean, so the handle stays manifest-safe.
    per_item_artifact_messages: bool = False


class MapActivityContext(BaseModel):
    """The per-call activity context envelope (the AI activity wrapper's optional
    second positional argument).

    Historically map-only — hence the name — the envelope now carries every
    per-call fact the WORKFLOW knows and the activity cannot recover from
    ``activity.info()``. The map fan-out fields are set only when the call is a
    map item (a plain step's envelope leaves them ``None``); ``subject_ids``
    (#715 slice 1) is set whenever the run has subjects, map or not. A plain
    step with no subjects passes NO envelope at all, so subject-free histories
    are byte-identical to pre-#715 starts.
    """

    map_step_id: str | None = None
    map_index: int | None = None
    map_size: int | None = None
    map_concurrency: int | None = None
    #: Provider-side cached session prepared once before the fan-out and reused by
    #: every per-item call in this map step (#60). None ⇒ no caching for the step.
    #: Content-free + serializable, so it rides safely in Temporal history.
    cached_session: CachedSessionHandle | None = None
    #: Subject id(s) this execution processes (#715 slice 1), read by the parent
    #: workflow off its OWN ``TypefluxSubjectIds`` search attribute (deterministic
    #: in the sandbox) and threaded here so the activity-side invocation context —
    #: and through it the cross-run cache record — carries them.
    subject_ids: tuple[str, ...] = ()

    def to_metadata(self) -> dict[str, int | str]:
        return {
            key: value
            for key, value in {
                "map_step_id": self.map_step_id,
                "map_index": self.map_index,
                "map_size": self.map_size,
                "map_concurrency": self.map_concurrency,
            }.items()
            if value is not None
        }


class ReviewCommand(BaseModel):
    user_decision: str
    reviewer: str | None = None
    notes: str | None = None
    #: Which gate to decide (#55 slice 4). Absent + exactly one gate waiting ⇒ that gate
    #: (V1 clients work verbatim); absent with a single-gate workflow ⇒ that gate; absent +
    #: several waiting ⇒ recorded ``review_invalid_user_decision`` (ambiguous, never guessed).
    gate: str | None = None


class WaitingGate(BaseModel):
    """One currently-open review gate reported in the status wire (#55 §8).

    The execution's OWN view of an open gate and its valid decisions. The control
    plane prefers this execution-reported set over the resolved spec (§6 drift
    caveat): a running child pins the plan it started with, which an edited spec no
    longer reflects.
    """

    gate_id: str
    after_step: str
    valid_user_decisions: dict[str, str]


class WorkflowLifecycleEvent(BaseModel):
    sequence: int
    state: str
    event: str
    timestamp: str | None = None
    step_id: str | None = None
    completed_units: int = 0
    total_units: int = 0
    cancellation_requested: bool = False
    cancellation_reason: str | None = None
    waiting_checkpoint: str | None = None
    review_user_decision: str | None = None
    review_route_target: str | None = None
    terminal_status: str | None = None
    #: The rendered ``when:`` predicate of a ``step_skipped`` event (#55 §3.3) —
    #: carried ONLY by that event; None (and excluded from serialization) on every
    #: other event, so pre-composition event payloads are unchanged. The Python
    #: generator emits ``step_skipped`` in #55 slice 2; TS emits it today.
    condition: str | None = None
    #: The gate a review event belongs to (#55 slice 4). Set ONLY on multi-gate
    #: (``lifecycle.gates``) review events; None (excluded under exclude_none) for V1
    #: single-``review`` specs and every non-review event, so pre-slice-4 event streams are
    #: byte-identical at the control-plane wire (TS omits the field identically).
    gate_id: str | None = None
    #: The compensation outcome (#299 D299-2): ``none`` / ``complete`` / ``partial``. Set ONLY
    #: once the compensation unwind has run — on the terminal ``workflow_failed`` /
    #: ``workflow_cancelled`` events — and None (excluded under exclude_none) everywhere else, so
    #: non-compensating event streams stay byte-identical. Additive in binding.v1.json
    #: ``event_fields`` and the openapi WorkflowLifecycleEvent (client 1.9.0). TS omits identically.
    compensation_status: str | None = None


class WorkflowLifecycleStatus(BaseModel):
    state: str
    current_step: str | None = None
    completed_units: int = 0
    total_units: int = 0
    cancellation_requested: bool = False
    cancellation_reason: str | None = None
    waiting_checkpoint: str | None = None
    review_user_decision: str | None = None
    review_route_target: str | None = None
    terminal_status: str | None = None
    event_count: int = 0
    events_truncated: bool = False
    oldest_event_sequence: int | None = None
    latest_event_sequence: int | None = None
    events: tuple[WorkflowLifecycleEvent, ...] = ()
    #: The execution's currently-open gates (#55 §8), ordered by open time — ALWAYS present,
    #: ``()`` when none (the status-field convention, like ``events``). Carries per-gate
    #: ``valid_user_decisions`` so the control plane can prefer this execution-reported set over
    #: the resolved spec (§6). Additive in binding.v1.json workflow_lifecycle_status.fields.
    waiting_gates: tuple[WaitingGate, ...] = ()
    #: The terminal compensation outcome (#299 D299-2): ``none`` / ``complete`` / ``partial``, or
    #: None until the compensation LIFO has unwound (None for every non-failed/non-cancelled run,
    #: and for a run with no ``compensate:``-bearing completed steps). Additive in binding.v1.json
    #: workflow_lifecycle_status.fields and the openapi WorkflowLifecycleStatus (client 1.9.0).
    compensation_status: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "current_step": self.current_step,
            "completed_units": self.completed_units,
            "total_units": self.total_units,
            "cancellation_requested": self.cancellation_requested,
            "waiting_checkpoint": self.waiting_checkpoint,
            "review_user_decision": self.review_user_decision,
            "review_route_target": self.review_route_target,
            "terminal_status": self.terminal_status,
        }


@dataclass(frozen=True)
class ActivityDefinitionSource:
    kind: Literal["yaml", "python", "unknown"] = "unknown"
    module: str | None = None
    export: str | None = None
    yaml_project: str | None = None
    yaml_name: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("yaml", "python", "unknown"):
            raise ValueError("ActivityDefinitionSource.kind must be yaml, python, or unknown")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "kind": self.kind,
            "module": self.module,
            "export": self.export,
            "yaml_project": self.yaml_project,
            "yaml_name": self.yaml_name,
        }
        return {key: value for key, value in payload.items() if value is not None}


# A hook is (input, output) -> output or (input, output, ctx) -> output; the
# real arity is validated by _validate_hook_signature, so the alias is variadic.
HookFn: TypeAlias = Callable[..., BaseModel]


def _is_basemodel_type(value: object) -> bool:
    return isinstance(value, type) and issubclass(value, BaseModel)


def _resolved_type_hints(fn: Callable[..., Any]) -> dict[str, Any]:
    try:
        return get_type_hints(fn)
    except Exception:
        return getattr(fn, "__annotations__", {})


def _validate_hook_signature(
    hook: HookFn,
    *,
    input_type: type[BaseModel],
    output_type: type[BaseModel],
) -> bool:
    """Validate a hook signature. Returns ``True`` if the hook opts into an
    :class:`ActivityContext` via a 3rd positional parameter."""

    sig = signature(hook)
    params = list(sig.parameters.values())
    if len(params) not in (2, 3):
        raise TypeError("hook must accept (input, output) or (input, output, ctx)")

    for param in params:
        if param.kind not in (
            Parameter.POSITIONAL_ONLY,
            Parameter.POSITIONAL_OR_KEYWORD,
        ):
            raise TypeError("hook parameters must be positional")
        if param.default is not Signature.empty:
            raise TypeError("hook parameters must not define defaults")

    hints = _resolved_type_hints(hook)
    first_annotation = hints.get(params[0].name, params[0].annotation)
    second_annotation = hints.get(params[1].name, params[1].annotation)
    return_annotation = hints.get("return", sig.return_annotation)

    if first_annotation is Signature.empty or first_annotation is not input_type:
        raise TypeError("hook first parameter must be annotated with input_type")
    if second_annotation is Signature.empty or second_annotation is not output_type:
        raise TypeError("hook second parameter must be annotated with output_type")
    if return_annotation is Signature.empty or return_annotation is not output_type:
        raise TypeError("hook return must be annotated with output_type")

    # The optional 3rd parameter (ctx) need not be annotated.
    return len(params) == 3


@dataclass(frozen=True)
class OutputCheckViolation:
    """A single cross-field output-contract violation reported by an
    :data:`OutputCheck` (#745).

    ``message`` is shown to the model verbatim in the repair prompt, so it should
    describe what is wrong legibly; ``path`` (dotted into the output) is optional
    context. Author-written, never model output.
    """

    message: str
    path: tuple[str | int, ...] = ()


# An output check sees ``(input, output)`` and returns ``None`` / ``[]`` to accept,
# or a non-empty list of violations to REJECT (#745). A raised exception is also a
# rejection (the executor normalizes it to a single violation). Unlike a hook it is
# PRE-acceptance: on the provider-backed path a rejection feeds the SAME repair loop
# as a schema-parse miss (the model gets its ``validation_retries`` to self-correct),
# and an output-check-rejected output is never cached.
OutputCheck: TypeAlias = Callable[[BaseModel, BaseModel], "list[OutputCheckViolation] | None"]


def _validate_output_check_signature(
    output_check: OutputCheck,
    *,
    input_type: type[BaseModel],
    output_type: type[BaseModel],
) -> None:
    """Validate an output_check signature (#745): exactly two positional
    ``(input, output)`` parameters. Type annotations are optional (a lambda need
    not annotate), but when present the first must be ``input_type`` and the
    second ``output_type`` (or the generic ``BaseModel`` base, for a reusable
    check), mirroring the hook/moderator contracts."""

    sig = signature(output_check)
    params = list(sig.parameters.values())
    if len(params) != 2:
        raise TypeError("output_check must accept exactly (input, output)")
    for param in params:
        if param.kind not in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD):
            raise TypeError("output_check parameters must be positional")
        if param.default is not Signature.empty:
            raise TypeError("output_check parameters must not define defaults")
    hints = _resolved_type_hints(output_check)
    first = hints.get(params[0].name, params[0].annotation)
    second = hints.get(params[1].name, params[1].annotation)
    if first is not Signature.empty and first not in (input_type, BaseModel):
        raise TypeError(
            "output_check first parameter must be annotated with input_type or BaseModel"
        )
    if second is not Signature.empty and second not in (output_type, BaseModel):
        raise TypeError(
            "output_check second parameter must be annotated with output_type or BaseModel"
        )


@dataclass(frozen=True)
class ActivityContext:
    """Execution context passed to a context-aware hook (the optional 3rd
    parameter, ``hook(input, output, ctx)``).

    Carries Temporal facts, the activity name, a generic ``tenant`` map, and the
    worker-registered ``deps`` container. A hook uses ``deps`` for side-effecting
    work (DB writes, vector search, …) scoped by ``tenant`` — without a DB
    client or tenant id needing to live on the input model. Hooks run inside the
    Temporal activity (not the workflow), so I/O here is allowed.

    ``deps`` is the same worker-registered instance shared by every activity on
    the worker, and hooks run on a thread pool — so ``deps`` (and anything it
    hands out) must be thread-safe or create per-call resources.
    """

    activity_name: str
    namespace: str | None = None
    workflow_id: str | None = None
    run_id: str | None = None
    activity_id: str | None = None
    attempt: int | None = None
    task_queue: str | None = None
    tenant: Mapping[str, str] = field(default_factory=dict)
    deps: Any = None


class ModerationResult(BaseModel):
    """Verdict a moderator returns for an activity's validated output (#158).

    Carries only the *semantic classification* — never the raw output — so it is
    safe to record as audit evidence. ``flagged`` drives the checkpoint's action
    (block/flag); ``categories``/``max_score``/``detail`` describe why.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    flagged: bool
    categories: tuple[str, ...] = ()
    max_score: float | None = None
    detail: str | None = None


# A moderator inspects validated output and returns a ModerationResult. Output
# only in v1 (#158); the policy-governed thresholds/categories layer is #158 PR2.
Moderator: TypeAlias = Callable[[BaseModel], "ModerationResult"]


def _validate_moderator_signature(moderator: Moderator, *, output_type: type[BaseModel]) -> None:
    sig = signature(moderator)
    params = list(sig.parameters.values())
    if len(params) != 1:
        raise TypeError("moderator must accept exactly one positional parameter: output")
    # Reject *args/**kwargs/keyword-only and defaults, mirroring the hook contract.
    if params[0].kind not in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD):
        raise TypeError("moderator parameter must be positional")
    if params[0].default is not Signature.empty:
        raise TypeError("moderator parameter must not define a default")
    hints = _resolved_type_hints(moderator)
    first_annotation = hints.get(params[0].name, params[0].annotation)
    return_annotation = hints.get("return", sig.return_annotation)
    # Accept the exact output type or the generic ``BaseModel`` base, so a
    # provider-backed moderator that works for any output (#158 PR3) is reusable.
    if first_annotation is Signature.empty or first_annotation not in (output_type, BaseModel):
        raise TypeError("moderator parameter must be annotated with output_type or BaseModel")
    if return_annotation is Signature.empty or return_annotation is not ModerationResult:
        raise TypeError("moderator return must be annotated with ModerationResult")


@dataclass(frozen=True)
class ModerationConfig:
    """Opt-in moderation request on an activity (#158 PR1).

    Runs ``moderator`` on the activity's validated output (after any ``hook``).
    ``on_violation`` selects the action when the verdict is flagged: ``block``
    fails the activity (fail-closed, non-retryable), ``flag`` records the verdict
    and continues. Policy-governed mandatory moderation is #158 PR2.
    """

    moderator: Moderator
    on_violation: Literal["block", "flag"] = "block"


@dataclass(frozen=True)
class SessionCacheConfig:
    """Runtime view of an activity's opt-in provider session-cache request (#60).

    The YAML-layer ``SessionCacheSpec`` is mapped onto this core type so the
    activity contract carries the request without the core package depending on
    the YAML package. ``enabled=False`` is an explicit opt-out (no caching even
    where the provider supports it); ``ttl_seconds=None`` lets the provider use
    its default lifetime.
    """

    enabled: bool = True
    ttl_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.ttl_seconds is not None and self.ttl_seconds < 1:
            raise ValueError("ttl_seconds must be >= 1")


@dataclass(frozen=True)
class CacheConfig:
    """Opt-in cross-run cache for an AI activity's validated output (#398).

    Distinct from ``SessionCacheConfig`` (the provider-side prefix cache, #60):
    this memoizes the *validated LLM output* across runs in a worker-registered
    ``CacheStore``. ``enabled=False`` is an explicit opt-out. ``bypass_reads_env``
    names an environment variable whose presence skips cache *reads* (writes
    still happen), to force regeneration — mirroring the regenerate-flag
    pattern in adopter caches.
    """

    enabled: bool = True
    bypass_reads_env: str | None = None


@dataclass(frozen=True)
class AIActivity:
    name: str
    input_type: type[BaseModel]
    output_type: type[BaseModel]
    prompt_ref: PromptRef
    hook: HookFn | None = None
    validation_retries: int = 1
    task_queue: str | None = None
    start_to_close_timeout: timedelta | None = None
    heartbeat_timeout: timedelta | None = None
    retry_policy: RetryPolicy | None = None
    definition_source: ActivityDefinitionSource = field(default_factory=ActivityDefinitionSource)
    artifact_inputs: tuple[ArtifactInput, ...] = ()
    provider_params: ProviderParams = field(default_factory=ProviderParams)
    #: Opt-in provider session-cache request; None ⇒ caching not requested (#60).
    session_cache: SessionCacheConfig | None = None
    #: Opt-in semantic moderation of validated output; None ⇒ not requested (#158).
    moderation: ModerationConfig | None = None
    #: Opt-in cross-run cache of the validated output; None ⇒ not requested (#398).
    cache: CacheConfig | None = None
    #: Opt-in input-aware, pre-acceptance output check; None ⇒ not requested (#745).
    #: A rejection feeds the repair loop (the model gets its validation_retries),
    #: and a rejected output is never cached. Contrast the post-acceptance ``hook``.
    output_check: OutputCheck | None = None
    #: Set in __post_init__: True when the hook takes a 3rd ActivityContext param (#397).
    hook_wants_context: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("AIActivity.name must not be empty")
        if not _is_basemodel_type(self.input_type):
            raise TypeError("input_type must be a Pydantic BaseModel subclass")
        if not _is_basemodel_type(self.output_type):
            raise TypeError("output_type must be a Pydantic BaseModel subclass")
        if self.validation_retries < 0:
            raise ValueError("validation_retries must be >= 0")
        object.__setattr__(self, "artifact_inputs", tuple(self.artifact_inputs))
        if not isinstance(self.provider_params, ProviderParams):
            object.__setattr__(
                self,
                "provider_params",
                ProviderParams.from_mapping(self.provider_params),
            )
        artifact_names = [artifact.name for artifact in self.artifact_inputs]
        duplicates = sorted({name for name in artifact_names if artifact_names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate artifact input name(s): {', '.join(duplicates)}")
        if self.hook is not None:
            wants_context = _validate_hook_signature(
                self.hook,
                input_type=self.input_type,
                output_type=self.output_type,
            )
            object.__setattr__(self, "hook_wants_context", wants_context)
        if self.output_check is not None:
            _validate_output_check_signature(
                self.output_check,
                input_type=self.input_type,
                output_type=self.output_type,
            )
        if self.moderation is not None:
            _validate_moderator_signature(
                self.moderation.moderator,
                output_type=self.output_type,
            )


@dataclass(frozen=True)
class TemporalActivityDescriptor:
    """Workflow-contract view of a plain ``@temporalio.activity.defn`` callable.

    Plain Temporal activities participate in YAML workflow graphs with their
    own name, typed input/output contract, timeout, retry policy, and task
    queue — without the AI wrapper (no registry/provider coupling, no AI
    preflight, no output-repair loop, no AI observability identity). The
    decorated callable is registered with the Temporal worker as-is.
    """

    name: str
    input_type: type[BaseModel]
    output_type: type[BaseModel]
    activity: Callable[..., Any]
    task_queue: str | None = None
    start_to_close_timeout: timedelta | None = None
    heartbeat_timeout: timedelta | None = None
    retry_policy: RetryPolicy | None = None
    definition_source: ActivityDefinitionSource = field(default_factory=ActivityDefinitionSource)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("TemporalActivityDescriptor.name must not be empty")
        if not _is_basemodel_type(self.input_type):
            raise TypeError("input_type must be a Pydantic BaseModel subclass")
        if not _is_basemodel_type(self.output_type):
            raise TypeError("output_type must be a Pydantic BaseModel subclass")
        if not callable(self.activity):
            raise TypeError("activity must be callable")


#: Union of activity shapes a YAML workflow step can reference.
YamlWorkflowActivity = AIActivity | TemporalActivityDescriptor


__all__ = [
    "ActivityDefinitionSource",
    "AIActivity",
    "CacheConfig",
    "CachedSessionHandle",
    "ChatMessage",
    "HookFn",
    "MapActivityContext",
    "ModerationConfig",
    "OutputCheck",
    "OutputCheckViolation",
    "ProviderParams",
    "PromptRef",
    "ResolvedPrompt",
    "ReviewCommand",
    "RetryPolicy",
    "SessionCacheConfig",
    "TemporalActivityDescriptor",
    "WorkflowLifecycleEvent",
    "WorkflowLifecycleStatus",
    "YamlWorkflowActivity",
]
