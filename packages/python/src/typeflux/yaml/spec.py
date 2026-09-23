from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from typeflux.core.artifacts import (
    ArtifactAttachment,
    ArtifactGroupPart,
    ArtifactInput,
    ArtifactPart,
    ProviderExtensionPart,
    TextPart,
)
from typeflux.core.contracts import ChatMessage, ProviderParams
from typeflux.core.subjects import SubjectInput
from typeflux.yaml.overrides import YamlOverrideProvenance
from typeflux.yaml.payload_codec import PayloadCodecSpec
from typeflux.yaml.secrets import SecretValueSpec, secret_value_configured

RESERVED_WORKFLOW_CONTEXT_KEYS = {"input"}


DEFAULT_MAP_COLLECT_MAX_BYTES = 1_500_000

#: The parallel nesting ceiling (#55 decision D3): parallel-in-branch-of-parallel covers
#: every reviewed real shape; deeper nesting is a strong signal the inner block should be
#: a sub-workflow — which also restores operability (its own id, status, drain row).
MAX_PARALLEL_NESTING_DEPTH = 3

#: The operator keys of a ``when:`` leaf predicate, in normalization order (TS
#: ``WHEN_OPERATORS`` parity — the shared cross-edition predicate DSL, #55 §3.2).
WHEN_OPERATORS = ("eq", "neq", "lt", "lte", "gt", "gte", "in", "exists")

#: Risk-tier vocabulary (#300 D300-1): a FIXED, ordered, low-cardinality enum —
#: ``safe`` ⊂ ``policy_gated`` ⊂ ``human_gated`` ⊂ ``prohibited``, ascending by
#: strictness. Fixed (not free-form) so composition has a total order (floor merges
#: take the highest tier), cross-project ``extends`` chains share one vocabulary, and
#: the search tag stays 4-valued. The rank is the index into this tuple.
RISK_TIER_ORDER: tuple[str, ...] = ("safe", "policy_gated", "human_gated", "prohibited")

#: The declared risk tier of a workflow (#300). Optional at the spec level; an unset
#: tier reads as ``safe`` at evaluation time (parity with how optional governance
#: fields behave — the default is NOT materialized into the spec, so a spec without
#: ``risk_tier`` stays byte-identical everywhere, incl. the graph digest).
RiskTier = Literal["safe", "policy_gated", "human_gated", "prohibited"]


class TemporalTLSConfigSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_root_ca_cert_file: str | None = None
    server_root_ca_cert: SecretValueSpec | None = None
    domain: str | None = None
    client_cert_file: str | None = None
    client_cert: SecretValueSpec | None = None
    client_private_key_file: str | None = None
    client_private_key: SecretValueSpec | None = None

    @field_validator(
        "server_root_ca_cert_file",
        "domain",
        "client_cert_file",
        "client_private_key_file",
        mode="before",
    )
    @classmethod
    def _normalize_blank_strings(cls, value: str | None) -> str | None:
        if isinstance(value, str) and value == "":
            return None
        return value

    @model_validator(mode="after")
    def _validate_client_certificate_pair(self) -> TemporalTLSConfigSpec:
        if self.server_root_ca_cert_file is not None and self.server_root_ca_cert is not None:
            raise ValueError(
                "runtime.temporal.tls.server_root_ca_cert_file and "
                "runtime.temporal.tls.server_root_ca_cert cannot both be configured"
            )
        if self.client_cert_file is not None and self.client_cert is not None:
            raise ValueError(
                "runtime.temporal.tls.client_cert_file and "
                "runtime.temporal.tls.client_cert cannot both be configured"
            )
        if self.client_private_key_file is not None and self.client_private_key is not None:
            raise ValueError(
                "runtime.temporal.tls.client_private_key_file and "
                "runtime.temporal.tls.client_private_key cannot both be configured"
            )
        has_client_cert = self.client_cert_file is not None or self.client_cert is not None
        has_client_key = (
            self.client_private_key_file is not None or self.client_private_key is not None
        )
        if has_client_cert != has_client_key:
            raise ValueError(
                "runtime.temporal.tls.client_cert/client_cert_file and "
                "runtime.temporal.tls.client_private_key/client_private_key_file must be "
                "configured together"
            )
        return self


class TemporalSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str = "localhost:7233"
    namespace: str = "default"
    tls: bool | TemporalTLSConfigSpec = False
    api_key: str | SecretValueSpec | None = None
    # AES-256-GCM Temporal payload codec (#188). Absent = OFF (plaintext). When declared,
    # every referenced key MUST resolve at client-connect time or startup fails (D188-3).
    payload_codec: PayloadCodecSpec | None = None
    # Keyword search attribute that carries the logical workflow name on
    # every Typeflux-started execution, for one visibility query across
    # versioned workflow types. Must be registered in the namespace before
    # enabling; unset means no search attribute is attached.
    workflow_search_attribute: str | None = None

    @field_validator("api_key", mode="before")
    @classmethod
    def _normalize_api_key(cls, value: object) -> object:
        if isinstance(value, str) and value == "":
            return None
        return value

    @field_validator("workflow_search_attribute")
    @classmethod
    def _validate_workflow_search_attribute(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value):
            raise ValueError(
                "runtime.temporal.workflow_search_attribute must start with a letter "
                "and contain only alphanumerics or underscores"
            )
        return value

    @model_validator(mode="after")
    def _validate_api_key_tls(self) -> TemporalSpec:
        # A literal key with TLS disabled is statically wrong. A value_from
        # reference may legitimately be unconfigured in local profiles, so
        # references are checked at client connect time, when the resolved
        # value is known.
        if isinstance(self.api_key, str) and self.tls is False:
            raise ValueError("runtime.temporal.api_key requires runtime.temporal.tls to be enabled")
        return self


class InlinePromptTextPartSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"]
    text: str

    def to_part(self) -> TextPart:
        return TextPart(text=self.text)


class InlinePromptArtifactPartSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["artifact"]
    artifact: str
    text: str | None = None

    def to_part(self) -> ArtifactPart:
        return ArtifactPart(artifact=self.artifact, text=self.text)


class InlinePromptArtifactGroupPartSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["artifact_group"]
    group: str
    text: str | None = None

    def to_part(self) -> ArtifactGroupPart:
        return ArtifactGroupPart(group=self.group, text=self.text)


class InlinePromptProviderExtensionPartSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["provider_extension"]
    provider: str
    payload: dict[str, object]

    def to_part(self) -> ProviderExtensionPart:
        return ProviderExtensionPart(provider=self.provider, payload=self.payload)


InlinePromptContentPartSpec = (
    InlinePromptTextPartSpec
    | InlinePromptArtifactPartSpec
    | InlinePromptArtifactGroupPartSpec
    | InlinePromptProviderExtensionPartSpec
)


class InlinePromptMessageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str | list[InlinePromptContentPartSpec]
    name: str | None = None

    def to_chat_message(self) -> ChatMessage:
        content: (
            str
            | tuple[
                TextPart | ArtifactPart | ArtifactGroupPart | ProviderExtensionPart,
                ...,
            ]
        )
        if isinstance(self.content, str):
            content = self.content
        else:
            content = tuple(part.to_part() for part in self.content)
        return ChatMessage(role=self.role, content=content, name=self.name)


class ProviderParamsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    top_k: int | None = None
    stop: str | list[str] | None = None
    seed: int | None = None
    timeout: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    thinking_budget: int | None = None

    @field_validator("model")
    @classmethod
    def _validate_model(cls, value: str | None) -> str | None:
        if value is not None and (not value or value.strip() != value):
            raise ValueError("provider params model must be non-empty and trimmed")
        return value

    @field_validator("max_tokens", "top_k")
    @classmethod
    def _validate_positive_int(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("provider params integer values must be >= 1")
        return value

    @field_validator("thinking_budget")
    @classmethod
    def _validate_thinking_budget(cls, value: int | None) -> int | None:
        # 0 disables thinking (Gemini 2.5 flash); negative is invalid.
        if value is not None and value < 0:
            raise ValueError("provider params thinking_budget must be >= 0")
        return value

    @field_validator("temperature")
    @classmethod
    def _validate_temperature(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 2:
            raise ValueError("provider params temperature must be between 0 and 2")
        return value

    @field_validator("top_p")
    @classmethod
    def _validate_top_p(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 1:
            raise ValueError("provider params top_p must be between 0 and 1")
        return value

    @field_validator("timeout")
    @classmethod
    def _validate_timeout(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("provider params timeout must be > 0")
        return value

    @field_validator("frequency_penalty", "presence_penalty")
    @classmethod
    def _validate_penalty(cls, value: float | None) -> float | None:
        if value is not None and not -2 <= value <= 2:
            raise ValueError("provider params penalties must be between -2 and 2")
        return value

    @field_validator("stop")
    @classmethod
    def _validate_stop(cls, value: str | list[str] | None) -> str | list[str] | None:
        values = [value] if isinstance(value, str) else value
        if values is not None:
            for item in values:
                if not item or item.strip() != item:
                    raise ValueError("provider params stop entries must be non-empty and trimmed")
        return value

    def to_provider_params(
        self,
        *,
        legacy_model: str | None = None,
        legacy_temperature: float | None = None,
    ) -> ProviderParams:
        payload = self.model_dump(exclude_none=True)
        stop = payload.get("stop")
        if isinstance(stop, str):
            payload["stop"] = (stop,)
        elif stop is not None:
            payload["stop"] = tuple(stop)
        params = ProviderParams.from_mapping(payload)
        if legacy_model is not None and params.model is not None and params.model != legacy_model:
            raise ValueError("legacy model and provider_params.model must match")
        if (
            legacy_temperature is not None
            and params.temperature is not None
            and params.temperature != legacy_temperature
        ):
            raise ValueError("legacy temperature and provider_params.temperature must match")
        return params.with_legacy(model=legacy_model, temperature=legacy_temperature)

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> ProviderParamsSpec:
        return cls.model_validate(value)


class InlinePromptSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[InlinePromptMessageSpec]
    model: str | None = None
    temperature: float | None = None
    provider_params: ProviderParamsSpec | None = None

    @field_validator("messages")
    @classmethod
    def _validate_messages(
        cls, value: list[InlinePromptMessageSpec]
    ) -> list[InlinePromptMessageSpec]:
        if not value:
            raise ValueError("inline prompt messages must not be empty")
        return value

    @model_validator(mode="after")
    def _validate_provider_params_compatibility(self) -> InlinePromptSpec:
        if self.provider_params is not None:
            self.provider_params.to_provider_params(
                legacy_model=self.model,
                legacy_temperature=self.temperature,
            )
        return self


def _require_custom_class(*, type_value: str | None, class_value: str | None, kind: str) -> None:
    """The shared `type: custom` ⇔ `class:` rule for every extension point (#189)."""
    if type_value == "custom" and class_value is None:
        raise ValueError(f"runtime.{kind}.type 'custom' requires a 'class'")
    if class_value is not None and type_value != "custom":
        raise ValueError(
            f"runtime.{kind}.class is only supported with {kind} type 'custom'; "
            f"{kind} type {type_value!r} ignores custom {kind} classes"
        )


def _validate_custom_config(
    config: dict[str, Any] | None, *, type_value: str | None, kind: str
) -> None:
    """The custom-extension ``config`` contract (#792): a FLAT map declared only on
    ``type: custom``, each value a literal string or a typed ``value_from`` reference —
    so the extension's configuration is spec-visible and its secrets join the bundle's
    ``secret_references`` inventory instead of hiding in ad-hoc env reads."""
    if config is None:
        return
    if type_value != "custom":
        raise ValueError(f"runtime.{kind}.config is only valid for type: custom")
    for key, value in config.items():
        if not isinstance(key, str) or not key or key.strip() != key:
            raise ValueError(f"runtime.{kind}.config keys must be non-empty, trimmed strings")
        if not isinstance(value, (str, SecretValueSpec)):
            raise ValueError(
                f"runtime.{kind}.config[{key!r}] must be a string or a value_from reference"
            )
        if isinstance(value, str) and not value:
            # "" means unset everywhere in the secret-slot machinery (resolver, inventory,
            # configured-flag), so an explicit empty literal would silently vanish before
            # reaching the class — reject it instead of reading like working configuration.
            raise ValueError(
                f"runtime.{kind}.config[{key!r}] must not be an empty literal; drop the key "
                "or use a value_from reference with required: false"
            )


class RegistrySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["inline", "langfuse", "langsmith", "custom"]
    label: str | None = None
    host: str | None = None
    registry_class: str | None = Field(default=None, alias="class")
    #: Custom-extension configuration (#792): flat map, literal or value_from values,
    #: passed to the class as ``cls(config=resolved)``. `type: custom` only.
    config: dict[str, str | SecretValueSpec] | None = None
    prompts: dict[str, str | InlinePromptSpec] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_registry_class_type(self) -> RegistrySpec:
        _require_custom_class(
            type_value=self.type, class_value=self.registry_class, kind="registry"
        )
        _validate_custom_config(self.config, type_value=self.type, kind="registry")
        return self


#: Single source of truth for default provider models used when
#: ``runtime.provider.model`` is omitted. Materialized into the resolved spec so
#: the effective model is visible in the audited artifact and read identically by
#: admission, runtime, and activities.
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_PROVIDER_MODELS = {
    "openai": DEFAULT_OPENAI_MODEL,
    "anthropic": DEFAULT_ANTHROPIC_MODEL,
    "gemini": DEFAULT_GEMINI_MODEL,
}


class VertexSpec(BaseModel):
    """Vertex AI configuration for the Gemini provider (#332).

    The presence of a ``vertex:`` block selects Vertex AI (``genai.Client(
    vertexai=True, …)``) over the Developer-API key path. Vertex authenticates
    with Application Default Credentials, not an API key, so ``api_key`` is not
    required (or used) when this is set. ``project``/``location`` are optional;
    the SDK falls back to ``GOOGLE_CLOUD_PROJECT``/``GOOGLE_CLOUD_LOCATION`` when
    omitted.
    """

    model_config = ConfigDict(extra="forbid")

    project: str | None = None
    location: str | None = None


class ProviderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["fake", "openai", "anthropic", "gemini", "custom"]
    model: str | None = None
    structured_mode: Literal["json_schema"] = "json_schema"
    provider_class: str | None = Field(default=None, alias="class")
    api_key: str | SecretValueSpec | None = None
    base_url: str | None = None
    vertex: VertexSpec | None = None
    params: ProviderParamsSpec | None = None
    #: Custom-extension configuration (#792): flat map, literal or value_from values,
    #: passed to the class as ``cls(config=resolved)``. `type: custom` only.
    config: dict[str, str | SecretValueSpec] | None = None
    allow_prompt_model_override: bool = False

    @model_validator(mode="after")
    def _validate_vertex_provider(self) -> ProviderSpec:
        # Vertex config is Gemini-only; on any other provider it would be silently
        # ignored (reads like a working selection that isn't).
        if self.vertex is not None:
            if self.type != "gemini":
                raise ValueError("runtime.provider.vertex is only valid for the gemini provider")
            # Vertex authenticates with Application Default Credentials; a
            # configured api_key is meaningless there. Reject only an actually
            # configured key (a literal value or a value_from reference) — not an
            # empty string or an unconfigured ref, which layered YAML uses to
            # CLEAR a Developer-API key while switching to Vertex (Bugbot #332).
            if secret_value_configured(self.api_key):
                raise ValueError(
                    "runtime.provider.vertex (Vertex AI uses Application Default "
                    "Credentials) is mutually exclusive with runtime.provider.api_key"
                )
        return self

    @model_validator(mode="after")
    def _validate_provider_class_type(self) -> ProviderSpec:
        # `type: custom` is the one sanctioned extension path: it requires a
        # class, and a class requires it (a class on a built-in type would be
        # silently ignored, which reads like a working override).
        _require_custom_class(
            type_value=self.type, class_value=self.provider_class, kind="provider"
        )
        _validate_custom_config(self.config, type_value=self.type, kind="provider")
        return self

    @model_validator(mode="after")
    def _materialize_default_model(self) -> ProviderSpec:
        # Make the implicit default explicit in the resolved spec so the policy
        # layer never enforces against an invisible, separately-derived default.
        if self.params is not None and self.params.model is not None:
            if self.model is not None and self.model != self.params.model:
                raise ValueError(
                    "runtime.provider.model and runtime.provider.params.model must match"
                )
            self.model = self.params.model
        if self.model is None:
            self.model = DEFAULT_PROVIDER_MODELS.get(self.type)
        return self

    def provider_params(self) -> ProviderParams:
        if self.params is None:
            return ProviderParams(model=self.model)
        return self.params.to_provider_params(legacy_model=self.model)


class ProviderLimitSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrent: int | None = None
    min_interval_seconds: float | None = None

    @field_validator("max_concurrent")
    @classmethod
    def _validate_max_concurrent(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("max_concurrent must be >= 1")
        return value

    @field_validator("min_interval_seconds")
    @classmethod
    def _validate_min_interval_seconds(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("min_interval_seconds must be >= 0")
        return value


class ProviderLimitProviderSpec(ProviderLimitSpec):
    models: dict[str, ProviderLimitSpec] = Field(default_factory=dict)


class ProviderLimitsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: ProviderLimitSpec | None = None
    providers: dict[str, ProviderLimitProviderSpec] = Field(default_factory=dict)


class ProviderRetrySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = 1
    initial_backoff_seconds: float = 0.0
    max_backoff_seconds: float | None = None
    backoff_multiplier: float = 2.0
    jitter_ratio: float = 0.1
    retry_rate_limits: bool = True
    retry_transient_errors: bool = True

    @field_validator("jitter_ratio")
    @classmethod
    def _validate_jitter_ratio(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")
        return value

    @field_validator("max_attempts")
    @classmethod
    def _validate_max_attempts(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_attempts must be >= 1")
        return value

    @field_validator("initial_backoff_seconds")
    @classmethod
    def _validate_initial_backoff_seconds(cls, value: float) -> float:
        if value < 0:
            raise ValueError("initial_backoff_seconds must be >= 0")
        return value

    @field_validator("max_backoff_seconds")
    @classmethod
    def _validate_max_backoff_seconds(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("max_backoff_seconds must be >= 0")
        return value

    @field_validator("backoff_multiplier")
    @classmethod
    def _validate_backoff_multiplier(cls, value: float) -> float:
        if value < 1:
            raise ValueError("backoff_multiplier must be >= 1")
        return value


class ActivityRetrySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    maximum_attempts: int = 5
    initial_interval_seconds: float = 1.0
    maximum_interval_seconds: float | None = 60.0
    backoff_coefficient: float = 2.0

    @field_validator("maximum_attempts")
    @classmethod
    def _validate_maximum_attempts(cls, value: int) -> int:
        # 0 is Temporal's explicit unlimited-retries sentinel.
        if value < 0:
            raise ValueError("maximum_attempts must be >= 0 (0 means unlimited)")
        return value

    @field_validator("initial_interval_seconds")
    @classmethod
    def _validate_initial_interval_seconds(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("initial_interval_seconds must be > 0")
        return value

    @field_validator("maximum_interval_seconds")
    @classmethod
    def _validate_maximum_interval_seconds(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("maximum_interval_seconds must be > 0")
        return value

    @field_validator("backoff_coefficient")
    @classmethod
    def _validate_backoff_coefficient(cls, value: float) -> float:
        if value < 1:
            raise ValueError("backoff_coefficient must be >= 1")
        return value

    def to_retry_policy(self) -> Any:
        from datetime import timedelta

        from temporalio.common import RetryPolicy

        return RetryPolicy(
            maximum_attempts=self.maximum_attempts,
            initial_interval=timedelta(seconds=self.initial_interval_seconds),
            maximum_interval=(
                timedelta(seconds=self.maximum_interval_seconds)
                if self.maximum_interval_seconds is not None
                else None
            ),
            backoff_coefficient=self.backoff_coefficient,
        )


class RedactionRuleSpec(BaseModel):
    """A single custom redaction rule (#188 D188-4) appended to the built-in catalog.

    ``pattern`` is compiled with Python's ``re`` at load; an invalid regex fails startup
    fail-closed (a redaction control must never silently no-op). Patterns are the Python
    ``re`` dialect — the same YAML gives an edition-specific regex on the TS side, so a
    portable rule sticks to features common to both (see docs/privacy.md).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    pattern: str
    replacement: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        # Names must be non-empty AND trimmed: policy-side require_custom_rules matches names
        # by exact (trim-enforced) membership, so an untrimmed "case_reference " would load
        # here but then fail admission with a misleading missing-rule error. Trimming the
        # replacement/pattern is NOT enforced — leading/trailing spaces can be legitimate
        # there (e.g. a replacement of "  " to blank out matches).
        if not value or value.strip() != value:
            raise ValueError(
                "observability.redaction.custom_rules name must be non-empty and trimmed"
            )
        return value

    @field_validator("replacement")
    @classmethod
    def _validate_replacement(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("observability.redaction.custom_rules replacement must be non-empty")
        return value

    @field_validator("pattern")
    @classmethod
    def _validate_pattern(cls, value: str) -> str:
        if not value:
            raise ValueError("observability.redaction.custom_rules pattern must be non-empty")
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(
                f"observability.redaction.custom_rules pattern is not a valid regex: {exc}"
            ) from exc
        return value


class RedactionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    emails: bool = True
    phones: bool = True
    ssn: bool = True
    credit_cards: bool = True
    preserve_typeflux_metadata: bool = True
    exclude_paths: list[str] = Field(default_factory=list)
    custom_rules: list[RedactionRuleSpec] = Field(default_factory=list)

    @field_validator("custom_rules")
    @classmethod
    def _validate_custom_rule_names(cls, value: list[RedactionRuleSpec]) -> list[RedactionRuleSpec]:
        names = [rule.name for rule in value]
        if len(names) != len(set(names)):
            raise ValueError("observability.redaction.custom_rules names must be unique")
        return value


class ObservabilityLangfuseSpec(BaseModel):
    """Spec-declared Langfuse credentials (#793): the same ``string | value_from`` contract
    as every sibling secret slot. Unset fields fall back to the standard env vars
    (``LANGFUSE_PUBLIC_KEY``/``LANGFUSE_SECRET_KEY``/``LANGFUSE_HOST``), so declaring the
    block is additive — what it buys is inventory honesty (``bundle.secret_references``)
    and admission-checkable references."""

    model_config = ConfigDict(extra="forbid")

    public_key: str | SecretValueSpec | None = None
    secret_key: str | SecretValueSpec | None = None
    #: Plain string (env interpolation covers the env-var pattern): a host is not a
    #: credential, so it stays out of the secret inventory/scaffolding contract.
    host: str | None = None


class ObservabilityLangsmithSpec(BaseModel):
    """Spec-declared LangSmith credentials (#793); env fallback is
    ``LANGSMITH_API_KEY``/``LANGSMITH_ENDPOINT``/``LANGSMITH_PROJECT``."""

    model_config = ConfigDict(extra="forbid")

    api_key: str | SecretValueSpec | None = None
    #: Plain strings (env interpolation covers the env-var pattern): not credentials, so
    #: they stay out of the secret inventory/scaffolding contract.
    endpoint: str | None = None
    project: str | None = None


class ObservabilitySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["none", "langfuse", "langsmith", "custom"] | None = None
    backend_class: str | None = Field(default=None, alias="class")
    #: Custom-extension configuration (#792): flat map, literal or value_from values,
    #: passed to the class as ``cls(config=resolved)``. `type: custom` only.
    config: dict[str, str | SecretValueSpec] | None = None
    #: Spec-declared backend credentials (#793); each block is valid only with its own
    #: ``type`` — a credentials block on a different backend would be silently dead.
    langfuse: ObservabilityLangfuseSpec | None = None
    langsmith: ObservabilityLangsmithSpec | None = None
    execution_manifest: bool = True
    redaction: RedactionSpec = Field(default_factory=RedactionSpec)

    @model_validator(mode="after")
    def _validate_backend_class_type(self) -> ObservabilitySpec:
        _require_custom_class(
            type_value=self.type, class_value=self.backend_class, kind="observability"
        )
        _validate_custom_config(self.config, type_value=self.type, kind="observability")
        if self.langfuse is not None and self.type != "langfuse":
            raise ValueError(
                "runtime.observability.langfuse credentials are only valid with type: langfuse"
            )
        if self.langsmith is not None and self.type != "langsmith":
            raise ValueError(
                "runtime.observability.langsmith credentials are only valid with type: langsmith"
            )
        return self


def _default_artifact_sources() -> list[
    Literal["local_path", "url", "object_uri", "provider_file"]
]:
    return ["local_path"]


class ArtifactRuntimeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_roots: list[str] = Field(default_factory=list)
    allowed_sources: list[Literal["local_path", "url", "object_uri", "provider_file"]] = Field(
        default_factory=_default_artifact_sources
    )
    allowed_media_types: list[str] = Field(default_factory=list)
    max_bytes: int | None = None

    @field_validator("local_roots", "allowed_sources", "allowed_media_types")
    @classmethod
    def _validate_non_empty_strings(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item or item.strip() != item:
                raise ValueError("artifact runtime entries must be non-empty and trimmed")
        return value

    @field_validator("max_bytes")
    @classmethod
    def _validate_max_bytes(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("runtime.artifacts.max_bytes must be >= 0")
        return value


class ImportPolicySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_absolute_activity_modules: bool = False
    allow_provider_class: bool = False
    allow_registry_class: bool = False
    allow_observability_class: bool = False
    allow_moderator_callable: bool = False
    allowed_module_roots: list[str] = Field(default_factory=list)

    @field_validator("allowed_module_roots")
    @classmethod
    def _validate_allowed_module_roots(cls, value: list[str]) -> list[str]:
        for root in value:
            if not root or root.strip() != root:
                raise ValueError("allowed_module_roots entries must be non-empty module roots")
        return value


class RuntimeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    temporal: TemporalSpec = Field(default_factory=TemporalSpec)
    registry: RegistrySpec
    provider: ProviderSpec
    provider_limits: ProviderLimitsSpec | None = None
    provider_retry: ProviderRetrySpec | None = None
    activity_retry: ActivityRetrySpec | None = None
    imports: ImportPolicySpec = Field(default_factory=ImportPolicySpec)
    observability: ObservabilitySpec = Field(default_factory=ObservabilitySpec)
    artifacts: ArtifactRuntimeSpec = Field(default_factory=ArtifactRuntimeSpec)
    #: The declared cache-erasure REQUIREMENT (#795): ``targeted`` demands per-subject
    #: invalidation — wiring a store without ``SubjectErasableCacheStore`` fails runtime
    #: assembly (the TS runtime's injected ``cacheStore``; a deployment that wires no
    #: store satisfies the requirement vacuously), and the erase CLI's cache surface
    #: fails loudly instead of falling back. Absent ≡ ``any`` (wired-store dependent,
    #: the pre-#795 behavior); the bundle discloses whichever contract applies.
    cache_erasure: Literal["targeted", "any"] | None = None


class ActivityModuleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module: str
    absolute: bool = False
    include: list[str] | None = None
    exclude: list[str] = Field(default_factory=list)


class PromptRefSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: int | None = None
    label: str | None = None
    type: Literal["auto", "text", "chat"] = "auto"

    @field_validator("version", mode="before")
    @classmethod
    def _validate_version_is_registry_version(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.isdigit():
            raise ValueError(
                "prompt.version pins an immutable integer registry version; "
                f"use 'label: {value}' to select a mutable prompt label"
            )
        return value

    @model_validator(mode="after")
    def _validate_version_label_exclusive(self) -> PromptRefSpec:
        if self.version is not None and self.label is not None:
            raise ValueError(
                "prompt.version and prompt.label are mutually exclusive; "
                "pin an immutable registry version or select a mutable label, not both"
            )
        return self


class ArtifactAttachmentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"] = "user"
    text: str | None = None

    def to_artifact_attachment(self) -> ArtifactAttachment:
        return ArtifactAttachment(role=self.role, text=self.text)


class ArtifactInputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    from_path: str = Field(alias="from")
    required: bool = True
    kind: (
        Literal[
            "document",
            "image",
            "audio",
            "video",
            "data",
            "archive",
            "provider_file",
            "external_uri",
            "other",
        ]
        | None
    ) = None
    media_types: list[str] = Field(default_factory=list)
    max_count: int | None = None
    max_bytes: int | None = None
    attach: ArtifactAttachmentSpec | None = None
    #: ``reference`` marks this artifact as part of the session-cached stable
    #: prefix (#363): cached once and reused across map items, not re-sent per
    #: item. Requires the activity to opt into ``cache:`` and the artifact to be
    #: identical across items.
    cache: Literal["reference"] | None = None

    @field_validator("name", "from_path")
    @classmethod
    def _validate_required_strings(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("artifact input fields must be non-empty and trimmed")
        return value

    def to_artifact_input(self) -> ArtifactInput:
        return ArtifactInput(
            name=self.name,
            from_path=self.from_path,
            required=self.required,
            kind=self.kind,
            media_types=tuple(self.media_types),
            max_count=self.max_count,
            max_bytes=self.max_bytes,
            attach=(self.attach.to_artifact_attachment() if self.attach is not None else None),
            cache_role=self.cache,
        )


class SessionCacheSpec(BaseModel):
    """Opt-in provider-side session caching for an activity (#60).

    When the activity runs in a map/fan-out step, the runtime prepares the
    provider cache once over the **stable prefix** (system instructions +
    artifacts marked ``cache: reference``) and reuses it across items, sending
    only the per-item input. Fail-soft: providers without the capability ignore
    it and send the full context as today. ``ttl_seconds`` is honored only by
    reference-style providers (Gemini); prefix-style caches are provider-managed.

    Reference-artifact constraints (reference-style providers):

    - A ``cache: reference`` artifact must be **identical across all map items**
      (it is resolved once from a representative item). Differing artifacts would
      cache one item's content for all.
    - The cache is reaped by ``ttl_seconds`` (Gemini default 3600s); the runtime
      does not yet explicitly release it after the fan-out, so set ``ttl_seconds``
      to comfortably exceed the fan-out duration. If the cache expires mid-fan-out
      the affected item fails loudly (never silently drops the document); resilient
      mid-run recovery + explicit release are tracked follow-ups.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    ttl_seconds: int | None = None

    @field_validator("ttl_seconds")
    @classmethod
    def _validate_ttl_seconds(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("cache.ttl_seconds must be >= 1")
        return value


class ModerationSpec(BaseModel):
    """YAML surface for activity output moderation (#158).

    Declares the moderator that inspects the activity's validated output: either a
    built-in ``provider`` moderator (no Python needed) or a custom ``moderator``
    import path (``module:callable`` returning a Moderator; gated by
    ``runtime.imports.allow_moderator_callable``). Exactly one must be set.
    ``on_violation`` selects the action on a flagged verdict. Declaring moderation
    here lets admission verify a policy ``semantics.required`` before deploy.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai", "gemini"] | None = None
    moderator: str | None = None
    on_violation: Literal["block", "flag"] = "block"
    #: Optional override of the provider moderation model (provider moderators only).
    model: str | None = None

    @field_validator("moderator", "model")
    @classmethod
    def _validate_optional_strings(cls, value: str | None) -> str | None:
        if value is not None and (not value or value.strip() != value):
            raise ValueError("moderation string must be non-empty and unpadded")
        return value

    @model_validator(mode="after")
    def _validate_source(self) -> ModerationSpec:
        if (self.provider is None) == (self.moderator is None):
            raise ValueError("moderation requires exactly one of 'provider' or 'moderator'")
        if self.model is not None and self.provider is None:
            raise ValueError("moderation.model applies only to a built-in 'provider' moderator")
        return self


class ActivityDefinitionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    input: str
    output: str
    prompt: str | PromptRefSpec
    validation_retries: int = 1
    start_to_close_timeout_seconds: float | None = None
    heartbeat_timeout_seconds: float | None = None
    retry: ActivityRetrySpec | None = None
    artifacts: list[ArtifactInputSpec] = Field(default_factory=list)
    provider_params: ProviderParamsSpec | None = None
    cache: SessionCacheSpec | None = None
    moderation: ModerationSpec | None = None
    #: The author's declaration that this activity performs an EXTERNAL side effect
    #: (a downstream write, a notification, a charge — anything not undone by simply
    #: letting the workflow fail) (#299 D299-5). Same trust model as ``cache: reference``:
    #: an author assertion the runtime does not verify, consumed by governance — a
    #: ``risk_tiers.require_compensation`` tier demands every side-effecting activity STEP
    #: declare ``compensate:``. Pure GOVERNANCE metadata: it never enters the workflow
    #: digest (definitions are not digest inputs; steps carry only activity name +
    #: present-only ``compensate``), so marking an activity side-effecting leaves every
    #: registered workflow type byte-identical — exactly like ``moderation`` and
    #: ``risk_tier``.
    side_effecting: bool = False

    @field_validator("validation_retries")
    @classmethod
    def _validate_validation_retries(cls, value: int) -> int:
        if value < 0:
            raise ValueError("validation_retries must be >= 0")
        return value

    @field_validator("start_to_close_timeout_seconds")
    @classmethod
    def _validate_start_to_close_timeout_seconds(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("start_to_close_timeout_seconds must be > 0")
        return value

    @field_validator("heartbeat_timeout_seconds")
    @classmethod
    def _validate_heartbeat_timeout_seconds(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("heartbeat_timeout_seconds must be > 0")
        return value


class ActivitiesSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modules: list[ActivityModuleSpec] = Field(default_factory=list)
    definitions: list[ActivityDefinitionSpec] = Field(default_factory=list)

    @field_validator("modules", mode="before")
    @classmethod
    def _coerce_modules(cls, value):
        if not isinstance(value, list):
            raise TypeError("activities.modules must be a list")
        coerced = []
        for item in value:
            if isinstance(item, str):
                coerced.append({"module": item})
            else:
                coerced.append(item)
        return coerced

    @property
    def is_empty(self) -> bool:
        return not self.modules and not self.definitions


#: A JSON literal a ``when:`` predicate compares against (#55 §3.2). Pure data — no
#: user code runs in the workflow, so predicates stay deterministic, digest-stable,
#: and statically analyzable for admission (#298). ``bool`` precedes the numeric
#: types so a YAML ``true`` never coerces into ``1``.
WhenLiteral = bool | int | float | str | None

#: Ordering comparisons (``lt``/``lte``/``gt``/``gte``) only make sense on orderable
#: literals (TS ``whenOrderable``: number | string).
WhenOrderable = int | float | str


class WhenLeafSpec(BaseModel):
    """One leaf predicate of the ``when:`` DSL (#55 §3.2): ``{path, <op>: literal}``
    with exactly one operator. ``eq: null`` is a real comparison against null —
    operator presence is checked via ``model_fields_set``, so a null literal never
    reads as "operator absent" (TS parity: presence is ``!== undefined``)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    path: str
    eq: WhenLiteral = None
    neq: WhenLiteral = None
    lt: WhenOrderable | None = None
    lte: WhenOrderable | None = None
    gt: WhenOrderable | None = None
    gte: WhenOrderable | None = None
    in_: list[WhenLiteral] | None = Field(default=None, alias="in")
    exists: bool | None = None

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("when.path must be non-empty and trimmed")
        return value

    @field_validator("lt", "lte", "gt", "gte", mode="before")
    @classmethod
    def _reject_boolean_ordering(cls, value: object) -> object:
        # TS parity (whenOrderable = number | string): a boolean is not orderable —
        # Pydantic's lax int coercion would otherwise silently read `lt: true` as 1.
        if isinstance(value, bool):
            raise ValueError("ordering comparisons (lt/lte/gt/gte) require a number or string")
        return value

    def _present_operators(self) -> list[str]:
        present = []
        for op in WHEN_OPERATORS:
            field_name = "in_" if op == "in" else op
            if field_name not in self.model_fields_set:
                continue
            # `eq`/`neq` admit an explicit null literal; every other operator's
            # null is meaningless and reads as absent.
            if op not in ("eq", "neq") and getattr(self, field_name) is None:
                continue
            present.append(op)
        return present

    @model_validator(mode="after")
    def _validate_exactly_one_operator(self) -> WhenLeafSpec:
        if len(self._present_operators()) != 1:
            raise ValueError(
                "a when predicate requires exactly one operator "
                "(eq, neq, lt, lte, gt, gte, in, exists)"
            )
        return self

    def operator(self) -> tuple[str, Any]:
        """The single normalized ``(op, value)`` pair (validated present)."""
        op = self._present_operators()[0]
        value = getattr(self, "in_" if op == "in" else op)
        if isinstance(value, list):
            value = tuple(value)
        return op, value


class WhenAllSpec(BaseModel):
    """One ``all:`` composition level over leaf predicates (decision D1 — never nested:
    a deeper boolean tree in YAML is unreviewable and admission-opaque)."""

    model_config = ConfigDict(extra="forbid")

    all: list[WhenLeafSpec]

    @field_validator("all")
    @classmethod
    def _validate_non_empty(cls, value: list[WhenLeafSpec]) -> list[WhenLeafSpec]:
        if not value:
            raise ValueError("when.all must not be empty")
        return value


class WhenAnySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    any: list[WhenLeafSpec]

    @field_validator("any")
    @classmethod
    def _validate_non_empty(cls, value: list[WhenLeafSpec]) -> list[WhenLeafSpec]:
        if not value:
            raise ValueError("when.any must not be empty")
        return value


class WhenPredicateStubSpec(BaseModel):
    """Reserved forward-compatible syntax (#55 §3.2): named injected predicates stay
    deferred — they would move branch logic out of the reviewable YAML — so their
    presence points at the literal DSL instead of failing as an unknown key."""

    model_config = ConfigDict(extra="forbid")

    predicate: str

    @model_validator(mode="after")
    def _reject(self) -> WhenPredicateStubSpec:
        raise ValueError(
            "when.predicate (named injected predicates) is not supported — express the "
            "gate with the literal predicate DSL: {path, eq/neq/lt/lte/gt/gte/in/exists}, "
            "optionally one all:/any: level (#55)"
        )


#: A ``when:`` gate on a step or parallel branch (#55 §3.2): a leaf predicate, or ONE
#: ``all:``/``any:`` composition level over leaf predicates. Evaluated once, when the
#: gated step/branch is reached, against recorded context only.
WhenSpec = WhenLeafSpec | WhenAllSpec | WhenAnySpec | WhenPredicateStubSpec


class WorkflowCompensateSpec(BaseModel):
    """Compensation for a completed step (#299 D299-1): run a normally-declared activity to
    undo the step's side effect. ``activity`` is an ordinary catalog-validated activity name
    (an undeclared name is a load error); ``input_from`` is a context dot-path (DEFAULT = the
    compensated step's OWN output — for a map step, each completed item's own result); ``retry``
    overrides the default bounded activity retry for this compensation only. Available on
    activity / map / subworkflow steps and steps inside parallel branches (the shared step
    schema). ``compensate.workflow`` (a compensating child workflow) is deferred to v2."""

    model_config = ConfigDict(extra="forbid")

    activity: str
    input_from: str | None = None
    retry: ActivityRetrySpec | None = None


class WorkflowStepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    activity: str
    #: Skip this step AND the remainder of its enclosing sequence when false (#55 §3.3).
    when: WhenSpec | None = None
    #: Compensation for this completed step (#299 D299-1).
    compensate: WorkflowCompensateSpec | None = None


class WorkflowMapCollectSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output: str
    field: str
    # Guard against Temporal's ~2MB per-payload limit with an actionable
    # error; 0 disables the check.
    max_bytes: int = DEFAULT_MAP_COLLECT_MAX_BYTES

    @field_validator("max_bytes")
    @classmethod
    def _validate_max_bytes(cls, value: int) -> int:
        if value < 0:
            raise ValueError("map.collect.max_bytes must be >= 0 (0 disables the guard)")
        return value


class WorkflowMapSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Fan an AI activity over the items (V1). Mutually exclusive with ``workflow``.
    activity: str | None = None
    #: Fan a sibling PROJECT workflow (by manifest workflow id) as a CHILD workflow
    #: per item (#55 §3.4). Mutually exclusive with ``activity``; resolvable only when
    #: the spec loads through its project (a standalone spec rejects at graph build).
    workflow: str | None = None
    over: str
    concurrency: int
    collect: WorkflowMapCollectSpec

    @field_validator("concurrency")
    @classmethod
    def _validate_concurrency(cls, value: int) -> int:
        if value < 1:
            raise ValueError("map.concurrency must be >= 1")
        return value

    @model_validator(mode="after")
    def _validate_target(self) -> WorkflowMapSpec:
        if (self.activity is None) == (self.workflow is None):
            raise ValueError("a map step requires exactly one of `activity` or `workflow` (#55)")
        return self


class WorkflowMapStepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    map: WorkflowMapSpec
    #: Skip this step AND the remainder of its enclosing sequence when false (#55 §3.3).
    when: WhenSpec | None = None
    #: Compensation per completed item, reverse item order (#299 D299-1).
    compensate: WorkflowCompensateSpec | None = None


class WorkflowParallelCollectSpec(BaseModel):
    """The ``collect`` of a parallel block (#55 §3.1) — the map collect shape minus
    ``field``: the merged object's fields ARE the branch ids (decision D4), so only
    the output schema ref and the payload bound are declared. ``max_bytes`` semantics
    are exactly the map's (#495): absent defaults the guard ON at 1.5MB, an explicit
    0 disables it."""

    model_config = ConfigDict(extra="forbid")

    output: str
    max_bytes: int = DEFAULT_MAP_COLLECT_MAX_BYTES

    @field_validator("max_bytes")
    @classmethod
    def _validate_max_bytes(cls, value: int) -> int:
        if value < 0:
            raise ValueError("collect.max_bytes must be >= 0 (0 disables the guard)")
        return value


class WorkflowSubworkflowStepSpec(BaseModel):
    """A ``workflow:`` step (#55 §3.4): run a sibling PROJECT workflow — referenced by
    its manifest workflow id — as a Temporal CHILD workflow. Resolvable only when the
    spec loads through its project; a standalone spec rejects at graph build."""

    model_config = ConfigDict(extra="forbid")

    id: str
    workflow: str
    #: Skip this step AND the remainder of its enclosing sequence when false (#55 §3.3).
    when: WhenSpec | None = None
    #: Parent-side compensation for having invoked the child (#299 D299-1/D299-3).
    compensate: WorkflowCompensateSpec | None = None


class WorkflowParallelBranchSpec(BaseModel):
    """One branch of a parallel block (#55 §3.1): a nested sequence of steps (any
    kinds, including nested ``parallel`` up to the depth ceiling), optionally gated by
    ``when`` (a gated-out branch contributes None to its collect field). Branch ids
    share the workflow's single flat id namespace."""

    model_config = ConfigDict(extra="forbid")

    id: str
    when: WhenSpec | None = None
    steps: list[
        WorkflowStepSpec
        | WorkflowMapStepSpec
        | WorkflowSubworkflowStepSpec
        | WorkflowParallelStepSpec
    ]

    @field_validator("steps")
    @classmethod
    def _validate_non_empty(cls, value: list[Any]) -> list[Any]:
        if not value:
            raise ValueError("parallel branch steps must not be empty")
        return value


class WorkflowParallelSpec(BaseModel):
    """A ``parallel:`` block (#55 §3.1): heterogeneous branches running concurrently,
    merged into a typed collect object keyed by branch id. The merge-field/Optional
    typing rule is checked against resolved Pydantic types in ``create_workflow``."""

    model_config = ConfigDict(extra="forbid")

    branches: list[WorkflowParallelBranchSpec]
    collect: WorkflowParallelCollectSpec

    @field_validator("branches")
    @classmethod
    def _validate_non_empty(
        cls, value: list[WorkflowParallelBranchSpec]
    ) -> list[WorkflowParallelBranchSpec]:
        if not value:
            raise ValueError("parallel.branches must not be empty")
        return value


class WorkflowParallelStepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    parallel: WorkflowParallelSpec
    #: Skip this step AND the remainder of its enclosing sequence when false (#55 §3.3).
    when: WhenSpec | None = None


#: A workflow step is exactly one of ``activity`` / ``map`` / ``workflow`` / ``parallel``
#: (the union plus ``extra="forbid"`` enforces it), optionally gated by ``when`` (#55).
WorkflowAnyStepSpec = (
    WorkflowStepSpec | WorkflowMapStepSpec | WorkflowSubworkflowStepSpec | WorkflowParallelStepSpec
)

# The step union is recursive through parallel branches; resolve the forward
# reference now that every participant exists.
WorkflowParallelBranchSpec.model_rebuild()


class WorkflowLifecycleReviewRouteSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: str

    @field_validator("route")
    @classmethod
    def _validate_route(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("review route must be non-empty")
        return value


class WorkflowLifecycleReviewTimeoutSpec(BaseModel):
    """Bounded waiting for a review gate (#297).

    When a human decision doesn't arrive within ``seconds``, the workflow applies
    ``on_timeout``: ``fail`` (fail-closed, the default), ``cancel`` (clean
    cancellation), or ``route`` to a named step (``route`` required — reuses the
    user_decisions routing + graph validation). The timer is a replay-safe
    Temporal durable timer.
    """

    model_config = ConfigDict(extra="forbid")

    seconds: int
    on_timeout: Literal["fail", "cancel", "route"] = "fail"
    route: str | None = None

    @field_validator("seconds")
    @classmethod
    def _validate_seconds(cls, value: int) -> int:
        if value < 1:
            raise ValueError("review timeout seconds must be >= 1")
        return value

    @model_validator(mode="after")
    def _validate_route(self) -> WorkflowLifecycleReviewTimeoutSpec:
        if self.on_timeout == "route":
            if not self.route or self.route.strip() != self.route:
                raise ValueError("review timeout on_timeout: route requires a non-empty route")
        elif self.route is not None:
            raise ValueError("review timeout route is only valid with on_timeout: route")
        return self


class WorkflowLifecycleReviewSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    after_step: str
    user_decisions: dict[str, WorkflowLifecycleReviewRouteSpec]
    invalid_user_decision: Literal["warn", "fail"] = "warn"
    timeout: WorkflowLifecycleReviewTimeoutSpec | None = None

    @field_validator("user_decisions")
    @classmethod
    def _validate_user_decisions(
        cls,
        value: dict[str, WorkflowLifecycleReviewRouteSpec],
    ) -> dict[str, WorkflowLifecycleReviewRouteSpec]:
        if not value:
            raise ValueError("review user_decisions must not be empty")
        for user_decision in value:
            if not user_decision or user_decision.strip() != user_decision:
                raise ValueError("review user_decisions entries must be non-empty")
        return value


class WorkflowLifecycleGateSpec(BaseModel):
    """One named review gate (#55 §8): the multi-gate generalization of ``review``.

    Same fields as ``WorkflowLifecycleReviewSpec`` plus a required ``id``; the
    singular ``review`` stays as sugar for one gate named ``"review"``. Routes are
    validated forward-only per gate in ``WorkflowSpec._validate_steps`` and typed in
    ``workflow.py`` ``_validate_review_routes`` — the §3.3 routed-tail checks per gate.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    after_step: str
    user_decisions: dict[str, WorkflowLifecycleReviewRouteSpec]
    invalid_user_decision: Literal["warn", "fail"] = "warn"
    timeout: WorkflowLifecycleReviewTimeoutSpec | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("gate id must be non-empty")
        return value

    @field_validator("user_decisions")
    @classmethod
    def _validate_user_decisions(
        cls,
        value: dict[str, WorkflowLifecycleReviewRouteSpec],
    ) -> dict[str, WorkflowLifecycleReviewRouteSpec]:
        if not value:
            raise ValueError("gate user_decisions must not be empty")
        for user_decision in value:
            if not user_decision or user_decision.strip() != user_decision:
                raise ValueError("gate user_decisions entries must be non-empty")
        return value


class WorkflowLifecycleHistorySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status_event_limit: int = 50

    @field_validator("status_event_limit")
    @classmethod
    def _validate_status_event_limit(cls, value: int) -> int:
        if value < 0:
            raise ValueError("workflow.lifecycle.history.status_event_limit must be >= 0")
        return value


class WorkflowLifecycleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    progress: bool = True
    cancellation: bool = True
    history: WorkflowLifecycleHistorySpec = Field(default_factory=WorkflowLifecycleHistorySpec)
    #: A workflow declares EITHER ``review`` (one gate, the V1 form — digest-stable) OR
    #: ``gates`` (a list of named gates, #55 slice 4), never both.
    review: WorkflowLifecycleReviewSpec | None = None
    gates: list[WorkflowLifecycleGateSpec] | None = None

    @model_validator(mode="after")
    def _validate_review_or_gates(self) -> WorkflowLifecycleSpec:
        if self.review is not None and self.gates is not None:
            raise ValueError(
                "workflow.lifecycle: use either 'review' (a single gate) or 'gates' "
                "(multiple named gates), not both"
            )
        if self.gates is not None:
            if not self.gates:
                raise ValueError("workflow.lifecycle.gates must not be empty")
            seen_ids: set[str] = set()
            seen_after: set[str] = set()
            # Decision DS4-6: a decision NAME reused across gates must route to the SAME
            # target (mirrors the activity-name collision rule) — the control plane's flat
            # valid_user_decisions fallback unions gates' decisions, so a divergent reuse
            # would advertise a route the runtime may route differently. Identical reuse
            # stays allowed, keeping the flat union well-defined.
            decision_routes: dict[str, tuple[str, str]] = {}
            for gate in self.gates:
                if gate.id in seen_ids:
                    raise ValueError(f"workflow.lifecycle.gates: duplicate gate id {gate.id!r}")
                seen_ids.add(gate.id)
                # Distinct after_step per gate (decision DS4-1): guarantees at most one gate
                # waits at a time in a v1 sequence, so routing is never ambiguous.
                if gate.after_step in seen_after:
                    raise ValueError(
                        f"workflow.lifecycle.gates: gate {gate.id!r} shares after_step "
                        f"{gate.after_step!r} with an earlier gate — each gate must follow a "
                        "distinct step (combine decisions into one gate for a single checkpoint)"
                    )
                seen_after.add(gate.after_step)
                for decision, route in gate.user_decisions.items():
                    prior = decision_routes.get(decision)
                    if prior is not None and prior[1] != route.route:
                        raise ValueError(
                            f"workflow.lifecycle.gates: decision {decision!r} routes to "
                            f"{prior[1]!r} on gate {prior[0]!r} but to {route.route!r} on "
                            f"gate {gate.id!r} — a decision name reused across gates must "
                            "keep identical route semantics (rename one decision, DS4-6)"
                        )
                    if prior is None:
                        decision_routes[decision] = (gate.id, route.route)
        return self

    def resolved_gates(self) -> list[WorkflowLifecycleGateSpec]:
        """The lifecycle's gates as a flat list — the single ``review`` normalizes to one
        gate named ``"review"`` (#55 §8 sugar), so runtime/validation share one code path."""
        if self.gates is not None:
            return list(self.gates)
        if self.review is not None:
            return [
                WorkflowLifecycleGateSpec(
                    id="review",
                    after_step=self.review.after_step,
                    user_decisions=self.review.user_decisions,
                    invalid_user_decision=self.review.invalid_user_decision,
                    timeout=self.review.timeout,
                )
            ]
        return []


class SubjectInputSpec(BaseModel):
    """A declarative subject selector on the workflow (#715 slice 1).

    Mirrors an activity ``artifacts:`` input's ``from:`` shape — ``from`` is a
    dotted ``input.<path>`` selector pulled off the validated workflow input at
    submit time to yield the subject id(s) this execution processes. The
    extracted ids stamp the ``TypefluxSubjectIds`` keyword-list search attribute
    (the subject->execution index) and fan to the observer / cache carriers.
    Multi-value: the selector may resolve to a list (a review packet spans
    several subjects). ``required`` (default) makes a missing selection a hard
    error at start — an un-indexed execution is invisible to erasure.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_path: str = Field(alias="from")
    required: bool = True

    @field_validator("from_path")
    @classmethod
    def _validate_from_path(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("subject input from must be non-empty and trimmed")
        if not value.startswith("input."):
            raise ValueError("subject input from must start with 'input.'")
        return value

    def to_subject_input(self) -> SubjectInput:
        return SubjectInput(from_path=self.from_path, required=self.required)


class WorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str | None = None
    input: str
    output: str
    steps: list[WorkflowAnyStepSpec]
    lifecycle: WorkflowLifecycleSpec | None = None
    #: The workflow's DECLARED risk tier (#300 D300-2). Optional — unset reads as
    #: ``safe`` at evaluation time (never materialized here, so it is pure governance
    #: metadata with no control-flow effect and stays OUT of ``workflow_spec_digest``).
    #: The project policy's ``risk_tiers`` dimension DEFINES what each tier requires.
    risk_tier: RiskTier | None = None
    #: Declarative subject selectors (#715 slice 1). Each pulls subject id(s) off
    #: the workflow input at submit time; the union stamps ``TypefluxSubjectIds``.
    #: Submit-time metadata with no control-flow effect, so — like ``risk_tier`` —
    #: it stays OUT of ``workflow_spec_digest`` (the same YAML through the same
    #: generator is the same program regardless of which input path holds the id).
    subjects: list[SubjectInputSpec] = Field(default_factory=list)

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
            raise ValueError(
                "workflow.version must start with an alphanumeric character and "
                "contain only alphanumerics, '.', '_', or '-'"
            )
        return value

    @model_validator(mode="after")
    def _validate_steps(self) -> WorkflowSpec:
        if not self.steps:
            raise ValueError("workflow.steps must contain at least one step")
        # Step and branch ids share the workflow's single FLAT id namespace (#55
        # §3.1): a step writes its result to ``context[step.id]``, which later
        # ``over``/``when`` paths read, and branch ids become the collect object's
        # field names — so duplicates or the reserved ``input`` id anywhere in the
        # tree would silently shadow another value.
        seen: set[str] = set()
        duplicates: set[str] = set()
        reserved: set[str] = set()

        def register(step_id: str) -> None:
            if step_id in seen:
                duplicates.add(step_id)
            if step_id in RESERVED_WORKFLOW_CONTEXT_KEYS:
                reserved.add(step_id)
            seen.add(step_id)

        def walk(steps: list[WorkflowAnyStepSpec], parallel_depth: int) -> None:
            for step in steps:
                register(step.id)
                if isinstance(step, WorkflowParallelStepSpec):
                    depth = parallel_depth + 1
                    if depth > MAX_PARALLEL_NESTING_DEPTH:
                        # Decision D3: the ceiling error names the way out.
                        raise ValueError(
                            f"workflow step {step.id!r} nests parallel blocks deeper than "
                            f"{MAX_PARALLEL_NESTING_DEPTH} — extract the inner block into a "
                            "sub-workflow instead (#55 slice 3): a sub-workflow restores "
                            "per-block operability (its own id, status, and drain row) that "
                            "deep nesting loses"
                        )
                    for branch in step.parallel.branches:
                        register(branch.id)
                        walk(branch.steps, depth)

        walk(self.steps, 0)
        if duplicates:
            raise ValueError(f"duplicate workflow step id(s): {', '.join(sorted(duplicates))}")
        if reserved:
            raise ValueError(f"reserved workflow step id(s): {', '.join(sorted(reserved))}")
        # Review gate targets are TOP-LEVEL step ids only (#55 §8: a parallel block is
        # one step; routing into the middle of a branch is not expressible).
        step_order = {step.id: index for index, step in enumerate(self.steps)}
        top_level = set(step_order)
        seen = top_level
        if self.lifecycle is not None:
            # The single `review` keeps its exact V1 error prose (byte-stable messages); named
            # `gates` use a gate-scoped label. Both share the same forward-only route discipline.
            single_review = self.lifecycle.review is not None
            for gate in self.lifecycle.resolved_gates():
                label = (
                    "workflow.lifecycle.review"
                    if single_review
                    else f"workflow.lifecycle.gates[{gate.id!r}]"
                )
                after_step = gate.after_step
                if after_step not in seen:
                    raise ValueError(f"{label}.after_step references unknown step: {after_step}")
                after_index = step_order[after_step]
                for user_decision, route in gate.user_decisions.items():
                    route_target = route.route
                    if route_target not in seen:
                        raise ValueError(
                            f"{label}.user_decisions "
                            f"{user_decision!r} routes to unknown step: {route_target}"
                        )
                    if step_order[route_target] <= after_index:
                        raise ValueError(
                            f"{label}.user_decisions "
                            f"{user_decision!r} must route to a step after {after_step}: "
                            f"{route_target}"
                        )
                timeout = gate.timeout
                if (
                    timeout is not None
                    and timeout.on_timeout == "route"
                    and timeout.route is not None
                ):
                    if timeout.route not in seen:
                        raise ValueError(f"{label}.timeout routes to unknown step: {timeout.route}")
                    if step_order[timeout.route] <= after_index:
                        raise ValueError(
                            f"{label}.timeout must route to a step after "
                            f"{after_step}: {timeout.route}"
                        )
        return self


class TypefluxYamlSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    _override_provenance: YamlOverrideProvenance | None = PrivateAttr(default=None)
    #: Safe component-profile provenance payloads (kind/id/name/hash/...),
    #: attached by project resolution (#214/#215).
    _component_provenance: tuple[dict[str, Any], ...] = PrivateAttr(default=())
    _source_path: Path | None = PrivateAttr(default=None)
    #: Admission provenance (an ``AdmissionReport``), attached ONLY when a spec entered
    #: via ``admit_spec`` (#298 D298-4). Never set on an operator filesystem flow, so
    #: its manifest carries no admission metadata (byte-unchanged).
    _admission_provenance: Any = PrivateAttr(default=None)

    project: str
    name: str
    task_queue: str
    runtime: RuntimeSpec
    activities: ActivitiesSpec
    workflow: WorkflowSpec

    @model_validator(mode="after")
    def _validate_activity_sources(self) -> TypefluxYamlSpec:
        # A workflow that calls an activity (an ``activity`` step or a ``map`` with an
        # ``activity`` target) must declare activities. A workflow composed ENTIRELY of
        # sub-workflow steps (#55 §3.4) legitimately declares none — its work lives in
        # the children — so the requirement is workflow-shaped, not unconditional.
        if self.activities.is_empty and _references_activity(self.workflow.steps):
            raise ValueError("activities must define modules, definitions, or both")
        return self

    @model_validator(mode="after")
    def _validate_provider_param_support(self) -> TypefluxYamlSpec:
        # #789 (audit B2; TS parity — the TS spec rejects at load via
        # unsupportedProviderParamKeys): a configured behavior param the selected
        # provider never maps would be silently ignored at runtime — reject it at
        # LOAD, so `project validate` (CI's own offline gate), admit, run, and
        # submit all refuse the same spec the worker preflight would. Unknown
        # provider types (`custom`/`fake`) skip — their provider is injected and
        # declares its own capability at build time.
        import typeflux.providers  # noqa: F401 - registration side effects
        from typeflux.providers.base import supported_provider_params

        provider_type = self.runtime.provider.type
        supported = supported_provider_params(provider_type)
        if supported is None:
            return self

        def _reject(params: ProviderParamsSpec | None, at: str) -> None:
            if params is None:
                return
            configured = set(params.model_dump(exclude_none=True))
            unsupported = sorted(configured - supported)
            if unsupported:
                raise ValueError(
                    f"provider {provider_type!r} does not support provider param(s) "
                    f"at {at}: {', '.join(unsupported)}"
                )

        _reject(self.runtime.provider.params, "$.runtime.provider.params")
        prompts = getattr(self.runtime.registry, "prompts", None) or {}
        for name, prompt in prompts.items():
            prompt_params = getattr(prompt, "provider_params", None)
            _reject(prompt_params, f"$.runtime.registry.prompts.{name}.provider_params")
        for index, definition in enumerate(self.activities.definitions):
            _reject(
                definition.provider_params,
                f"$.activities.definitions[{index}].provider_params",
            )
        return self


def _references_activity(steps: list[WorkflowAnyStepSpec]) -> bool:
    for step in steps:
        if isinstance(step, WorkflowStepSpec):
            return True
        if isinstance(step, WorkflowMapStepSpec) and step.map.activity is not None:
            return True
        if isinstance(step, WorkflowParallelStepSpec) and _references_activity(
            [nested for branch in step.parallel.branches for nested in branch.steps]
        ):
            return True
    return False
