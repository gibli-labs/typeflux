"""Per-subject crypto-shred keystore + subject-scoped codec seam (#715 slice 4).

Keystore lifecycle (mint-on-first-use, destroy, destroy-then-encode fail-closed,
destroy-then-decode DISTINCT shred error, unknown subject, thread-safety), the kid
scheme (canonical round-trip + fail-closed on malformed/non-canonical), the combine,
the subject-scoped codec driven through the SDK's REAL serialization-context seam
(no-subject byte-identical fallback, single/multi-subject round-trip, AND-shred,
fail-closed unbound encode), the binding registry + visibility fallback, and the
WIRED runtime build path (`_codec_data_converter` + spec ``subject_scope`` +
``_register_subject_scope_binding``) end to end.
"""

from __future__ import annotations

import asyncio
import base64
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio.api.common.v1 import Payload
from temporalio.converter import WorkflowSerializationContext

from typeflux.core.subjects import SUBJECT_IDS_SEARCH_ATTRIBUTE
from typeflux.yaml.payload_codec import (
    ENCRYPTED_ENCODING,
    KEY_ID_METADATA_KEY,
    KEY_LEN,
    PayloadCodecError,
    PayloadCodecKeySpec,
    PayloadCodecSpec,
    TypefluxAesGcmPayloadCodec,
)
from typeflux.yaml.secrets import SecretValueFromSpec
from typeflux.yaml.subject_keystore import (
    InMemorySubjectKeystore,
    SubjectKeyBindings,
    SubjectKeyShreddedError,
    SubjectKeyUnknownError,
    SubjectScopedPayloadCodec,
    combine_subject_key,
    parse_subject_kid,
    subject_key_state,
    subject_kid,
)

_SHARED_KEY = b"\x11" * KEY_LEN
# A textual, obviously-synthetic 32-byte codec key for the wired-build tests
# (secret-scanner friendly: English-like low entropy, the live tests' convention).
_WIRED_TEST_KEY = "typeflux-test-subject-key-32byte"


def _base() -> TypefluxAesGcmPayloadCodec:
    return TypefluxAesGcmPayloadCodec(current_kid="k1", keys={"k1": _SHARED_KEY})


def _bound_codec(
    keystore: InMemorySubjectKeystore,
    subjects: list[str],
    *,
    workflow_id: str = "wf-1",
) -> SubjectScopedPayloadCodec:
    """A codec bound the way the SDK binds it: register + with_context(workflow ctx)."""

    codec = SubjectScopedPayloadCodec(_base(), keystore)
    codec.bindings.register(workflow_id, subjects)
    return codec.with_context(
        WorkflowSerializationContext(namespace="default", workflow_id=workflow_id)
    )


def _sample(data: bytes = b'{"claim":"redact-me"}') -> Payload:
    return Payload(metadata={"encoding": b"json/plain"}, data=data)


# --- keystore lifecycle ---------------------------------------------------------------


def test_mint_on_first_use_is_stable_and_32_bytes() -> None:
    ks = InMemorySubjectKeystore()
    key = ks.data_key("subject-0001", create=True)
    assert len(key) == KEY_LEN
    # Same subject → same key (stable, not a fresh mint each call).
    assert ks.data_key("subject-0001", create=True) == key
    # Different subject → different key.
    assert ks.data_key("subject-0002", create=True) != key


def test_decode_path_without_create_raises_unknown_for_missing_subject() -> None:
    ks = InMemorySubjectKeystore()
    with pytest.raises(SubjectKeyUnknownError):
        ks.data_key("never-seen", create=False)


def test_destroy_then_encode_fails_closed_never_mints() -> None:
    ks = InMemorySubjectKeystore()
    ks.data_key("subject-0001", create=True)
    result = ks.destroy_subject_key("subject-0001")
    assert result.key_existed is True
    assert result.already_destroyed is False
    # A destroyed subject NEVER mints a fresh key, even on the create path.
    with pytest.raises(SubjectKeyShreddedError):
        ks.data_key("subject-0001", create=True)


def test_destroy_before_any_key_still_tombstones() -> None:
    ks = InMemorySubjectKeystore()
    result = ks.destroy_subject_key("subject-0001")
    # No live key existed, but the subject is now permanently un-mintable.
    assert result.key_existed is False
    with pytest.raises(SubjectKeyShreddedError):
        ks.data_key("subject-0001", create=True)


def test_destroy_is_idempotent() -> None:
    ks = InMemorySubjectKeystore()
    ks.data_key("subject-0001", create=True)
    first = ks.destroy_subject_key("subject-0001")
    second = ks.destroy_subject_key("subject-0001")
    assert first.already_destroyed is False and first.key_existed is True
    assert second.already_destroyed is True and second.key_existed is False


def test_destroyed_key_is_unrecoverable_through_the_api() -> None:
    ks = InMemorySubjectKeystore()
    ks.data_key("subject-0001", create=True)
    ks.destroy_subject_key("subject-0001")
    # No API path returns the destroyed key material — both create modes raise the shred.
    with pytest.raises(SubjectKeyShreddedError):
        ks.data_key("subject-0001", create=False)
    with pytest.raises(SubjectKeyShreddedError):
        ks.data_key("subject-0001", create=True)


def test_empty_subject_id_fails_closed() -> None:
    ks = InMemorySubjectKeystore()
    with pytest.raises(PayloadCodecError):
        ks.data_key("", create=True)
    with pytest.raises(PayloadCodecError):
        ks.destroy_subject_key("")


def test_subject_key_state_probe_is_non_minting_and_non_destroying() -> None:
    # The slice-5 dry-run introspection: three states, and the probe NEVER writes —
    # an "absent" probe must not mint a record and must not leave a tombstone.
    ks = InMemorySubjectKeystore()
    assert subject_key_state(ks, "subject-0001") == "absent"
    assert subject_key_state(ks, "subject-0001") == "absent"  # still absent: no mint
    key = ks.data_key("subject-0001", create=True)
    assert subject_key_state(ks, "subject-0001") == "live"
    # Probing did not rotate/destroy: the same key comes back.
    assert ks.data_key("subject-0001", create=False) == key
    ks.destroy_subject_key("subject-0001")
    assert subject_key_state(ks, "subject-0001") == "destroyed"
    with pytest.raises(PayloadCodecError):
        ks.subject_key_state("")


def test_subject_key_state_falls_back_to_a_read_only_probe() -> None:
    # A backend that predates the optional subjectKeyState method is probed through
    # data_key(create=False) — never minting — with its typed errors mapped.
    class _MinimalKeystore:
        def __init__(self) -> None:
            self._inner = InMemorySubjectKeystore()
            self.create_calls: list[bool] = []

        def data_key(self, subject_id: str, *, create: bool) -> bytes:
            self.create_calls.append(create)
            return self._inner.data_key(subject_id, create=create)

        def destroy_subject_key(self, subject_id: str):  # noqa: ANN201
            return self._inner.destroy_subject_key(subject_id)

    ks = _MinimalKeystore()
    assert subject_key_state(ks, "subject-0001") == "absent"
    ks._inner.data_key("subject-0001", create=True)
    assert subject_key_state(ks, "subject-0001") == "live"
    ks.destroy_subject_key("subject-0001")
    assert subject_key_state(ks, "subject-0001") == "destroyed"
    # Every probe went through the read-only path.
    assert set(ks.create_calls) == {False}


def test_keystore_is_runtime_checkable_protocol() -> None:
    from typeflux.yaml.subject_keystore import SubjectKeystore

    assert isinstance(InMemorySubjectKeystore(), SubjectKeystore)


def test_concurrent_mint_and_destroy_never_leaves_a_live_key() -> None:
    # A destroy interleaved with concurrent first-use mints must win: after the barrier
    # the subject is shredded and no thread can hold a mintable key. Exercises the lock
    # over the compound mint/destroy sections.
    for _ in range(50):
        ks = InMemorySubjectKeystore()
        barrier = threading.Barrier(2)

        def mint(store: InMemorySubjectKeystore = ks, gate: threading.Barrier = barrier) -> None:
            gate.wait()
            try:
                store.data_key("subject-0001", create=True)
            except SubjectKeyShreddedError:
                pass

        def destroy(store: InMemorySubjectKeystore = ks, gate: threading.Barrier = barrier) -> None:
            gate.wait()
            store.destroy_subject_key("subject-0001")

        threads = [threading.Thread(target=mint), threading.Thread(target=destroy)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Regardless of interleaving, the destroy tombstone stands.
        with pytest.raises(SubjectKeyShreddedError):
            ks.data_key("subject-0001", create=True)


# --- kid scheme + combine -------------------------------------------------------------


def test_subject_kid_round_trips_through_parse() -> None:
    for ids in (["subject-0001"], ["subject-0001", "subject-0002"], ["a:b,c=d weird"]):
        kid = subject_kid(ids)
        assert kid.startswith("tfsubj1:")
        assert parse_subject_kid(kid) == tuple(ids)


def test_subject_kid_rejects_empty_set() -> None:
    with pytest.raises(PayloadCodecError):
        subject_kid([])


def test_parse_rejects_non_subject_kid_and_malformed() -> None:
    with pytest.raises(PayloadCodecError):
        parse_subject_kid("key-2026")  # a shared kid, not subject-scoped
    with pytest.raises(PayloadCodecError):
        parse_subject_kid("tfsubj1:")  # names no subjects
    with pytest.raises(PayloadCodecError):
        parse_subject_kid("tfsubj1:!!!not-base64!!!")


def test_parse_rejects_non_canonical_segments() -> None:
    # kid metadata is unauthenticated: both editions must accept exactly ONE encoding
    # per subject set. Python's lenient urlsafe_b64decode would happily open padded or
    # standard-alphabet segments the TS parser rejects — the canonical re-encode check
    # closes that cross-edition divergence.
    with pytest.raises(PayloadCodecError, match="non-canonical"):
        parse_subject_kid("tfsubj1:c3ViamVjdA==")  # '='-padded form of 'subject'
    # A standard-alphabet segment ('+'/'/') whose url-safe form differs byte-for-byte.
    standard = base64.b64encode(b"subject\xfb\xff").decode("ascii").rstrip("=")
    assert "+" in standard or "/" in standard
    with pytest.raises(PayloadCodecError):
        parse_subject_kid("tfsubj1:" + standard)


def test_combine_is_order_sensitive_and_32_bytes() -> None:
    a, b = b"\x10" * KEY_LEN, b"\x20" * KEY_LEN
    combined = combine_subject_key([a, b])
    assert len(combined) == KEY_LEN
    # Order matters (kid order pins it), so swapping gives a different key.
    assert combine_subject_key([b, a]) != combined
    # Single-subject is the degenerate case, distinct from either raw key.
    assert combine_subject_key([a]) not in (a, b)


def test_combine_rejects_empty_and_wrong_length() -> None:
    with pytest.raises(PayloadCodecError):
        combine_subject_key([])
    with pytest.raises(PayloadCodecError):
        combine_subject_key([b"short"])


# --- reserved-prefix guard ------------------------------------------------------------


def test_configured_key_id_may_not_use_reserved_prefix() -> None:
    with pytest.raises(ValueError, match="reserved"):
        PayloadCodecKeySpec(id="tfsubj1:foo", value_from=SecretValueFromSpec(env="X"))
    with pytest.raises(PayloadCodecError, match="reserved"):
        TypefluxAesGcmPayloadCodec(current_kid="tfsubj1:foo", keys={"tfsubj1:foo": _SHARED_KEY})


# --- the subject-scoped codec through the SDK context seam ----------------------------


def test_no_subject_execution_uses_shared_key_path() -> None:
    ks = InMemorySubjectKeystore()
    codec = _bound_codec(ks, [])
    enc = asyncio.run(codec.encode([_sample()]))
    # Shared-key kid on the wire (byte-for-byte the #188 path), no subject minted.
    assert enc[0].metadata[KEY_ID_METADATA_KEY].decode("utf-8") == "k1"
    assert enc[0].metadata["encoding"] == ENCRYPTED_ENCODING
    # A plain shared-key codec decodes it (proves the wire is the shared format).
    assert asyncio.run(_base().decode(enc))[0].data == b'{"claim":"redact-me"}'


def test_single_subject_round_trip() -> None:
    ks = InMemorySubjectKeystore()
    codec = _bound_codec(ks, ["subject-0001"])
    enc = asyncio.run(codec.encode([_sample()]))
    assert enc[0].metadata[KEY_ID_METADATA_KEY].decode("utf-8") == subject_kid(["subject-0001"])
    dec = asyncio.run(codec.decode(enc))
    assert dec[0].data == b'{"claim":"redact-me"}'
    assert dec[0].metadata["encoding"] == b"json/plain"


def test_multi_subject_round_trip_and_and_shred_semantics() -> None:
    ks = InMemorySubjectKeystore()
    codec = _bound_codec(ks, ["subject-0001", "subject-0002"])
    enc = asyncio.run(codec.encode([_sample()]))
    assert asyncio.run(codec.decode(enc))[0].data == b'{"claim":"redact-me"}'
    # Destroying EITHER member subject shreds the shared payload (AND-semantics).
    ks.destroy_subject_key("subject-0002")
    with pytest.raises(SubjectKeyShreddedError):
        asyncio.run(codec.decode(enc))


def test_decode_after_shred_raises_distinct_error_never_plaintext() -> None:
    ks = InMemorySubjectKeystore()
    codec = _bound_codec(ks, ["subject-0001"])
    enc = asyncio.run(codec.encode([_sample()]))
    ks.destroy_subject_key("subject-0001")
    with pytest.raises(SubjectKeyShreddedError) as excinfo:
        asyncio.run(codec.decode(enc))
    # Names the specific shredded subject; distinct from a generic auth failure.
    assert excinfo.value.subject_id == "subject-0001"
    # And it IS catchable as the codec base type (fail-closed handlers still catch it).
    assert isinstance(excinfo.value, PayloadCodecError)


def test_decode_mixes_subject_scoped_shared_and_passthrough() -> None:
    ks = InMemorySubjectKeystore()
    codec = _bound_codec(ks, ["subject-0001"])
    subject_payload = asyncio.run(codec.encode([_sample(b"subj")]))[0]
    shared_payload = asyncio.run(_base().encode([_sample(b"shared")]))[0]
    plain_payload = _sample(b"plain")  # no encrypted marker → passthrough
    out = asyncio.run(codec.decode([subject_payload, shared_payload, plain_payload]))
    assert [p.data for p in out] == [b"subj", b"shared", b"plain"]


def test_decode_needs_no_binding_for_lifecycle_path() -> None:
    ks = InMemorySubjectKeystore()
    codec = _bound_codec(ks, ["subject-0001"])
    enc = asyncio.run(codec.encode([_sample()]))
    # Decode is kid-driven: an UNBOUND codec instance (fresh, no context, empty
    # registry) decodes it — the synchronous lifecycle decode path.
    plain = SubjectScopedPayloadCodec(_base(), ks)
    assert plain.decode_sync(enc)[0].data == b'{"claim":"redact-me"}'


def test_unbound_encode_fails_closed() -> None:
    # No serialization context ⇒ the owning execution is unknown ⇒ encode must fail
    # closed rather than guess shared-key for a possibly-subject execution.
    codec = SubjectScopedPayloadCodec(_base(), InMemorySubjectKeystore())
    with pytest.raises(PayloadCodecError, match="serialization context"):
        asyncio.run(codec.encode([_sample()]))


def test_malformed_registered_subjects_fail_closed() -> None:
    codec = SubjectScopedPayloadCodec(_base(), InMemorySubjectKeystore())
    with pytest.raises(ValueError, match="non-empty"):
        codec.bindings.register("wf-1", ["", "ok"])


# --- the binding registry + visibility fallback ---------------------------------------


class _FakeTyped:
    def __init__(self, value: Any) -> None:
        self._value = value

    def get(self, key: Any) -> Any:
        if getattr(key, "name", None) == SUBJECT_IDS_SEARCH_ATTRIBUTE:
            if self._value is None:
                raise KeyError(key)
            return self._value
        raise KeyError(key)


class _FakeDescribeClient:
    def __init__(self, subjects_by_workflow: dict[str, Any], *, fail: bool = False) -> None:
        self._subjects = subjects_by_workflow
        self._fail = fail
        self.describes: list[str] = []

    def get_workflow_handle(self, workflow_id: str) -> Any:
        client = self

        class _Handle:
            async def describe(self) -> Any:
                client.describes.append(workflow_id)
                if client._fail:
                    raise RuntimeError("visibility down")
                if workflow_id not in client._subjects:
                    raise RuntimeError("workflow not found")
                return SimpleNamespace(
                    typed_search_attributes=_FakeTyped(client._subjects[workflow_id])
                )

        return _Handle()


def test_bindings_registry_wins_without_a_describe() -> None:
    bindings = SubjectKeyBindings()
    bindings.register("wf-1", ["subject-0001"])
    assert asyncio.run(bindings.resolve("wf-1")) == ("subject-0001",)


def test_bindings_visibility_fallback_reads_the_search_attribute_and_caches() -> None:
    bindings = SubjectKeyBindings()
    client = _FakeDescribeClient({"wf-9": ["subject-0001", "subject-0002"]})
    bindings.bind_client(client)
    assert asyncio.run(bindings.resolve("wf-9")) == ("subject-0001", "subject-0002")
    # Cached: a second resolve makes no further describe.
    assert asyncio.run(bindings.resolve("wf-9")) == ("subject-0001", "subject-0002")
    assert client.describes == ["wf-9"]


def test_bindings_describe_without_attribute_pins_no_subjects() -> None:
    bindings = SubjectKeyBindings()
    client = _FakeDescribeClient({"wf-plain": None})
    bindings.bind_client(client)
    assert asyncio.run(bindings.resolve("wf-plain")) == ()


def test_bindings_miss_without_client_fails_closed() -> None:
    bindings = SubjectKeyBindings()
    with pytest.raises(PayloadCodecError, match="fails closed"):
        asyncio.run(bindings.resolve("wf-unknown"))


def test_bindings_describe_failure_fails_closed_with_child_hint() -> None:
    bindings = SubjectKeyBindings()
    bindings.bind_client(_FakeDescribeClient({}, fail=True))
    with pytest.raises(PayloadCodecError, match="sub-workflow composition"):
        asyncio.run(bindings.resolve("wf-child"))


# --- the WIRED runtime build path (#715 slice 4, item: spec model = wired) ------------


def _subject_scope_spec() -> PayloadCodecSpec:
    return PayloadCodecSpec.model_validate(
        {
            "type": "aes",
            "current": "k1",
            "keys": [{"id": "k1", "value_from": {"env": "TF_SUBJ_CODEC_KEY"}}],
            "subject_scope": {},
        }
    )


def _wired_converter(monkeypatch: pytest.MonkeyPatch, keystore: Any | None = None) -> Any:
    from temporalio.contrib.pydantic import pydantic_data_converter

    from typeflux.yaml.runtime import _codec_data_converter

    monkeypatch.setenv("TF_SUBJ_CODEC_KEY", _WIRED_TEST_KEY)
    # The converter build FAILS CLOSED without a keystore (see the dedicated test);
    # build_runtime is the one caller allowed to supply the in-memory default, so the
    # test helper mirrors it.
    return _codec_data_converter(
        pydantic_data_converter,
        _subject_scope_spec(),
        subject_keystore=keystore if keystore is not None else InMemorySubjectKeystore(),
    )


def test_spec_subject_scope_materializes_the_in_memory_default() -> None:
    spec = _subject_scope_spec()
    assert spec.subject_scope is not None
    # The zod-optional/pydantic-default parity rule: the default is materialized.
    assert spec.subject_scope.keystore == "in_memory"


def test_wired_converter_builds_subject_scoped_codec(monkeypatch: pytest.MonkeyPatch) -> None:
    converter = _wired_converter(monkeypatch)
    assert isinstance(converter.payload_codec, SubjectScopedPayloadCodec)


def test_wired_converter_without_keystore_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # #715 Bugbot: subject_scope on a path with no injected keystore must be a LOUD
    # error, never a silently-minted process-local keystore (a CP/reader process would
    # seal subject payloads under keys no worker holds).
    from temporalio.contrib.pydantic import pydantic_data_converter

    from typeflux.yaml.runtime import _codec_data_converter

    monkeypatch.setenv("TF_SUBJ_CODEC_KEY", _WIRED_TEST_KEY)
    with pytest.raises(PayloadCodecError, match="SHARED keystore"):
        _codec_data_converter(pydantic_data_converter, _subject_scope_spec())


def test_wired_converter_without_subject_scope_stays_plain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from temporalio.contrib.pydantic import pydantic_data_converter

    from typeflux.yaml.runtime import _codec_data_converter

    monkeypatch.setenv("TF_SUBJ_CODEC_KEY", _WIRED_TEST_KEY)
    spec = PayloadCodecSpec.model_validate(
        {
            "type": "aes",
            "current": "k1",
            "keys": [{"id": "k1", "value_from": {"env": "TF_SUBJ_CODEC_KEY"}}],
        }
    )
    converter = _codec_data_converter(pydantic_data_converter, spec)
    assert isinstance(converter.payload_codec, TypefluxAesGcmPayloadCodec)
    assert not isinstance(converter.payload_codec, SubjectScopedPayloadCodec)


def test_wired_path_seals_subject_execution_and_shreds_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The e2e proof through the REAL wiring: spec ``subject_scope`` →
    ``_codec_data_converter`` → start-path binding registration → the SDK's own
    ``DataConverter.with_context`` → subject-sealed payloads; destroy → distinct
    shred error; post-shred encode fails closed."""

    from typeflux.yaml.runtime import _register_subject_scope_binding

    keystore = InMemorySubjectKeystore()
    converter = _wired_converter(monkeypatch, keystore)
    # The start path registers the binding through the runtime helper, exactly as
    # execute_workflow/start_workflow do (stub client exposing the converter).
    stub_client = SimpleNamespace(data_converter=converter)
    _register_subject_scope_binding(stub_client, "wf-subj", ["subject-0001"])
    # The SDK binds the codec per operation via DataConverter.with_context — use the
    # REAL SDK method, not a hand-rolled copy.
    bound = converter.with_context(
        WorkflowSerializationContext(namespace="default", workflow_id="wf-subj")
    )
    enc = asyncio.run(bound.payload_codec.encode([_sample()]))
    kid = enc[0].metadata[KEY_ID_METADATA_KEY].decode("utf-8")
    assert kid == subject_kid(["subject-0001"])
    assert asyncio.run(bound.payload_codec.decode(enc))[0].data == b'{"claim":"redact-me"}'
    # Erasure: destroy the record → decode fails with the DISTINCT shred error.
    keystore.destroy_subject_key("subject-0001")
    with pytest.raises(SubjectKeyShreddedError):
        asyncio.run(bound.payload_codec.decode(enc))
    # And a post-shred encode for the subject fails closed (no resurrection).
    with pytest.raises(SubjectKeyShreddedError):
        asyncio.run(bound.payload_codec.encode([_sample()]))


def test_wired_path_no_subject_execution_is_byte_compatible_with_plain_codec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-subject execution through the SAME wired converter produces shared-key wire
    bytes the UNWRAPPED codec opens — the fallback is provably unchanged."""

    from typeflux.yaml.runtime import _register_subject_scope_binding

    converter = _wired_converter(monkeypatch)
    stub_client = SimpleNamespace(data_converter=converter)
    _register_subject_scope_binding(stub_client, "wf-plain", [])
    bound = converter.with_context(
        WorkflowSerializationContext(namespace="default", workflow_id="wf-plain")
    )
    enc = asyncio.run(bound.payload_codec.encode([_sample()]))
    assert enc[0].metadata[KEY_ID_METADATA_KEY].decode("utf-8") == "k1"
    # The plain shared-key codec (same key material) opens it — same wire format.
    plain = TypefluxAesGcmPayloadCodec(current_kid="k1", keys={"k1": _WIRED_TEST_KEY.encode()})
    assert plain.decode_sync(enc)[0].data == b'{"claim":"redact-me"}'


def test_register_helper_is_a_noop_for_plain_codecs() -> None:
    from temporalio.contrib.pydantic import pydantic_data_converter

    from typeflux.yaml.runtime import _register_subject_scope_binding

    # No codec at all, and a plain converter: both are silent no-ops.
    _register_subject_scope_binding(SimpleNamespace(data_converter=None), "wf-1", [])
    _register_subject_scope_binding(
        SimpleNamespace(data_converter=pydantic_data_converter), "wf-1", []
    )


def test_build_runtime_rejects_subject_scope_with_subworkflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #715 slice 4 boundary: a child's input is encoded before the child exists, so
    # its binding cannot be resolved — the combination is rejected at build time,
    # BEFORE any client connect.
    from typeflux.yaml.runtime import build_runtime

    monkeypatch.setenv("TF_SUBJ_CODEC_KEY", _WIRED_TEST_KEY)
    spec = SimpleNamespace(
        runtime=SimpleNamespace(temporal=SimpleNamespace(payload_codec=_subject_scope_spec())),
        workflow=SimpleNamespace(version=None),
    )
    prepared = SimpleNamespace(child_workflow_classes=(object,), plugin=None)
    with pytest.raises(ValueError, match="sub-workflow composition"):
        asyncio.run(build_runtime(spec, prepared=prepared))  # type: ignore[arg-type]


def test_build_runtime_rejects_subject_scope_when_the_spec_declares_subworkflows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # #715 Bugbot: the rejection keys on what the SPEC GRAPH declares (a `workflow:`
    # step), NOT only on resolved child classes — a spec with sub-workflow steps and NO
    # child classes passed must still be rejected (TS specReferencesSubworkflows
    # parity), before any client connect.
    from tests.test_yaml import _write_demo_yaml
    from typeflux.yaml.loader import load_yaml_spec
    from typeflux.yaml.runtime import build_runtime

    monkeypatch.setenv("TF_SUBJ_CODEC_KEY", _WIRED_TEST_KEY)
    spec = load_yaml_spec(
        _write_demo_yaml(
            tmp_path,
            runtime=_SUBJECT_SCOPE_RUNTIME,
            steps="""
    - id: first
      activity: first
    - id: sub
      workflow: sibling_workflow
    """,
        )
    )
    prepared = SimpleNamespace(child_workflow_classes=(), plugin=None)
    with pytest.raises(ValueError, match="sub-workflow composition"):
        asyncio.run(build_runtime(spec, prepared=prepared))  # type: ignore[arg-type]


_SUBJECT_SCOPE_RUNTIME = """
temporal:
  address: localhost:7233
  payload_codec:
    type: aes
    current: main
    keys:
      - id: main
        value_from:
          env: TF_SUBJ_CODEC_KEY
    subject_scope: {}
registry:
  type: inline
  prompts:
    first: first {{value}}
    second: second {{value}}
provider:
  type: fake
"""


def test_cp_binding_rejects_subject_scope_without_a_shared_keystore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #715 Bugbot HIGH: the CP's ts-binding driver is never the sole owner of subject
    # key records — subject_scope with no injected keystore must be a LOUD config
    # error, never a silently-minted process-local keystore whose signal encodes no
    # worker can decode.
    from typeflux.project.binding_ts import TsBindingConfigError, _resolve_payload_codec

    monkeypatch.setenv("TF_SUBJ_CODEC_KEY", _WIRED_TEST_KEY)
    block = {
        "type": "aes",
        "current": "k1",
        "keys": [{"id": "k1", "value_from": {"env": "TF_SUBJ_CODEC_KEY"}}],
        "subject_scope": {},
    }
    with pytest.raises(TsBindingConfigError, match="SHARED SubjectKeystore"):
        _resolve_payload_codec(block)


def test_cp_binding_with_injected_shared_keystore_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With the deployment's SHARED keystore injected, the CP-side codec seals a
    # subject-scoped payload the (keystore-sharing) worker-side codec opens — and
    # vice versa.
    from typeflux.project.binding_ts import _resolve_payload_codec

    monkeypatch.setenv("TF_SUBJ_CODEC_KEY", _WIRED_TEST_KEY)
    block = {
        "type": "aes",
        "current": "k1",
        "keys": [{"id": "k1", "value_from": {"env": "TF_SUBJ_CODEC_KEY"}}],
        "subject_scope": {},
    }
    shared_keystore = InMemorySubjectKeystore()
    cp_codec = _resolve_payload_codec(block, subject_keystore=shared_keystore)
    assert isinstance(cp_codec, SubjectScopedPayloadCodec)
    cp_codec.bindings.register("wf-signal", ["subject-0001"])
    bound = cp_codec.with_context(
        WorkflowSerializationContext(namespace="default", workflow_id="wf-signal")
    )
    sealed = asyncio.run(bound.encode([_sample(b"signal")]))
    assert sealed[0].metadata[KEY_ID_METADATA_KEY].decode("utf-8") == subject_kid(["subject-0001"])
    # The worker-side codec (same shared keystore, fresh registry) opens it — decode
    # is kid-driven.
    worker_codec = SubjectScopedPayloadCodec(_base(), shared_keystore)
    assert worker_codec.decode_sync(sealed)[0].data == b"signal"


def test_yaml_spec_accepts_subject_scope_block(tmp_path: Path) -> None:
    # The spec-level opt-in parses through the REAL loader path and materializes the
    # in_memory default.
    from tests.test_yaml import _write_demo_yaml
    from typeflux.yaml.loader import load_yaml_spec

    spec = load_yaml_spec(_write_demo_yaml(tmp_path, runtime=_SUBJECT_SCOPE_RUNTIME))
    codec_spec = spec.runtime.temporal.payload_codec
    assert codec_spec is not None
    assert codec_spec.subject_scope is not None
    assert codec_spec.subject_scope.keystore == "in_memory"
