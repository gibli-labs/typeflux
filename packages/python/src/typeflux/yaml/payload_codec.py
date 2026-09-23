"""AES-256-GCM Temporal payload codec (#188 slice 1).

The codec encrypts the WHOLE serialized ``Payload`` proto (Temporal's reference-codec
pattern) so every workflow/activity IO value, review-signal payload, and inlined
artifact rides Temporal history as ciphertext. The wire layout is PINNED in
``contracts/temporal-binding/binding.v1.json`` and implemented identically in the TS
edition (``temporal-yaml/src/payload-codec.ts``) so a payload encrypted by one edition
decrypts in the other under the same key.

Wire layout (byte-for-byte, both editions):

* the encrypted ``Payload`` carries ``metadata["encoding"] = b"binary/encrypted"`` and
  ``metadata["typeflux-key-id"] = <kid utf-8 bytes>``;
* ``data = nonce(12 random bytes) || ciphertext || tag(16 bytes)`` where ``ciphertext``
  and ``tag`` are the AES-256-GCM sealing of the serialized inner ``Payload`` proto with
  NO associated data. cryptography's ``AESGCM.encrypt`` appends the 16-byte tag to the
  ciphertext, so ``ciphertext || tag`` is exactly its output.

Fail-closed discipline (D188-3): an UNKNOWN key id or an authentication-tag failure is a
hard error — the codec NEVER passes ciphertext through as plaintext. A payload with no
``binary/encrypted`` marker passes ``decode`` through untouched, so a codec reading
pre-codec history (or a mixed history) does not choke.

Secret discipline: key VALUES never cross a resolver wire, are never logged, and the key
slots join ``SECRET_SLOT_PATHS`` so bundles/plans mask them to source_kind/source_name.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from typeflux.yaml.secrets import (
    SecretValueFromSpec,
    SecretValueSpec,
    resolve_optional_secret_bytes,
)

if TYPE_CHECKING:
    from temporalio.api.common.v1 import Payload

# --- Pinned wire constants (identical in the TS edition) ------------------------------

#: ``metadata["encoding"]`` marker on an encrypted payload.
ENCRYPTED_ENCODING = b"binary/encrypted"
#: ``metadata`` key carrying the encrypting key's id (utf-8 bytes).
KEY_ID_METADATA_KEY = "typeflux-key-id"
#: AES-GCM nonce length, bytes. A FRESH CSPRNG value per encrypt — never reused.
NONCE_LEN = 12
#: AES-GCM authentication tag length, bytes.
TAG_LEN = 16
#: AES-256 key length, bytes.
KEY_LEN = 32

#: Wire scheme marker for a SUBJECT-scoped ``typeflux-key-id`` (#715 slice 4). A
#: subject-scoped payload's key id is ``tfsubj1:<b64url(s1)>,<b64url(s2)>,...``;
#: a shared-key (no-subject) payload keeps its plain configured kid, byte-for-byte
#: as before slice 4. The two kid namespaces are kept DISJOINT: a configured codec
#: key id may not start with :data:`RESERVED_KID_PREFIX`, so decode routes
#: unambiguously on the prefix. Byte-pinned in ``binding.v1.json``
#: (``payload_codec.subject_key_scheme``).
SUBJECT_KID_SCHEME = "tfsubj1"
#: The reserved prefix (``"tfsubj1:"``) a configured shared key id must NOT use.
RESERVED_KID_PREFIX = f"{SUBJECT_KID_SCHEME}:"


class PayloadCodecError(RuntimeError):
    """A codec load/encrypt/decrypt failure — always fail-closed, never a passthrough."""


# --- Spec models ----------------------------------------------------------------------


class PayloadCodecKeySpec(BaseModel):
    """One keyed-map entry: a stable id plus the secret reference to its 32-byte value."""

    model_config = ConfigDict(extra="forbid")

    id: str
    value_from: SecretValueFromSpec

    @model_validator(mode="after")
    def _validate_id(self) -> PayloadCodecKeySpec:
        if not self.id or self.id.strip() != self.id:
            raise ValueError(
                "runtime.temporal.payload_codec.keys[].id must be non-empty and trimmed"
            )
        if self.id.startswith(RESERVED_KID_PREFIX):
            # Keep the shared-key and subject-scoped kid namespaces disjoint so
            # decode routes unambiguously on the prefix (#715 slice 4).
            raise ValueError(
                f"runtime.temporal.payload_codec.keys[].id may not start with the reserved "
                f"subject-key prefix {RESERVED_KID_PREFIX!r} (it is the #715 subject-scoped "
                "key-id namespace)"
            )
        return self


class PayloadCodecSubjectScopeSpec(BaseModel):
    """``runtime.temporal.payload_codec.subject_scope`` — per-subject crypto-shred (#715 slice 4).

    Declaring this block turns ON subject-scoped sealing: an execution with subject ids
    (#715 slice 1) seals its payloads under a key derived from per-subject keystore
    records, so destroying a record crypto-shreds that subject's history. An execution
    with NO subjects keeps the shared-key behavior byte-for-byte.

    ``keystore`` names the reference backend. ``in_memory`` is PROCESS-LOCAL — a starter
    and a worker in different processes mint DIFFERENT keys for the same subject — so it
    is built by DEFAULT only on the sole-owner path (``build_runtime``, where one process
    holds both the starting client and the worker); every other path (control-plane
    connects, the ts-binding driver) FAILS CLOSED unless a shared ``SubjectKeystore``
    backend is injected. Split starter/worker deployments MUST inject a shared backend
    via ``build_runtime(subject_keystore=...)`` (see docs/privacy.md).
    """

    model_config = ConfigDict(extra="forbid")

    keystore: Literal["in_memory"] = "in_memory"


class PayloadCodecSpec(BaseModel):
    """``runtime.temporal.payload_codec`` — a keyed-map AES-256-GCM codec (rotation-first).

    Encrypt with ``current``; decrypt by reading the payload's key id and looking up any
    declared key (rotation: add a key, flip ``current``, old history still decrypts under
    its id). The codec is OFF unless this block is DECLARED. The optional
    ``subject_scope`` block layers per-subject crypto-shred on top (#715 slice 4).
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["aes"]
    current: str
    keys: list[PayloadCodecKeySpec]
    subject_scope: PayloadCodecSubjectScopeSpec | None = None

    @model_validator(mode="after")
    def _validate(self) -> PayloadCodecSpec:
        if not self.keys:
            raise ValueError("runtime.temporal.payload_codec of type aes requires at least one key")
        ids = [key.id for key in self.keys]
        if len(ids) != len(set(ids)):
            raise ValueError("runtime.temporal.payload_codec.keys ids must be unique")
        if self.current not in ids:
            raise ValueError("runtime.temporal.payload_codec.current must name a declared key id")
        return self


# --- AES-256-GCM primitives (byte-pinned, shared by codec + conformance vectors) ------


def _aesgcm(key: bytes) -> Any:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(key)


def seal_payload_bytes(*, key: bytes, kid: str, nonce: bytes, plaintext: bytes) -> Payload:
    """Seal a serialized inner ``Payload`` (``plaintext``) into an encrypted ``Payload``.

    ``nonce`` is supplied by the caller so this primitive is deterministic (the conformance
    vectors pin a fixed nonce); the production codec always passes a fresh CSPRNG nonce.
    """
    from temporalio.api.common.v1 import Payload

    if len(key) != KEY_LEN:
        raise PayloadCodecError("AES-256 key must be exactly 32 bytes")
    if len(nonce) != NONCE_LEN:
        raise PayloadCodecError("AES-GCM nonce must be exactly 12 bytes")
    ciphertext_and_tag = _aesgcm(key).encrypt(nonce, plaintext, None)
    return Payload(
        metadata={
            "encoding": ENCRYPTED_ENCODING,
            KEY_ID_METADATA_KEY: kid.encode("utf-8"),
        },
        data=nonce + ciphertext_and_tag,
    )


def open_payload_bytes(*, key: bytes, data: bytes) -> bytes:
    """Open the ``data`` (``nonce || ciphertext || tag``) of an encrypted payload.

    Returns the serialized inner ``Payload`` bytes. Raises ``PayloadCodecError`` on an
    authentication-tag failure (tampered ciphertext, wrong key) — never returns garbage.
    """
    if len(data) < NONCE_LEN + TAG_LEN:
        raise PayloadCodecError("encrypted payload data is too short to hold a nonce and tag")
    nonce, ciphertext_and_tag = data[:NONCE_LEN], data[NONCE_LEN:]
    try:
        return _aesgcm(key).decrypt(nonce, ciphertext_and_tag, None)
    except Exception as exc:  # noqa: BLE001 - any AEAD failure is fail-closed.
        raise PayloadCodecError(
            "encrypted payload failed authentication (wrong key or tampered)"
        ) from exc


# --- The composite codec --------------------------------------------------------------


class TypefluxAesGcmPayloadCodec:
    """The ONE composite AES-256-GCM codec both Python client sites wrap.

    Implements the ``temporalio.converter.PayloadCodec`` async ``encode``/``decode``
    surface; the crypto itself is synchronous, so ``decode_sync`` exposes the same
    decode for the synchronous lifecycle decode path.
    """

    def __init__(self, *, current_kid: str, keys: dict[str, bytes]) -> None:
        if current_kid not in keys:
            raise PayloadCodecError("payload codec current key id is not among the resolved keys")
        for kid, value in keys.items():
            if kid.startswith(RESERVED_KID_PREFIX):
                raise PayloadCodecError(
                    f"payload codec key id {kid!r} may not use the reserved subject-key prefix "
                    f"{RESERVED_KID_PREFIX!r} (#715 slice 4)"
                )
            if len(value) != KEY_LEN:
                # Never log the value; name + length only.
                raise PayloadCodecError(
                    f"payload codec key {kid!r} must be exactly {KEY_LEN} bytes (got {len(value)})"
                )
        self._current_kid = current_kid
        self._keys = dict(keys)

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        key = self._keys[self._current_kid]
        return [
            seal_payload_bytes(
                key=key,
                kid=self._current_kid,
                nonce=os.urandom(NONCE_LEN),
                plaintext=payload.SerializeToString(),
            )
            for payload in payloads
        ]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return self.decode_sync(payloads)

    def decode_sync(self, payloads: Sequence[Payload]) -> list[Payload]:
        from temporalio.api.common.v1 import Payload

        decoded: list[Payload] = []
        for payload in payloads:
            if payload.metadata.get("encoding") != ENCRYPTED_ENCODING:
                # Pre-codec / non-encrypted payload: pass through untouched.
                decoded.append(payload)
                continue
            kid_bytes = payload.metadata.get(KEY_ID_METADATA_KEY)
            if kid_bytes is None:
                raise PayloadCodecError("encrypted payload carries no typeflux-key-id")
            kid = kid_bytes.decode("utf-8")
            key = self._keys.get(kid)
            if key is None:
                # Unknown key id: fail closed, never passthrough.
                raise PayloadCodecError(f"no key registered for encrypted payload key id {kid!r}")
            inner = Payload()
            inner.ParseFromString(open_payload_bytes(key=key, data=payload.data))
            decoded.append(inner)
        return decoded


def build_payload_codec(
    spec: PayloadCodecSpec | None,
    *,
    env: Mapping[str, str] | None = None,
) -> TypefluxAesGcmPayloadCodec | None:
    """Build the codec from a resolved spec, or ``None`` when the block is absent.

    Fail-closed (D188-3): every declared key MUST resolve to exactly 32 bytes or this
    raises — a PII codec never silently degrades to plaintext. Key values are resolved
    via ``resolve_optional_secret_bytes`` and never logged.

    ``env`` overrides the process environment for env-source keys (default os.environ) so a
    caller can thread a project environment's ``variables`` layer in (binding_ts) without
    mutating the process env; the yaml/runtime.py call site keeps the os.environ default.
    """
    if spec is None:
        return None
    keys: dict[str, bytes] = {}
    for key_spec in spec.keys:
        runtime_path = f"runtime.temporal.payload_codec.keys[{key_spec.id}].value_from"
        resolved = resolve_optional_secret_bytes(
            SecretValueSpec(value_from=key_spec.value_from),
            runtime_path=runtime_path,
            env=env,
        )
        if resolved is None:
            raise PayloadCodecError(
                f"payload codec key {key_spec.id!r} did not resolve ({runtime_path})"
            )
        if len(resolved) != KEY_LEN:
            raise PayloadCodecError(
                f"payload codec key {key_spec.id!r} must be exactly {KEY_LEN} bytes "
                f"(got {len(resolved)})"
            )
        keys[key_spec.id] = resolved
    return TypefluxAesGcmPayloadCodec(current_kid=spec.current, keys=keys)


__all__ = [
    "ENCRYPTED_ENCODING",
    "KEY_ID_METADATA_KEY",
    "KEY_LEN",
    "NONCE_LEN",
    "RESERVED_KID_PREFIX",
    "SUBJECT_KID_SCHEME",
    "TAG_LEN",
    "PayloadCodecError",
    "PayloadCodecKeySpec",
    "PayloadCodecSpec",
    "PayloadCodecSubjectScopeSpec",
    "TypefluxAesGcmPayloadCodec",
    "build_payload_codec",
    "open_payload_bytes",
    "seal_payload_bytes",
]
