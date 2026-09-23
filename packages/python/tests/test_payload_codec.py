"""AES-256-GCM Temporal payload codec (#188 slice 1).

Round-trip + fail-closed unit tests, spec/load validation, the SECRET_SLOT_PATHS masking
proof, digest invariance (codec-off = unchanged), and the CROSS-EDITION conformance vector
(the load-bearing proof that the Python and TS codecs share a byte-identical wire format).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from temporalio.api.common.v1 import Payload

from tests.test_yaml import _write_demo_project, _write_demo_yaml
from typeflux.yaml.loader import load_yaml_spec
from typeflux.yaml.payload_codec import (
    ENCRYPTED_ENCODING,
    KEY_ID_METADATA_KEY,
    NONCE_LEN,
    TAG_LEN,
    PayloadCodecError,
    PayloadCodecSpec,
    TypefluxAesGcmPayloadCodec,
    build_payload_codec,
    open_payload_bytes,
    seal_payload_bytes,
)
from typeflux.yaml.secrets import secret_reference_records

_VECTORS = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "contracts"
        / "temporal-binding"
        / "payload-codec-vectors.json"
    ).read_text(encoding="utf-8")
)

_KEY = b"\x00" * 16 + b"\x11" * 16  # 32 bytes


def _codec() -> TypefluxAesGcmPayloadCodec:
    return TypefluxAesGcmPayloadCodec(current_kid="k1", keys={"k1": _KEY})


def _sample() -> Payload:
    return Payload(metadata={"encoding": b"json/plain"}, data=b'{"claim":"redact-me"}')


# --- round trip -----------------------------------------------------------------------


def test_encrypt_decrypt_round_trip() -> None:
    codec = _codec()
    original = _sample()
    encrypted = asyncio.run(codec.encode([original]))
    assert encrypted[0].metadata["encoding"] == ENCRYPTED_ENCODING
    assert encrypted[0].metadata[KEY_ID_METADATA_KEY] == b"k1"
    assert len(encrypted[0].data) == NONCE_LEN + len(original.SerializeToString()) + TAG_LEN
    decrypted = asyncio.run(codec.decode(encrypted))
    assert decrypted[0].data == original.data
    assert dict(decrypted[0].metadata) == dict(original.metadata)


def test_nonce_is_unique_per_encrypt() -> None:
    codec = _codec()
    original = _sample()
    first = asyncio.run(codec.encode([original]))
    second = asyncio.run(codec.encode([original]))
    # Same plaintext, different ciphertext ⇒ a fresh CSPRNG nonce each time.
    assert first[0].data != second[0].data
    assert first[0].data[:NONCE_LEN] != second[0].data[:NONCE_LEN]


def test_plaintext_payload_passes_decode_untouched() -> None:
    # A pre-codec / non-encrypted payload (no binary/encrypted marker) is passed through.
    codec = _codec()
    plain = _sample()
    out = codec.decode_sync([plain])
    assert out[0].data == plain.data
    assert dict(out[0].metadata) == dict(plain.metadata)


def test_rotation_decrypts_old_key_after_current_flip() -> None:
    old, new = b"a" * 32, b"b" * 32
    encrypting = TypefluxAesGcmPayloadCodec(current_kid="old", keys={"old": old})
    encrypted = asyncio.run(encrypting.encode([_sample()]))
    # `current` flips to `new`, but the old key stays declared → old history still decrypts.
    rotated = TypefluxAesGcmPayloadCodec(current_kid="new", keys={"new": new, "old": old})
    decrypted = asyncio.run(rotated.decode(encrypted))
    assert decrypted[0].data == _sample().data


# --- fail-closed ----------------------------------------------------------------------


def test_unknown_key_id_fails_closed() -> None:
    encrypted = seal_payload_bytes(
        key=b"z" * 32, kid="unregistered", nonce=b"\x01" * NONCE_LEN, plaintext=b"x"
    )
    with pytest.raises(PayloadCodecError, match="no key registered"):
        _codec().decode_sync([encrypted])


def test_tampered_ciphertext_fails_authentication() -> None:
    encrypted = asyncio.run(_codec().encode([_sample()]))[0]
    tampered = Payload()
    tampered.CopyFrom(encrypted)
    flipped = bytearray(tampered.data)
    flipped[NONCE_LEN + 2] ^= 0x01  # flip a ciphertext byte
    tampered.data = bytes(flipped)
    with pytest.raises(PayloadCodecError, match="authentication"):
        _codec().decode_sync([tampered])


def test_wrong_length_key_rejected_at_build() -> None:
    with pytest.raises(PayloadCodecError, match="32 bytes"):
        TypefluxAesGcmPayloadCodec(current_kid="k1", keys={"k1": b"too-short"})


def test_current_not_in_keys_rejected() -> None:
    with pytest.raises(PayloadCodecError, match="current key id"):
        TypefluxAesGcmPayloadCodec(current_kid="missing", keys={"k1": _KEY})


# --- spec + build resolution ----------------------------------------------------------


def test_build_from_spec_resolves_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TF_CODEC_KEY", ("01234567" + "89abcdef") * 2)  # 32 bytes utf-8
    spec = PayloadCodecSpec.model_validate(
        {
            "type": "aes",
            "current": "k1",
            "keys": [{"id": "k1", "value_from": {"env": "TF_CODEC_KEY"}}],
        }
    )
    codec = build_payload_codec(spec)
    assert codec is not None
    round_tripped = asyncio.run(codec.decode(asyncio.run(codec.encode([_sample()]))))
    assert round_tripped[0].data == _sample().data


def test_build_missing_required_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TF_CODEC_MISSING", raising=False)
    spec = PayloadCodecSpec.model_validate(
        {
            "type": "aes",
            "current": "k1",
            "keys": [{"id": "k1", "value_from": {"env": "TF_CODEC_MISSING"}}],
        }
    )
    with pytest.raises((PayloadCodecError, ValueError)):
        build_payload_codec(spec)


def test_build_wrong_length_env_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TF_CODEC_SHORT", "not-32-bytes")
    spec = PayloadCodecSpec.model_validate(
        {
            "type": "aes",
            "current": "k1",
            "keys": [{"id": "k1", "value_from": {"env": "TF_CODEC_SHORT"}}],
        }
    )
    with pytest.raises(PayloadCodecError, match="32 bytes"):
        build_payload_codec(spec)


def test_build_none_returns_none() -> None:
    assert build_payload_codec(None) is None


def test_spec_requires_current_to_name_a_key() -> None:
    with pytest.raises(ValueError, match="current must name"):
        PayloadCodecSpec.model_validate(
            {"type": "aes", "current": "nope", "keys": [{"id": "k1", "value_from": {"env": "X"}}]}
        )


def test_spec_requires_at_least_one_key() -> None:
    with pytest.raises(ValueError, match="at least one key"):
        PayloadCodecSpec.model_validate({"type": "aes", "current": "k1", "keys": []})


def test_spec_rejects_duplicate_key_ids() -> None:
    with pytest.raises(ValueError, match="unique"):
        PayloadCodecSpec.model_validate(
            {
                "type": "aes",
                "current": "k1",
                "keys": [
                    {"id": "k1", "value_from": {"env": "A"}},
                    {"id": "k1", "value_from": {"env": "B"}},
                ],
            }
        )


# --- spec load: the field on TemporalSpec ---------------------------------------------

_CODEC_RUNTIME = """
temporal:
  address: localhost:7233
  payload_codec:
    type: aes
    current: main
    keys:
      - id: main
        value_from:
          env: TF_CODEC_KEY
registry:
  type: inline
  prompts:
    first: first {{value}}
    second: second {{value}}
provider:
  type: fake
"""


def test_payload_codec_loads_onto_temporal_spec(tmp_path: Path) -> None:
    spec = load_yaml_spec(_write_demo_yaml(tmp_path, runtime=_CODEC_RUNTIME))
    codec_spec = spec.runtime.temporal.payload_codec
    assert codec_spec is not None
    assert codec_spec.type == "aes"
    assert codec_spec.current == "main"
    assert [key.id for key in codec_spec.keys] == ["main"]
    assert codec_spec.keys[0].value_from.env == "TF_CODEC_KEY"


def test_absent_payload_codec_is_off(tmp_path: Path) -> None:
    spec = load_yaml_spec(_write_demo_yaml(tmp_path))
    assert spec.runtime.temporal.payload_codec is None


# --- SECRET_SLOT_PATHS masking (the "never credentials" contract) ---------------------


def test_secret_reference_records_mask_codec_key_to_source(tmp_path: Path) -> None:
    spec = load_yaml_spec(_write_demo_yaml(tmp_path, runtime=_CODEC_RUNTIME))
    records = secret_reference_records(spec)
    codec_records = [r for r in records if "payload_codec" in r.runtime_path]
    assert len(codec_records) == 1
    record = codec_records[0]
    assert record.runtime_path == "runtime.temporal.payload_codec.keys[main].value_from"
    assert record.source_kind == "env"
    assert record.source_name == "TF_CODEC_KEY"
    # The record carries provenance only — never the key VALUE.
    assert not any("0123456789" in str(value) for value in vars(record).values())


# --- digest invariance (codec-off = unchanged) ----------------------------------------


def test_enabling_codec_does_not_change_workflow_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from temporalio import workflow

    from typeflux.yaml.imports import collect_activities
    from typeflux.yaml.workflow import create_workflow

    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))

    plain = load_yaml_spec(_write_demo_yaml(tmp_path, workflow_version="v1"))
    with_codec = load_yaml_spec(
        _write_demo_yaml(tmp_path, runtime=_CODEC_RUNTIME, workflow_version="v1")
    )
    # The codec is runtime-only; the workflow type (spec digest) hashes the graph, NOT
    # runtime.temporal — so enabling the codec leaves the registered type/digest unchanged.
    plain_type = workflow._Definition.must_from_class(
        create_workflow(plain, collect_activities(plain))
    ).name
    codec_type = workflow._Definition.must_from_class(
        create_workflow(with_codec, collect_activities(with_codec))
    ).name
    assert plain_type == codec_type


# --- cross-edition conformance (byte-pinned, shared vector) ----------------------------


def test_cross_edition_vector_seal_and_open() -> None:
    for vector in _VECTORS["vectors"]:
        key = bytes.fromhex(vector["key_hex"])
        nonce = bytes.fromhex(vector["nonce_hex"])
        plaintext = bytes.fromhex(vector["plaintext_hex"])
        sealed = seal_payload_bytes(key=key, kid=vector["kid"], nonce=nonce, plaintext=plaintext)
        # Byte-identical to the pinned wire bytes (the TS edition asserts the SAME).
        assert sealed.data.hex() == vector["data_hex"]
        assert sealed.metadata["encoding"] == ENCRYPTED_ENCODING
        assert sealed.metadata[KEY_ID_METADATA_KEY].decode("utf-8") == vector["kid"]
        # And the pinned ciphertext opens back to the exact plaintext (decrypt interop).
        assert open_payload_bytes(key=key, data=bytes.fromhex(vector["data_hex"])) == plaintext
        # The inner proto parses to the described Payload.
        inner = Payload()
        inner.ParseFromString(plaintext)
        described = vector["inner_payload"]
        assert inner.metadata["encoding"].decode("utf-8") == described["metadata"]["encoding"]
        assert inner.data.decode("utf-8") == described["data_utf8"]


def test_subject_scoped_conformance_vectors() -> None:
    # The #715 slice 4 subject-scoped crypto-shred vectors: reproduce the pinned kid +
    # combined key + ciphertext, open them back, and route a destroyed subject to the
    # DISTINCT shred error (never plaintext). The TS edition asserts the SAME vectors.
    from typeflux.yaml.subject_keystore import (
        InMemorySubjectKeystore,
        SubjectKeyShreddedError,
        SubjectScopedPayloadCodec,
        combine_subject_key,
        subject_kid,
    )

    scheme = _VECTORS["subject_scoped"]
    nonce = bytes.fromhex(scheme["nonce_hex"])
    plaintext = bytes.fromhex(scheme["plaintext_hex"])
    by_name = {vector["name"]: vector for vector in scheme["vectors"]}
    for vector in scheme["vectors"]:
        record_keys = [
            bytes.fromhex(vector["subject_record_keys_hex"][subject_id])
            for subject_id in vector["subject_ids"]
        ]
        combined = combine_subject_key(record_keys)
        assert combined.hex() == vector["combined_sha256_hex"], vector["name"]
        assert subject_kid(vector["subject_ids"]) == vector["kid"], vector["name"]
        sealed = seal_payload_bytes(
            key=combined, kid=vector["kid"], nonce=nonce, plaintext=plaintext
        )
        assert sealed.data.hex() == vector["data_hex"], vector["name"]
        assert open_payload_bytes(key=combined, data=bytes.fromhex(vector["data_hex"])) == plaintext

    # decode-after-shred: destroy the named subject, then decode of the pinned data_hex
    # raises the distinct shred error rather than returning plaintext. (Decode is
    # kid-driven — no serialization context needed.)
    shred = scheme["decode_after_shred"]
    target = by_name[shred["vector_name"]]
    keystore = InMemorySubjectKeystore()
    # Seed the keystore's records so the codec matches the vector's key material.
    for subject_id, key_hex in target["subject_record_keys_hex"].items():
        keystore.seed_record_for_test(subject_id, bytes.fromhex(key_hex))
    codec = SubjectScopedPayloadCodec(
        TypefluxAesGcmPayloadCodec(current_kid="k1", keys={"k1": _KEY}),
        keystore,
    )
    sealed_payload = Payload(
        metadata={
            "encoding": ENCRYPTED_ENCODING,
            KEY_ID_METADATA_KEY: target["kid"].encode("utf-8"),
        },
        data=bytes.fromhex(target["data_hex"]),
    )
    keystore.destroy_subject_key(shred["destroy_subject_id"])
    with pytest.raises(SubjectKeyShreddedError):
        codec.decode_sync([sealed_payload])


def test_shared_vectors_reproduce_through_the_subject_scoped_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The binding note's claim, proven at the VECTOR level: the pinned SHARED-key
    vectors reproduce byte-for-byte THROUGH the subject-scoped wrapper when the
    execution has no subjects — encode (pinned nonce) and decode both — so any drift
    in the fallback path fails CI."""

    import os as _os

    from temporalio.converter import WorkflowSerializationContext

    from typeflux.yaml.subject_keystore import (
        InMemorySubjectKeystore,
        SubjectScopedPayloadCodec,
    )

    for vector in _VECTORS["vectors"]:
        key = bytes.fromhex(vector["key_hex"])
        nonce = bytes.fromhex(vector["nonce_hex"])
        plaintext = bytes.fromhex(vector["plaintext_hex"])
        base = TypefluxAesGcmPayloadCodec(current_kid=vector["kid"], keys={vector["kid"]: key})
        wrapper = SubjectScopedPayloadCodec(base, InMemorySubjectKeystore())
        wrapper.bindings.register("wf-no-subjects", [])
        bound = wrapper.with_context(
            WorkflowSerializationContext(namespace="default", workflow_id="wf-no-subjects")
        )
        # ENCODE through the wrapper (pinned nonce via urandom patch) must reproduce
        # the pinned wire bytes exactly.
        inner = Payload()
        inner.ParseFromString(plaintext)
        monkeypatch.setattr(_os, "urandom", lambda n, _nonce=nonce: _nonce)
        try:
            sealed = asyncio.run(bound.encode([inner]))[0]
        finally:
            monkeypatch.undo()
        assert sealed.data.hex() == vector["data_hex"]
        assert sealed.metadata[KEY_ID_METADATA_KEY].decode("utf-8") == vector["kid"]
        # DECODE of the pinned wire bytes through the wrapper opens to the pinned
        # inner payload (byte equality of the serialized inner proto).
        pinned = Payload(
            metadata={
                "encoding": ENCRYPTED_ENCODING,
                KEY_ID_METADATA_KEY: vector["kid"].encode("utf-8"),
            },
            data=bytes.fromhex(vector["data_hex"]),
        )
        opened = bound.decode_sync([pinned])[0]
        assert opened.SerializeToString() == plaintext


# --- the synchronous lifecycle decode path is codec-aware -----------------------------


def test_lifecycle_decode_is_codec_aware() -> None:
    import dataclasses

    from temporalio.contrib.pydantic import pydantic_data_converter

    from typeflux.lifecycle import _decode_payloads

    codec = _codec()
    values = ["approve"]
    plaintext_payloads = pydantic_data_converter.payload_converter.to_payloads(values)
    encrypted = asyncio.run(codec.encode(plaintext_payloads))
    converter = dataclasses.replace(pydantic_data_converter, payload_codec=codec)

    class _Attrs:
        class input:  # noqa: N801 - mimics the proto attrs shape
            payloads = encrypted

    decoded = _decode_payloads(_Attrs, data_converter=converter)
    assert decoded == values
