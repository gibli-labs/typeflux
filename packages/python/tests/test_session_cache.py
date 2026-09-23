"""Provider session-cache foundation (#60, phase 1): the handle contract, the
opt-in capability probe, and the fail-soft fallback."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from typeflux.core.contracts import CachedSessionHandle
from typeflux.providers.base import (
    no_session_cache_handle,
    supports_session_cache,
)


class _Caching:
    provider_name = "demo"
    supports_session_cache = True


class _NonCaching:
    provider_name = "plain"


def test_supports_session_cache_is_opt_in() -> None:
    # Default False: a provider that doesn't declare the capability keeps today's
    # behavior (full context per call).
    assert supports_session_cache(_NonCaching()) is False
    assert supports_session_cache(None) is False
    assert supports_session_cache(_Caching()) is True


def test_no_session_cache_handle_is_a_fail_soft_fallback() -> None:
    handle = no_session_cache_handle(_NonCaching(), identity_hash="abc123", model="m")

    assert handle.supported is False
    assert handle.provider == "plain"
    assert handle.identity_hash == "abc123"
    assert handle.model == "m"
    assert handle.cache_id is None


def test_no_session_cache_handle_defaults_unknown_provider() -> None:
    assert no_session_cache_handle(None, identity_hash="h").provider == "unknown"


def test_handle_is_frozen_serializable_and_content_free() -> None:
    handle = CachedSessionHandle(
        provider="gemini",
        identity_hash="deadbeef",
        supported=True,
        cache_id="cachedContents/xyz",
        model="gemini-2.5-flash",
        created_at="2026-06-18T00:00:00Z",
        ttl_seconds=300,
    )

    # Frozen: a handle threaded through Temporal history must not be mutated.
    with pytest.raises(ValidationError):
        handle.model = "other"  # type: ignore[misc]

    # Round-trips for serialization into manifests / Temporal payloads.
    assert CachedSessionHandle.model_validate(handle.model_dump()) == handle

    # Manifest-safe: only identity + reference fields, never cached content.
    assert set(handle.model_dump()) == {
        "provider",
        "identity_hash",
        "supported",
        "style",
        "cache_id",
        "model",
        "created_at",
        "ttl_seconds",
        "reference_cached",
        "prefix_stable_messages",
        "per_item_artifact_messages",
    }


def test_handle_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CachedSessionHandle(provider="p", identity_hash="h", cached_content="secret")  # type: ignore[call-arg]


# --- Phase 5 foundations: spec + identity hash ------------------------------


class _Out(BaseModel):
    label: str


def test_session_cache_spec_defaults_and_ttl_validation() -> None:
    from typeflux.yaml.spec import ActivityDefinitionSpec, SessionCacheSpec

    assert SessionCacheSpec().enabled is True
    assert SessionCacheSpec().ttl_seconds is None
    assert SessionCacheSpec(ttl_seconds=300).ttl_seconds == 300
    with pytest.raises(ValueError, match="ttl_seconds must be >= 1"):
        SessionCacheSpec(ttl_seconds=0)

    # Opt-in on an activity definition.
    activity = ActivityDefinitionSpec(
        name="a",
        input="schemas:In",
        output="schemas:Out",
        prompt="p",
        cache={"enabled": True, "ttl_seconds": 300},
    )
    assert activity.cache is not None and activity.cache.ttl_seconds == 300
    # Absent by default (opt-in).
    assert ActivityDefinitionSpec(name="b", input="i", output="o", prompt="p").cache is None


def test_reference_artifact_requires_attach() -> None:
    # #363 review (Bugbot): a cache: reference artifact with no attach rule is
    # never presented to the model (not cached at prep, skipped per item), so it
    # would silently vanish. Reject it at construction.
    from typeflux.core.artifacts import ArtifactAttachment, ArtifactInput

    with pytest.raises(ValueError, match="reference artifact must declare how it attaches"):
        ArtifactInput(name="contract", from_path="input.contract", cache_role="reference")
    ok = ArtifactInput(
        name="contract",
        from_path="input.contract",
        cache_role="reference",
        attach=ArtifactAttachment(role="user", text="Contract:"),
    )
    assert ok.cache_role == "reference"


def test_artifact_input_cache_role_maps_from_spec() -> None:
    # #363: `cache: reference` on an artifact input marks it as the stable cached
    # prefix; it threads through to_artifact_input into the runtime ArtifactInput.
    from typeflux.yaml.spec import ArtifactInputSpec

    ref = ArtifactInputSpec(
        name="contract",
        **{"from": "input.contract"},
        cache="reference",
        attach={"role": "user", "text": "Contract:"},
    )
    assert ref.to_artifact_input().cache_role == "reference"
    # Default: ordinary per-item artifact.
    plain = ArtifactInputSpec(name="q", **{"from": "input.q"})
    assert plain.to_artifact_input().cache_role is None
    # Only "reference" is a valid value.
    with pytest.raises(ValueError):
        ArtifactInputSpec(name="x", **{"from": "input.x"}, cache="bogus")


def test_session_cache_identity_is_deterministic_and_sensitive() -> None:
    from typeflux.core.contracts import ChatMessage, ProviderParams
    from typeflux.execution.session_cache import session_cache_identity

    base: dict = dict(
        provider_name="gemini",
        model="gemini-2.5-flash",
        provider_params=ProviderParams(temperature=0),
        system_messages=[ChatMessage(role="system", content="Be precise.")],
        output_schema=_Out,
    )
    h = session_cache_identity(**base)
    assert len(h) == 64
    assert session_cache_identity(**base) == h  # deterministic

    # Sensitive to each part of the stable prefix.
    assert session_cache_identity(**{**base, "model": "other"}) != h
    assert (
        session_cache_identity(
            **{**base, "system_messages": [ChatMessage(role="system", content="Different.")]}
        )
        != h
    )
    assert session_cache_identity(**{**base, "provider_profile": {"project": "p"}}) != h
    assert session_cache_identity(**{**base, "provider_params": ProviderParams(temperature=1)}) != h

    class _Other(BaseModel):
        x: int

    assert session_cache_identity(**{**base, "output_schema": _Other}) != h


def test_session_cache_identity_cross_sdk_pinned() -> None:
    """The TS SDK pins these exact constants for the same inputs
    (temporal/test/session-cache.test.ts): the identity recipes are
    byte-identical across SDKs for aligned shapes, so either side changing its
    recipe breaks its own suite loudly (#478 PR2)."""

    from typeflux.core.artifacts import (
        ArtifactGroupPart,
        ArtifactRef,
        ResolvedArtifact,
        ResolvedArtifactGroup,
        TextPart,
    )
    from typeflux.core.contracts import ChatMessage
    from typeflux.execution.session_cache import session_cache_identity

    group = ResolvedArtifactGroup(
        name="docs",
        artifacts=(
            ResolvedArtifact(
                group="docs",
                index=0,
                ref=ArtifactRef(source={"type": "local_path", "path": "/data/contract.pdf"}),
                source_kind="local_path",
                kind="document",
                media_type="application/pdf",
                role=None,
                sha256="d" * 64,
                size_bytes=2048,
            ),
        ),
    )
    shared: dict = dict(
        provider_name="anthropic",
        provider_profile={"region": "us-east5"},
        model="claude-x",
        reference_artifacts=(group,),
    )
    assert (
        session_cache_identity(
            **shared,
            system_messages=[ChatMessage(role="system", content="You are a careful reviewer.")],
        )
        == "9f5f392619d4266fb0da95f94857853464af62ea99f3555726b73cb8ea3293df"
    )
    assert (
        session_cache_identity(
            **shared,
            system_messages=[
                ChatMessage(
                    role="system",
                    content=(TextPart("Follow the rubric."), ArtifactGroupPart(group="docs")),
                )
            ],
        )
        == "9b3c06e45c73f44ca65cac469638df94f50da484aa3d94ebcd651a4b1f9a5436"
    )


def test_assert_stable_system_prefix_rejects_template_variables() -> None:
    from typeflux.core.contracts import ChatMessage
    from typeflux.execution.session_cache import (
        UnstableCachePrefixError,
        assert_stable_system_prefix,
    )

    # Stable: no per-item interpolation.
    assert_stable_system_prefix([ChatMessage(role="system", content="You are precise.")])

    # Unstable: a {{var}} in the system prefix would vary per item.
    with pytest.raises(UnstableCachePrefixError, match="per-item template variable"):
        assert_stable_system_prefix(
            [ChatMessage(role="system", content="You handle {{ engagement_id }}.")]
        )

    # Unstable also when the template hides in an ArtifactGroupPart/ArtifactPart
    # text field (render_content_parts templates those too) — not just TextPart.
    from typeflux.core.artifacts import ArtifactGroupPart, TextPart

    with pytest.raises(UnstableCachePrefixError):
        assert_stable_system_prefix(
            [
                ChatMessage(
                    role="system",
                    content=(
                        TextPart("Stable instructions."),
                        ArtifactGroupPart(group="doc", text="for item {{ id }}"),
                    ),
                )
            ]
        )


class _PrefixCachingProvider:
    """Minimal prefix-style caching provider for prepare_session_cache tests."""

    provider_name = "demo"
    supports_session_cache = True

    def __init__(self) -> None:
        from typeflux.core.contracts import ProviderParams

        self.default_provider_params = ProviderParams(model="demo-model")
        self.prepared: list[dict] = []

    def prepare_cached_session(self, **kwargs):
        from typeflux.core.contracts import CachedSessionHandle

        self.prepared.append(kwargs)
        return CachedSessionHandle(
            provider="demo",
            identity_hash=kwargs["identity_hash"],
            supported=True,
            style="prefix",
            model=kwargs.get("model"),
            ttl_seconds=kwargs.get("ttl_seconds"),
        )


class _NonCachingProvider:
    provider_name = "plain"

    def __init__(self) -> None:
        from typeflux.core.contracts import ProviderParams

        self.default_provider_params = ProviderParams(model="plain-model")


def _cache_activity(system: str = "You extract citations from the document.", **cache):
    from typeflux.core.contracts import AIActivity, PromptRef, SessionCacheConfig

    activity = AIActivity(
        name="extract",
        input_type=_In,
        output_type=_Out,
        prompt_ref=PromptRef("extract"),
        session_cache=SessionCacheConfig(**cache) if cache else SessionCacheConfig(),
    )
    return activity, system


class _In(BaseModel):
    query: str


def _registry_with_system(system: str):
    from typeflux.core.contracts import ChatMessage, PromptRef, ResolvedPrompt
    from typeflux.prompts import InlinePromptRegistry

    return InlinePromptRegistry(
        {
            "extract": ResolvedPrompt(
                ref=PromptRef("extract"),
                messages=(
                    ChatMessage(role="system", content=system),
                    ChatMessage(role="user", content="Find: {{ query }}"),
                ),
                resolved_version="v1",
                model="demo-model",
                temperature=0,
                metadata={},
            )
        }
    )


def test_prepare_session_cache_returns_supported_handle_with_stamped_created_at() -> None:
    from typeflux.execution.executor import prepare_session_cache

    activity, system = _cache_activity(ttl_seconds=300)
    provider = _PrefixCachingProvider()
    handle = prepare_session_cache(
        activity=activity,
        registry=_registry_with_system(system),
        provider=provider,
        created_at="2026-06-19T00:00:00Z",
    )

    assert handle.supported is True
    assert handle.style == "prefix"
    assert handle.created_at == "2026-06-19T00:00:00Z"  # stamped by caller, not provider
    assert handle.ttl_seconds == 300
    # Only the system message is offered as the stable prefix (not the user turn).
    assert len(provider.prepared) == 1
    offered = provider.prepared[0]["messages"]
    assert [m.role for m in offered] == ["system"]


class _ReferenceCachingProvider:
    """A reference-style caching provider (like Gemini) for partition tests."""

    provider_name = "ref-demo"
    supports_session_cache = True
    session_cache_style = "reference"

    def __init__(self) -> None:
        from typeflux.core.contracts import ProviderParams

        self.default_provider_params = ProviderParams(model="ref-model")
        self.prepared: list[dict] = []

    def prepare_cached_session(self, **kwargs):
        from typeflux.core.contracts import CachedSessionHandle

        self.prepared.append(kwargs)
        return CachedSessionHandle(
            provider="ref-demo",
            identity_hash=kwargs["identity_hash"],
            supported=True,
            style="reference",
            cache_id="cachedContents/x",
            model=kwargs.get("model"),
            ttl_seconds=kwargs.get("ttl_seconds"),
        )


def _ref_activity():
    from typeflux.core.artifacts import ArtifactAttachment, ArtifactInput
    from typeflux.core.contracts import AIActivity, PromptRef, SessionCacheConfig

    return AIActivity(
        name="extract",
        input_type=_In,
        output_type=_Out,
        prompt_ref=PromptRef("extract"),
        session_cache=SessionCacheConfig(),
        artifact_inputs=(
            ArtifactInput(
                name="contract",
                from_path="input.query",  # mocked resolve; path is irrelevant here
                cache_role="reference",
                attach=ArtifactAttachment(role="user", text="Contract:"),
            ),
            ArtifactInput(name="extra", from_path="input.query"),
        ),
    )


def test_prep_resolves_reference_artifacts_for_reference_style(monkeypatch) -> None:
    # #363: a reference-style provider gets the cache: reference artifacts in its
    # prepared prefix, and they participate in the identity hash.
    from typeflux.core.artifacts import (
        ArtifactRef,
        ArtifactSource,
        ResolvedArtifact,
        ResolvedArtifactGroup,
    )
    from typeflux.execution import executor as ex

    captured: dict = {}

    def fake_resolve(input_value, artifact_inputs, *, policy=None):
        captured["inputs"] = [ai.name for ai in artifact_inputs]
        # Return a real (non-empty) group so reference_cached reflects a true cache.
        return tuple(
            ResolvedArtifactGroup(
                name=ai.name,
                artifacts=(
                    ResolvedArtifact(
                        group=ai.name,
                        index=0,
                        ref=ArtifactRef(source=ArtifactSource(type="local_path", path="x.pdf")),
                        source_kind="local_path",
                        kind="document",
                        media_type="application/pdf",
                        role=None,
                        sha256=None,
                        size_bytes=1,
                    ),
                ),
            )
            for ai in artifact_inputs
        )

    monkeypatch.setattr(ex, "resolve_artifact_inputs", fake_resolve)

    provider = _ReferenceCachingProvider()
    handle = ex.prepare_session_cache(
        activity=_ref_activity(),
        registry=_registry_with_system("You extract citations."),
        provider=provider,
        created_at="t",
        input_value=_In(query="q"),
    )
    assert handle.supported is True and handle.style == "reference"
    assert handle.reference_cached is True  # artifacts actually cached
    # Only the reference-role artifact is resolved for the cached prefix.
    assert captured["inputs"] == ["contract"]
    assert len(provider.prepared) == 1


def test_prep_reference_cached_false_when_artifact_missing(monkeypatch) -> None:
    # #363 review: a supported reference handle whose reference artifacts resolved
    # empty (e.g. representative item missing the optional doc) must NOT claim
    # reference_cached — otherwise per-item would silently drop the document.
    from typeflux.execution import executor as ex

    monkeypatch.setattr(ex, "resolve_artifact_inputs", lambda *a, **k: ())  # empty

    handle = ex.prepare_session_cache(
        activity=_ref_activity(),
        registry=_registry_with_system("You extract citations."),
        provider=_ReferenceCachingProvider(),
        created_at="t",
        input_value=_In(query="q"),
    )
    # System prefix still cached, but no reference artifact was → flag stays False.
    assert handle.supported is True
    assert handle.reference_cached is False


def test_prep_reference_cached_false_when_provider_declines(monkeypatch) -> None:
    # #516: a provider may decline POLITELY — return a fail-soft handle
    # (supported=False) from prepare_cached_session without raising. The per-item
    # skip gates on reference_cached ALONE, so flipping the flag for an unengaged
    # handle would drop documents that were never cached (TS PR #515 parity).
    from typeflux.core.artifacts import (
        ArtifactRef,
        ArtifactSource,
        ResolvedArtifact,
        ResolvedArtifactGroup,
    )
    from typeflux.execution import executor as ex
    from typeflux.providers.base import no_session_cache_handle

    def fake_resolve(input_value, artifact_inputs, *, policy=None):
        # Real (non-empty) reference groups — the decline alone must keep the flag off.
        return tuple(
            ResolvedArtifactGroup(
                name=ai.name,
                artifacts=(
                    ResolvedArtifact(
                        group=ai.name,
                        index=0,
                        ref=ArtifactRef(source=ArtifactSource(type="local_path", path="x.pdf")),
                        source_kind="local_path",
                        kind="document",
                        media_type="application/pdf",
                        role=None,
                        sha256=None,
                        size_bytes=1,
                    ),
                ),
            )
            for ai in artifact_inputs
        )

    monkeypatch.setattr(ex, "resolve_artifact_inputs", fake_resolve)

    class _DecliningProvider(_ReferenceCachingProvider):
        def prepare_cached_session(self, **kwargs):
            self.prepared.append(kwargs)
            return no_session_cache_handle(
                self, identity_hash=kwargs["identity_hash"], model=kwargs.get("model")
            )

    handle = ex.prepare_session_cache(
        activity=_ref_activity(),
        registry=_registry_with_system("You extract citations."),
        provider=_DecliningProvider(),
        created_at="t",
        input_value=_In(query="q"),
    )
    assert handle.supported is False
    assert handle.reference_cached is False


def test_per_item_skips_reference_artifacts_only_on_reference_hit(monkeypatch) -> None:
    # #363 partition: the per-item path drops cache: reference artifacts on a
    # reference-style hit (they live in the cache), but keeps them for prefix
    # style and for a fail-soft (uncached) handle — those need full context.
    from typeflux.core.contracts import CachedSessionHandle
    from typeflux.execution import executor as ex
    from typeflux.testing import FakeProvider

    seen: list[list[str]] = []

    def fake_resolve(input_value, artifact_inputs, *, policy=None):
        seen.append([ai.name for ai in artifact_inputs])
        return ()

    monkeypatch.setattr(ex, "resolve_artifact_inputs", fake_resolve)

    def run(cached_session):
        ex.prepare_ai_activity_execution(
            activity=_ref_activity(),
            input_value=_In(query="q"),
            registry=_registry_with_system("You extract citations."),
            provider=FakeProvider([]),
            cached_session=cached_session,
        )

    run(
        CachedSessionHandle(
            provider="ref-demo",
            identity_hash="h",
            supported=True,
            style="reference",
            reference_cached=True,
        )
    )
    run(
        CachedSessionHandle(
            provider="ref-demo",
            identity_hash="h",
            supported=True,
            style="reference",
            reference_cached=False,
        )
    )
    run(CachedSessionHandle(provider="d", identity_hash="h", supported=True, style="prefix"))
    run(CachedSessionHandle(provider="d", identity_hash="h", supported=False))
    run(None)

    assert seen[0] == ["extra"]  # reference artifacts cached → contract excluded
    assert seen[1] == [
        "contract",
        "extra",
    ]  # reference style, nothing cached → kept (no silent drop)
    assert seen[2] == ["contract", "extra"]  # prefix → kept (re-sent + breakpoint)
    assert seen[3] == ["contract", "extra"]  # fail-soft → kept (full context)
    assert seen[4] == ["contract", "extra"]  # no caching → kept


def test_prepare_session_cache_fails_soft_for_noncaching_provider() -> None:
    from typeflux.execution.executor import prepare_session_cache

    activity, system = _cache_activity()
    handle = prepare_session_cache(
        activity=activity,
        registry=_registry_with_system(system),
        provider=_NonCachingProvider(),
        created_at="t",
    )
    assert handle.supported is False
    assert handle.provider == "plain"


def test_prepare_session_cache_disabled_config_is_fail_soft() -> None:
    from typeflux.execution.executor import prepare_session_cache

    activity, system = _cache_activity(enabled=False)
    provider = _PrefixCachingProvider()
    handle = prepare_session_cache(
        activity=activity,
        registry=_registry_with_system(system),
        provider=provider,
        created_at="t",
    )
    assert handle.supported is False
    assert provider.prepared == []  # never asked the provider to prepare


def test_repair_message_role_is_user_only_for_reference_hit() -> None:
    # #368c: a reference-style cache hit serves the cached prefix and ignores
    # per-call system content, so the validation-repair message must be a user
    # turn to reach the model; everything else keeps system.
    from typeflux.core.contracts import CachedSessionHandle
    from typeflux.execution.executor import _repair_message_role

    ref_hit = CachedSessionHandle(
        provider="g", identity_hash="h", supported=True, style="reference"
    )
    assert _repair_message_role(ref_hit) == "user"
    assert (
        _repair_message_role(
            CachedSessionHandle(provider="a", identity_hash="h", supported=True, style="prefix")
        )
        == "system"
    )
    assert (
        _repair_message_role(CachedSessionHandle(provider="x", identity_hash="h", supported=False))
        == "system"
    )
    assert _repair_message_role(None) == "system"


def test_prepare_session_cache_fails_soft_when_provider_prep_errors() -> None:
    # #60: a provider that claims the capability but errors at prepare time (e.g.
    # Gemini free-tier explicit-cache cap, transient API error) must degrade to
    # uncached, never fail the map step.
    from typeflux.execution.executor import prepare_session_cache

    class _Boom(_PrefixCachingProvider):
        def prepare_cached_session(self, **kwargs):
            raise RuntimeError("free-tier cache limit=0")

    activity, system = _cache_activity()
    handle = prepare_session_cache(
        activity=activity,
        registry=_registry_with_system(system),
        provider=_Boom(),
        created_at="t",
    )
    assert handle.supported is False  # degraded, not raised


def test_prepare_session_cache_fails_soft_when_no_system_prefix() -> None:
    # #60 review: with no system message there is no stable prefix to cache, so a
    # "supported" handle would be a silent no-op (prefix breakpoint never lands).
    from typeflux.core.contracts import (
        AIActivity,
        ChatMessage,
        PromptRef,
        ResolvedPrompt,
        SessionCacheConfig,
    )
    from typeflux.execution.executor import prepare_session_cache
    from typeflux.prompts import InlinePromptRegistry

    activity = AIActivity(
        name="extract",
        input_type=_In,
        output_type=_Out,
        prompt_ref=PromptRef("extract"),
        session_cache=SessionCacheConfig(),
    )
    registry = InlinePromptRegistry(
        {
            "extract": ResolvedPrompt(
                ref=PromptRef("extract"),
                messages=(ChatMessage(role="user", content="Just a user turn {{ query }}"),),
                resolved_version="v1",
                model="demo-model",
                temperature=0,
                metadata={},
            )
        }
    )
    provider = _PrefixCachingProvider()
    handle = prepare_session_cache(
        activity=activity, registry=registry, provider=provider, created_at="t"
    )
    assert handle.supported is False
    assert provider.prepared == []  # never asked the provider to prepare


def test_prepare_session_cache_enforces_model_policy_before_provider_prep() -> None:
    # #60 review (codex P1): a rejected model must never reach the provider's
    # prepare (reference-style uploads it), so the guard runs before prepare.
    from typeflux.execution.executor import prepare_session_cache
    from typeflux.providers.errors import ProviderPolicyError

    class _RejectingGuard:
        def enforce_provider_model(self, **kwargs):
            raise ProviderPolicyError("model not allowed", provider="demo")

    activity, system = _cache_activity()
    provider = _PrefixCachingProvider()
    with pytest.raises(ProviderPolicyError):
        prepare_session_cache(
            activity=activity,
            registry=_registry_with_system(system),
            provider=provider,
            created_at="t",
            provider_model_policy_guard=_RejectingGuard(),
        )
    assert provider.prepared == []  # guard blocked before any provider call


def test_prepare_session_cache_rejects_templated_system_prefix() -> None:
    from typeflux.execution.executor import prepare_session_cache
    from typeflux.execution.session_cache import UnstableCachePrefixError

    activity, _ = _cache_activity()
    with pytest.raises(UnstableCachePrefixError):
        prepare_session_cache(
            activity=activity,
            registry=_registry_with_system("You handle {{ query }} per item."),
            provider=_PrefixCachingProvider(),
            created_at="t",
        )


def test_session_cache_config_participates_in_workflow_digest() -> None:
    # #60 replay-safety: toggling caching on a map step must change the workflow
    # digest (and thus the registered workflow type), so old histories never
    # replay against a command sequence that now includes a cache-prep activity.
    from typeflux.core.contracts import SessionCacheConfig
    from typeflux.yaml.identity import _call_payload
    from typeflux.yaml.workflow import MapCallSpec, MapCollectCallSpec

    def _map(cache: SessionCacheConfig | None) -> MapCallSpec:
        return MapCallSpec(
            step_id="s",
            activity_name="a",
            over="input.items",
            concurrency=2,
            collect=MapCollectCallSpec(output_type=_Out, field="label"),
            task_queue=None,
            start_to_close_timeout=__import__("datetime").timedelta(seconds=1),
            retry_policy=None,
            session_cache=cache,
        )

    none_payload = _call_payload(_map(None))
    disabled_payload = _call_payload(_map(SessionCacheConfig(enabled=False, ttl_seconds=300)))
    enabled_payload = _call_payload(_map(SessionCacheConfig(enabled=True, ttl_seconds=300)))
    enabled_other_ttl = _call_payload(_map(SessionCacheConfig(enabled=True, ttl_seconds=600)))

    # No cache and disabled cache emit no prep step ⇒ identical identity.
    assert none_payload["session_cache"] is None
    assert disabled_payload["session_cache"] is None
    # Enabling caching changes identity; ttl is part of it.
    assert enabled_payload["session_cache"] == {"enabled": True, "ttl_seconds": 300}
    assert enabled_payload != none_payload
    assert enabled_payload != enabled_other_ttl


# --- #362: stable reference artifacts join the prefix cache ------------------


def _resolved_group(name: str):
    from typeflux.core.artifacts import (
        ArtifactRef,
        ArtifactSource,
        ResolvedArtifact,
        ResolvedArtifactGroup,
    )

    return ResolvedArtifactGroup(
        name=name,
        artifacts=(
            ResolvedArtifact(
                group=name,
                index=0,
                ref=ArtifactRef(source=ArtifactSource(type="local_path", path="x.pdf")),
                source_kind="local_path",
                kind="document",
                media_type="application/pdf",
                role=None,
                sha256=None,
                size_bytes=1,
            ),
        ),
    )


def _fake_resolve_groups(input_value, artifact_inputs, *, policy=None):
    return tuple(_resolved_group(ai.name) for ai in artifact_inputs)


def _compose_activity(*, with_reference: bool = True):
    from typeflux.core.artifacts import ArtifactAttachment, ArtifactInput
    from typeflux.core.contracts import AIActivity, PromptRef, SessionCacheConfig

    inputs = []
    if with_reference:
        inputs.append(
            ArtifactInput(
                name="contract",
                from_path="input.query",
                cache_role="reference",
                attach=ArtifactAttachment(role="user", text="Contract:"),
            )
        )
    inputs.append(
        ArtifactInput(
            name="evidence",
            from_path="input.query",
            attach=ArtifactAttachment(role="user", text="Evidence:"),
        )
    )
    return AIActivity(
        name="extract",
        input_type=_In,
        output_type=_Out,
        prompt_ref=PromptRef("extract"),
        session_cache=SessionCacheConfig(),
        artifact_inputs=tuple(inputs),
    )


def _msg_label(message) -> str:
    if message.role == "system":
        return "system"
    parts = message.content if isinstance(message.content, tuple) else (message.content,)
    groups = [getattr(part, "group", None) for part in parts]
    if "contract" in groups:
        return "reference"
    if "evidence" in groups:
        return "evidence"
    return "query"


def _registry_user_only():
    from typeflux.core.contracts import ChatMessage, PromptRef, ResolvedPrompt
    from typeflux.prompts import InlinePromptRegistry

    return InlinePromptRegistry(
        {
            "extract": ResolvedPrompt(
                ref=PromptRef("extract"),
                messages=(ChatMessage(role="user", content="Find: {{ query }}"),),
                resolved_version="v1",
                model="demo-model",
                temperature=0,
                metadata={},
            )
        }
    )


def _compose_labels(
    cached_session, *, with_reference: bool = True, registry=None, monkeypatch
) -> list[str]:
    from typeflux.execution import executor as ex
    from typeflux.testing import FakeProvider

    monkeypatch.setattr(ex, "resolve_artifact_inputs", _fake_resolve_groups)
    prepared = ex.prepare_ai_activity_execution(
        activity=_compose_activity(with_reference=with_reference),
        input_value=_In(query="q"),
        registry=registry
        if registry is not None
        else _registry_with_system("You extract citations."),
        provider=FakeProvider([]),
        cached_session=cached_session,
    )
    return [_msg_label(m) for m in prepared.messages]


def test_prefix_style_composition_places_reference_before_per_item_turn(monkeypatch) -> None:
    # #362 D362-2: prefix-style cache lifts the reference artifact into the cached
    # prefix — after the system block, before the varying per-item turn — while the
    # per-item (non-reference) artifact stays behind the turn.
    handle = CachedSessionHandle(
        provider="anthropic",
        identity_hash="h",
        supported=True,
        style="prefix",
        prefix_stable_messages=1,
    )
    labels = _compose_labels(handle, monkeypatch=monkeypatch)
    assert labels == ["system", "reference", "query", "evidence"]


def test_reference_style_hit_keeps_append_last_and_drops_reference(monkeypatch) -> None:
    # #362: a reference-style hit keeps today's append-last order and drops the
    # reference artifact (it lives in the provider cache object).
    handle = CachedSessionHandle(
        provider="ref-demo",
        identity_hash="h",
        supported=True,
        style="reference",
        reference_cached=True,
    )
    labels = _compose_labels(handle, monkeypatch=monkeypatch)
    assert labels == ["system", "query", "evidence"]


def test_uncached_keeps_append_last_order_byte_for_byte(monkeypatch) -> None:
    # #362: no caching ⇒ both artifacts append last, in author order (reference
    # input declared first), exactly as before this change.
    labels = _compose_labels(None, monkeypatch=monkeypatch)
    assert labels == ["system", "query", "reference", "evidence"]


def test_prefix_style_without_reference_inputs_keeps_append_last(monkeypatch) -> None:
    # #362: prefix-style but no reference input ⇒ composition rule does not engage,
    # the per-item artifact still appends last.
    handle = CachedSessionHandle(
        provider="anthropic", identity_hash="h", supported=True, style="prefix"
    )
    labels = _compose_labels(handle, with_reference=False, monkeypatch=monkeypatch)
    assert labels == ["system", "query", "evidence"]


def _prefix_ref_activity(attach_role: str = "user"):
    from typeflux.core.artifacts import ArtifactAttachment, ArtifactInput
    from typeflux.core.contracts import AIActivity, PromptRef, SessionCacheConfig

    return AIActivity(
        name="extract",
        input_type=_In,
        output_type=_Out,
        prompt_ref=PromptRef("extract"),
        session_cache=SessionCacheConfig(),
        artifact_inputs=(
            ArtifactInput(
                name="contract",
                from_path="input.query",
                cache_role="reference",
                attach=ArtifactAttachment(role=attach_role, text="Contract:"),
            ),
        ),
    )


def test_prep_sets_prefix_stable_messages_for_prefix_style() -> None:
    # #362 D362-3: prefix-style prep records the static count of reference turns on
    # the handle — no artifact resolution needed — and never sets reference_cached.
    from typeflux.execution.executor import prepare_session_cache

    handle = prepare_session_cache(
        activity=_prefix_ref_activity(),
        registry=_registry_with_system("You extract citations."),
        provider=_PrefixCachingProvider(),
        created_at="t",
    )
    assert handle.style == "prefix"
    assert handle.prefix_stable_messages == 1
    assert handle.reference_cached is False


def test_prep_prefix_stable_messages_none_without_reference_inputs() -> None:
    # #362: a prefix-style cache with no reference artifact keeps the field None so
    # the provider falls back to its legacy conversation[-2] breakpoint contract.
    from typeflux.execution.executor import prepare_session_cache

    activity, system = _cache_activity()
    handle = prepare_session_cache(
        activity=activity,
        registry=_registry_with_system(system),
        provider=_PrefixCachingProvider(),
        created_at="t",
    )
    assert handle.style == "prefix"
    assert handle.prefix_stable_messages is None


def test_prep_prefix_stable_messages_excludes_system_role_reference() -> None:
    # #362 D362-3: a system-role reference rides in the (already cached) system
    # block, not the conversation, so it must not count toward the breakpoint index.
    from typeflux.execution.executor import prepare_session_cache

    handle = prepare_session_cache(
        activity=_prefix_ref_activity(attach_role="system"),
        registry=_registry_with_system("You extract citations."),
        provider=_PrefixCachingProvider(),
        created_at="t",
    )
    assert handle.prefix_stable_messages is None


def test_prep_prefix_stable_messages_none_for_reference_style(monkeypatch) -> None:
    # #362: reference-style leaves prefix_stable_messages None — its artifacts are
    # uploaded into the cache object, not positioned in the per-item conversation.
    from typeflux.execution import executor as ex

    monkeypatch.setattr(ex, "resolve_artifact_inputs", _fake_resolve_groups)
    handle = ex.prepare_session_cache(
        activity=_ref_activity(),
        registry=_registry_with_system("You extract citations."),
        provider=_ReferenceCachingProvider(),
        created_at="t",
        input_value=_In(query="q"),
    )
    assert handle.style == "reference"
    assert handle.reference_cached is True
    assert handle.prefix_stable_messages is None


def _prefix_per_item_activity():
    # #698: a prefix-style activity whose ONLY artifact is a per-item (non-reference)
    # attach — the shape that trails the varying per-item turn with an artifact.
    from typeflux.core.artifacts import ArtifactAttachment, ArtifactInput
    from typeflux.core.contracts import AIActivity, PromptRef, SessionCacheConfig

    return AIActivity(
        name="extract",
        input_type=_In,
        output_type=_Out,
        prompt_ref=PromptRef("extract"),
        session_cache=SessionCacheConfig(),
        artifact_inputs=(
            ArtifactInput(
                name="evidence",
                from_path="input.query",
                attach=ArtifactAttachment(role="user", text="Evidence:"),
            ),
        ),
    )


def test_prep_sets_per_item_artifact_messages_for_prefix_style() -> None:
    # #698: prefix-style prep flags a per-item (non-reference) artifact attach so the
    # Anthropic breakpoint skips the conversation instead of mis-marking the query at
    # [-2]. With no reference input the reference count stays None.
    from typeflux.execution.executor import prepare_session_cache

    handle = prepare_session_cache(
        activity=_prefix_per_item_activity(),
        registry=_registry_with_system("You extract citations."),
        provider=_PrefixCachingProvider(),
        created_at="t",
    )
    assert handle.style == "prefix"
    assert handle.prefix_stable_messages is None
    assert handle.per_item_artifact_messages is True


def test_prep_per_item_artifact_messages_false_without_per_item_attach() -> None:
    # #698: the proven stable-instructions shape (no artifact inputs at all) keeps the
    # flag False, so the provider keeps its legacy conversation[-2] contract.
    from typeflux.execution.executor import prepare_session_cache

    activity, system = _cache_activity()
    handle = prepare_session_cache(
        activity=activity,
        registry=_registry_with_system(system),
        provider=_PrefixCachingProvider(),
        created_at="t",
    )
    assert handle.style == "prefix"
    assert handle.per_item_artifact_messages is False


def test_prep_per_item_artifact_messages_excludes_system_role_attach() -> None:
    # #698: a system-role per-item artifact folds into the (cached) system block, not
    # the conversation, so it must not flip the conversation-skip flag.
    from typeflux.core.artifacts import ArtifactAttachment, ArtifactInput
    from typeflux.core.contracts import AIActivity, PromptRef, SessionCacheConfig
    from typeflux.execution.executor import prepare_session_cache

    activity = AIActivity(
        name="extract",
        input_type=_In,
        output_type=_Out,
        prompt_ref=PromptRef("extract"),
        session_cache=SessionCacheConfig(),
        artifact_inputs=(
            ArtifactInput(
                name="evidence",
                from_path="input.query",
                attach=ArtifactAttachment(role="system", text="Evidence:"),
            ),
        ),
    )
    handle = prepare_session_cache(
        activity=activity,
        registry=_registry_with_system("You extract citations."),
        provider=_PrefixCachingProvider(),
        created_at="t",
    )
    assert handle.per_item_artifact_messages is False


def test_prep_sets_both_reference_count_and_per_item_flag_together() -> None:
    # #698 + #362: an activity with BOTH a reference input and a per-item artifact
    # sets prefix_stable_messages=1 (the reference span is authoritative) AND
    # per_item_artifact_messages=True. The provider then marks the reference span and
    # the flag is inert — end-to-end confirmation of the combined truth-table row.
    from typeflux.core.artifacts import ArtifactAttachment, ArtifactInput
    from typeflux.core.contracts import AIActivity, PromptRef, SessionCacheConfig
    from typeflux.execution.executor import prepare_session_cache

    activity = AIActivity(
        name="extract",
        input_type=_In,
        output_type=_Out,
        prompt_ref=PromptRef("extract"),
        session_cache=SessionCacheConfig(),
        artifact_inputs=(
            ArtifactInput(
                name="contract",
                from_path="input.query",
                cache_role="reference",
                attach=ArtifactAttachment(role="user", text="Contract:"),
            ),
            ArtifactInput(
                name="evidence",
                from_path="input.query",
                attach=ArtifactAttachment(role="user", text="Evidence:"),
            ),
        ),
    )
    handle = prepare_session_cache(
        activity=activity,
        registry=_registry_with_system("You extract citations."),
        provider=_PrefixCachingProvider(),
        created_at="t",
    )
    assert handle.prefix_stable_messages == 1
    assert handle.per_item_artifact_messages is True


def test_prep_per_item_artifact_messages_false_for_reference_style(monkeypatch) -> None:
    # #698: reference-style leaves the flag False — its per-item conversation is not the
    # prefix-marked path (artifacts live in the cache object).
    from typeflux.execution import executor as ex

    monkeypatch.setattr(ex, "resolve_artifact_inputs", _fake_resolve_groups)
    handle = ex.prepare_session_cache(
        activity=_ref_activity(),
        registry=_registry_with_system("You extract citations."),
        provider=_ReferenceCachingProvider(),
        created_at="t",
        input_value=_In(query="q"),
    )
    assert handle.style == "reference"
    assert handle.per_item_artifact_messages is False


# --- #362 review round: reference-only prompts + fail-closed optional shape ---


def test_reference_artifact_rejects_optional_without_attach_text() -> None:
    # #362 review (fail-closed): an optional, textless reference artifact emits NO
    # attach message for items where it resolves empty, so the conversation shape
    # (and the breakpoint index derived from the handle's static count) would vary
    # across items — breaking the byte-identical-prefix contract. Invalid at
    # construction; the two stable shapes are allowed.
    from typeflux.core.artifacts import ArtifactAttachment, ArtifactInput

    with pytest.raises(ValueError, match="must always produce its attach message"):
        ArtifactInput(
            name="contract",
            from_path="input.contract",
            cache_role="reference",
            required=False,
            attach=ArtifactAttachment(role="user"),
        )
    # Allowed: required + textless (resolution fails loud when the doc is absent,
    # so the attach message is emitted for every item that runs)…
    required_textless = ArtifactInput(
        name="contract",
        from_path="input.contract",
        cache_role="reference",
        attach=ArtifactAttachment(role="user"),
    )
    assert required_textless.required is True
    # …and optional WITH static attach text (the message is always emitted, with
    # or without the resolved artifact part).
    optional_with_text = ArtifactInput(
        name="contract",
        from_path="input.contract",
        cache_role="reference",
        required=False,
        attach=ArtifactAttachment(role="user", text="Contract:"),
    )
    assert optional_with_text.required is False


def test_spec_rejects_optional_textless_reference_artifact() -> None:
    # #362 review: the yaml spec surface routes through ArtifactInput, so the
    # invalid shape load-rejects with an error naming the artifact.
    from typeflux.yaml.spec import ArtifactInputSpec

    spec = ArtifactInputSpec.model_validate(
        {
            "name": "contract",
            "from": "input.contract",
            "cache": "reference",
            "required": False,
            "attach": {"role": "user"},
        }
    )
    with pytest.raises(ValueError, match="'contract'.*must always produce its attach message"):
        spec.to_artifact_input()


def test_prep_engages_prefix_cache_for_reference_only_prompt() -> None:
    # #362 review (codex P2): an activity whose ONLY stable content is the
    # reference document (no system message) must still engage a prefix-style
    # cache — the doc leads every per-item conversation and the breakpoint marks
    # it. (The true nothing-stable case — no system AND no reference input — still
    # fails soft: test_prepare_session_cache_fails_soft_when_no_system_prefix.)
    from typeflux.execution.executor import prepare_session_cache

    provider = _PrefixCachingProvider()
    handle = prepare_session_cache(
        activity=_prefix_ref_activity(),
        registry=_registry_user_only(),
        provider=provider,
        created_at="t",
    )
    assert handle.supported is True
    assert handle.style == "prefix"
    assert handle.prefix_stable_messages == 1
    assert len(provider.prepared) == 1  # the provider WAS asked to prepare


def test_prefix_composition_applies_for_reference_only_prompt(monkeypatch) -> None:
    # #362 review: with no system message the reference artifact still leads the
    # conversation, ahead of the varying per-item turn.
    handle = CachedSessionHandle(
        provider="anthropic",
        identity_hash="h",
        supported=True,
        style="prefix",
        prefix_stable_messages=1,
    )
    labels = _compose_labels(handle, registry=_registry_user_only(), monkeypatch=monkeypatch)
    assert labels == ["reference", "query", "evidence"]


def test_prefix_stable_count_matches_composed_reference_turns(monkeypatch) -> None:
    # #362 review invariant: for every VALID reference shape the handle's static
    # count equals the number of reference attach turns actually composed — even
    # when an optional (with-text) reference group resolves EMPTY, the attach
    # message is still emitted (text-only), so the breakpoint index stays on
    # stable content.
    from typeflux.core.artifacts import (
        ArtifactAttachment,
        ArtifactInput,
        ResolvedArtifactGroup,
    )
    from typeflux.core.contracts import AIActivity, PromptRef, SessionCacheConfig
    from typeflux.execution import executor as ex
    from typeflux.testing import FakeProvider

    def activity_for(reference: ArtifactInput) -> AIActivity:
        return AIActivity(
            name="extract",
            input_type=_In,
            output_type=_Out,
            prompt_ref=PromptRef("extract"),
            session_cache=SessionCacheConfig(),
            artifact_inputs=(reference,),
        )

    shapes = [
        # required + textless, group resolves non-empty.
        (
            ArtifactInput(
                name="contract",
                from_path="input.query",
                cache_role="reference",
                attach=ArtifactAttachment(role="user"),
            ),
            _fake_resolve_groups,
        ),
        # optional + text, group resolves EMPTY: the attach message still emits.
        (
            ArtifactInput(
                name="contract",
                from_path="input.query",
                cache_role="reference",
                required=False,
                attach=ArtifactAttachment(role="user", text="Contract:"),
            ),
            lambda input_value, artifact_inputs, *, policy=None: tuple(
                ResolvedArtifactGroup(name=ai.name, artifacts=()) for ai in artifact_inputs
            ),
        ),
    ]
    handle = CachedSessionHandle(
        provider="anthropic",
        identity_hash="h",
        supported=True,
        style="prefix",
        prefix_stable_messages=1,
    )
    for reference, fake_resolve in shapes:
        activity = activity_for(reference)
        monkeypatch.setattr(ex, "resolve_artifact_inputs", fake_resolve)
        prepared = ex.prepare_ai_activity_execution(
            activity=activity,
            input_value=_In(query="q"),
            registry=_registry_with_system("You extract citations."),
            provider=FakeProvider([]),
            cached_session=handle,
        )
        # Prompt renders [system, user]; everything beyond is the reference turn.
        composed_reference_turns = len(prepared.messages) - 2
        assert composed_reference_turns == ex._prefix_stable_message_count(activity) == 1
