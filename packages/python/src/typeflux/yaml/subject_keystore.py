"""Per-subject crypto-shred keystore + subject-scoped codec seam (#715 slice 4).

The Temporal payload codec (``payload_codec.py``) is static per-worker and
CONTENT-BLIND: its ``encode`` seals every payload under one shared ``current``
key, so crypto-shred there is all-or-nothing per key id, never per subject. This
module makes per-subject shred real, per the ratified §4.1 decision (option
**ii**: per-subject KEYSTORE KEY RECORDS):

* a :class:`SubjectKeystore` maps ``subject_id -> 32-byte data-key record``,
  minted on first use, with :meth:`SubjectKeystore.destroy_subject_key` as the
  erasure primitive — destroying a record renders that subject's ciphertext
  permanently unreadable (workflow-id-independent, survives Temporal backups);
* a :class:`SubjectScopedPayloadCodec` wraps the shared-key codec and resolves
  the OWNING EXECUTION's subject ids per encode, sealing subject-scoped payloads
  under a key DERIVED FROM the execution's per-subject records so destroying ANY
  member subject's record shreds the payload.

**How the codec learns the execution (the wired seam).** The Temporal SDK's
serialization contexts (``temporalio.converter.WithSerializationContext``) hand
the codec the owning execution's ``workflow_id`` on every standard client and
worker path — the SDK calls ``with_context`` before each operation. The bound
codec then resolves ``workflow_id -> subject ids`` through
:class:`SubjectKeyBindings`:

1. the **registry** — the runtime start path registers the ids it stamps into
   ``TypefluxSubjectIds`` (including an explicit "no subjects" entry) at the
   moment it starts the execution;
2. the **visibility fallback** — a registry miss (another process started the
   execution) describes the execution and reads the ``TypefluxSubjectIds``
   search attribute, the same index erasure enumerates; cached per workflow id;
3. anything else **fails closed** — no context, no binding, and no client to
   ask means subject scoping cannot be honored, and the codec raises rather
   than silently sealing a subject's payload under the shared key.

Why keystore records, not HKDF-derived keys (option **i**, REJECTED): a key
derived as ``HKDF(master, info=subject_id)`` cannot be single-subject-destroyed —
the master survives, so the derived key is always re-derivable; "erasing" it means
rotating the master and re-encrypting every survivor. The combine here takes REAL
keystore record keys as inputs (:func:`combine_subject_key`), not a master, so
destroying a record permanently removes an input to the SHA-256 combine and the
sealing key is unreconstructable. (Option **iii**, ``DeleteWorkflowExecution``, is
the closed-execution complement, implemented in ``project/erase_executions.py``.)

Fail-closed discipline (both the shred and the codec inherit #188's posture):

* a DESTROYED subject fails closed on encode — the keystore never mints a fresh
  key for it, so a shred is permanent and a post-erasure execution cannot silently
  resurrect the subject's readable channel;
* decode of a subject-scoped payload whose record is destroyed raises the DISTINCT
  :class:`SubjectKeyShreddedError` (naming the shred), never plaintext, never an
  empty result;
* subject ids MUST NOT be recycled after erasure — a tombstone is permanent, so a
  reused surrogate id fails closed forever (correct: reuse would conflate two
  people under one key).

**Deployment boundary (documented, not silent):** the reference
:class:`InMemorySubjectKeystore` and the binding registry are PROCESS-LOCAL. A
starter and a worker in different processes with independent in-memory keystores
mint DIFFERENT keys for the same subject — cross-process deployments MUST inject
a shared :class:`SubjectKeystore` backend (``build_runtime(subject_keystore=...)``).
Subject bindings, by contrast, resolve cross-process out of the box via the
visibility fallback. Sub-workflow composition is NOT yet supported under
``subject_scope`` (a child's input is encoded before the child exists, so its
binding cannot be resolved); the runtime rejects the combination at build time.

The subject-key wire scheme (kid format + the SHA-256 combine) is byte-pinned in
``contracts/temporal-binding/binding.v1.json`` so a record shredded by one edition
is unreadable in the other.
"""

from __future__ import annotations

import base64
import hashlib
import os
import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from temporalio.converter import (
    ActivitySerializationContext,
    PayloadCodec,
    SerializationContext,
    WithSerializationContext,
    WorkflowSerializationContext,
)

from typeflux.yaml.payload_codec import (
    ENCRYPTED_ENCODING,
    KEY_ID_METADATA_KEY,
    KEY_LEN,
    NONCE_LEN,
    RESERVED_KID_PREFIX,
    PayloadCodecError,
    TypefluxAesGcmPayloadCodec,
    open_payload_bytes,
    seal_payload_bytes,
)

if TYPE_CHECKING:
    from temporalio.api.common.v1 import Payload

#: The three states a subject's key record can be in — the return of the NON-MINTING
#: introspection the erasure dry-run reads (#715 slice 5). ``live`` = a real key exists;
#: ``destroyed`` = a permanent shred tombstone; ``absent`` = never keyed.
SubjectKeyState = Literal["live", "destroyed", "absent"]

__all__ = [
    "InMemorySubjectKeystore",
    "SubjectKeyBindings",
    "SubjectKeyDestructionResult",
    "SubjectKeyShreddedError",
    "SubjectKeyState",
    "SubjectKeyUnknownError",
    "SubjectKeystore",
    "SubjectScopedPayloadCodec",
    "combine_subject_key",
    "parse_subject_kid",
    "subject_key_state",
    "subject_kid",
]


# --- Errors ---------------------------------------------------------------------------


class SubjectKeyShreddedError(PayloadCodecError):
    """Decode/encode hit a subject whose key record has been SHREDDED (#715 slice 4).

    A DISTINCT, catchable error (subclass of :class:`PayloadCodecError`, so existing
    fail-closed ``except PayloadCodecError`` handlers still catch it) that NAMES the
    shred rather than surfacing as a generic crypto/auth failure. Its presence means
    the subject was erased: the ciphertext is intact but permanently unreadable —
    never a plaintext passthrough, never a silent empty.
    """

    def __init__(self, subject_id: str) -> None:
        self.subject_id = subject_id
        super().__init__(
            f"subject {subject_id!r} key record has been shredded (erased, #715): its "
            "payloads are permanently unreadable — this is the crypto-shred, not a "
            "transient crypto failure"
        )


class SubjectKeyUnknownError(PayloadCodecError):
    """A subject-scoped decode named a subject with NO key record (never created).

    Distinct from :class:`SubjectKeyShreddedError` (which means erased): this means
    the keystore has no memory of the subject at all — a wrong/foreign keystore (e.g.
    a process-local in-memory keystore that did not mint the key), or a payload from
    a different deployment. Still fail-closed (never plaintext), but a different
    diagnosis than an intentional shred.
    """

    def __init__(self, subject_id: str) -> None:
        self.subject_id = subject_id
        super().__init__(
            f"no key record for subject {subject_id!r} (never created and not a shred); "
            "cannot decode a payload sealed for it — if this process is not the one that "
            "minted the key, configure a SHARED SubjectKeystore backend (the in-memory "
            "reference keystore is process-local)"
        )


# --- The keystore seam ----------------------------------------------------------------


@runtime_checkable
class SubjectKeystore(Protocol):
    """Maps ``subject_id -> 32-byte data-key record`` with a real single-subject destroy.

    The crypto-shred primitive. A production backend (KMS-wrapped, Postgres, Vault)
    implements the same two methods; :class:`InMemorySubjectKeystore` is the reference
    — and is PROCESS-LOCAL, so it is a test/dev backend only (see the module docstring's
    deployment boundary).

    Contract:

    * :meth:`data_key` with ``create=True`` (encode path) mints a fresh 32-byte key on
      first use and returns the existing one thereafter — but a DESTROYED subject
      raises :class:`SubjectKeyShreddedError` (never mints a fresh key: a shred is
      permanent).
    * :meth:`data_key` with ``create=False`` (decode path) returns the live key, raises
      :class:`SubjectKeyShreddedError` for a destroyed subject, and
      :class:`SubjectKeyUnknownError` for one that was never created.
    * :meth:`destroy_subject_key` drops the key bytes and leaves a permanent tombstone;
      idempotent. Destroyed keys are UNRECOVERABLE through this API — no method ever
      returns (or re-mints) a destroyed subject's key material.

    An OPTIONAL :meth:`subject_key_state` gives the erasure dry-run a NON-MINTING,
    NON-DESTROYING existence probe (#715 slice 5): a shred plan must report whether a
    record would be destroyed WITHOUT minting one (mint-on-first-use is an encode-path
    concern) and without leaving a tombstone. A backend that does not implement it is
    still driven correctly — :func:`subject_key_state` falls back to a read-only
    ``data_key(create=False)`` probe — so it is a convenience, not a hard requirement.
    """

    def data_key(self, subject_id: str, *, create: bool) -> bytes: ...

    def destroy_subject_key(self, subject_id: str) -> SubjectKeyDestructionResult: ...


class SubjectKeyDestructionResult:
    """Outcome of a :meth:`SubjectKeystore.destroy_subject_key` call (#715 slice 4).

    ``key_existed`` is True when a LIVE key was actually shredded (versus a subject
    that was never keyed, or already destroyed — both leave a tombstone but shred no
    fresh material). ``already_destroyed`` marks an idempotent repeat. The erasure
    receipt (slice 5) folds these into its ``shredded_key_records`` count.
    """

    __slots__ = ("already_destroyed", "key_existed", "subject_id")

    def __init__(self, subject_id: str, *, key_existed: bool, already_destroyed: bool) -> None:
        self.subject_id = subject_id
        self.key_existed = key_existed
        self.already_destroyed = already_destroyed

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"SubjectKeyDestructionResult(subject_id={self.subject_id!r}, "
            f"key_existed={self.key_existed}, already_destroyed={self.already_destroyed})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SubjectKeyDestructionResult):
            return NotImplemented
        return (
            self.subject_id == other.subject_id
            and self.key_existed == other.key_existed
            and self.already_destroyed == other.already_destroyed
        )


class InMemorySubjectKeystore:
    """Process-local reference :class:`SubjectKeystore`.

    For tests and single-process use ONLY: two processes each holding an in-memory
    keystore mint DIFFERENT keys for the same subject, so any cross-process deployment
    must back the same protocol with a shared store (KMS/Vault/Postgres).

    One map holds both live records and tombstones — ``bytes`` is a live key, ``None``
    is a destroyed subject's PERMANENT tombstone — so the two states can never desync
    under future mutation paths. A destroyed subject retains NO key bytes: the destroy
    is unrecoverable through this API, and a later encode for the subject fails closed
    instead of minting a fresh key.

    **Thread-safety**: mint-on-first-use, lookup, and destroy are compound operations
    over the record map, and a codec runs on real worker threads (Temporal's data
    converter encodes concurrently). A single lock guards each whole operation so a
    destroy can never interleave with a concurrent first-use mint and leave a live key
    for an erased subject.
    """

    def __init__(self) -> None:
        # subject id -> live 32-byte key, or None = destroyed (permanent tombstone).
        self._records: dict[str, bytes | None] = {}
        self._lock = threading.Lock()

    def data_key(self, subject_id: str, *, create: bool) -> bytes:
        if not subject_id:
            # Fail closed: an empty subject id would collide the whole "no subject"
            # namespace onto one record — never a silent mint.
            raise PayloadCodecError("subject id for a data key must be a non-empty string")
        with self._lock:
            if subject_id in self._records:
                existing = self._records[subject_id]
                if existing is None:
                    raise SubjectKeyShreddedError(subject_id)
                return existing
            if not create:
                raise SubjectKeyUnknownError(subject_id)
            key = os.urandom(KEY_LEN)
            self._records[subject_id] = key
            return key

    def destroy_subject_key(self, subject_id: str) -> SubjectKeyDestructionResult:
        if not subject_id:
            raise PayloadCodecError("subject id to destroy must be a non-empty string")
        with self._lock:
            existing = self._records.get(subject_id)
            already_destroyed = subject_id in self._records and existing is None
            key_existed = existing is not None
            # Drop the key BYTES (unrecoverable) and leave a permanent tombstone so a
            # future encode fails closed rather than re-minting the erased subject.
            self._records[subject_id] = None
            return SubjectKeyDestructionResult(
                subject_id,
                key_existed=key_existed,
                already_destroyed=already_destroyed,
            )

    def subject_key_state(self, subject_id: str) -> SubjectKeyState:
        """NON-MINTING, NON-DESTROYING existence probe for the erasure dry-run (#715 slice 5).

        Returns ``"live"`` / ``"destroyed"`` / ``"absent"`` by reading the record map
        under the lock WITHOUT ever creating a record or a tombstone — a dry run must
        never mint (mint-on-first-use is encode-only) nor destroy (that is the execute
        path). ``"destroyed"`` and ``"absent"`` differ so the plan distinguishes an
        already-shredded subject from one that was never keyed."""

        if not subject_id:
            raise PayloadCodecError("subject id to probe must be a non-empty string")
        with self._lock:
            if subject_id not in self._records:
                return "absent"
            return "destroyed" if self._records[subject_id] is None else "live"

    def seed_record_for_test(self, subject_id: str, key: bytes) -> None:
        """Test-only: seed a record so a codec seals bytes matching a pinned vector.

        NOT part of the :class:`SubjectKeystore` protocol."""

        self._records[subject_id] = bytes(key)


def subject_key_state(keystore: SubjectKeystore, subject_id: str) -> SubjectKeyState:
    """Probe a keystore for a subject's key state WITHOUT minting or destroying (#715 slice 5).

    Prefers a backend's own :meth:`SubjectKeystore.subject_key_state` (the reference
    :class:`InMemorySubjectKeystore` implements it); a backend that predates that method
    falls back to a read-only ``data_key(subject_id, create=False)`` probe — which never
    mints (``create=False``) and never destroys — mapping its fail-closed typed errors
    to the three states. Either way a dry run leaves the keystore byte-for-byte unchanged.
    """

    probe = getattr(keystore, "subject_key_state", None)
    if callable(probe):
        state: SubjectKeyState = probe(subject_id)
        return state
    try:
        keystore.data_key(subject_id, create=False)
    except SubjectKeyShreddedError:
        return "destroyed"
    except SubjectKeyUnknownError:
        return "absent"
    return "live"


# --- The subject-key wire scheme (byte-pinned in binding.v1.json) ---------------------


def subject_kid(subject_ids: Sequence[str]) -> str:
    """Build the subject-scoped ``typeflux-key-id`` for an execution's subject set.

    Format ``tfsubj1:<b64url(s1)>,<b64url(s2)>,...`` — the scheme marker, then each
    subject id base64url-encoded (no padding, so it is delimiter-safe even if the id
    contains ``:`` or ``,``), comma-joined in the resolved order (primary first). The
    full set is embedded so decode can re-fetch every member record and reconstruct the
    combined key. Both editions build this identically.
    """

    if not subject_ids:
        raise PayloadCodecError("cannot build a subject-scoped key id for an empty subject set")
    parts = [_b64url_no_pad(subject_id) for subject_id in subject_ids]
    return RESERVED_KID_PREFIX + ",".join(parts)


def _b64url_no_pad(subject_id: str) -> str:
    return base64.urlsafe_b64encode(subject_id.encode("utf-8")).rstrip(b"=").decode("ascii")


def parse_subject_kid(kid: str) -> tuple[str, ...]:
    """Parse a subject-scoped ``typeflux-key-id`` back to its ordered subject set.

    Raises :class:`PayloadCodecError` (fail-closed) for a kid that does not carry the
    scheme marker or whose body is malformed. CANONICAL segments only: each segment
    must re-encode to exactly itself (rejecting ``=``-padded, standard-alphabet, or
    otherwise non-canonical encodings the lenient decoder would accept), matching the
    TS parser byte-for-byte — kid metadata is unauthenticated, so both editions must
    agree on exactly one accepted encoding per subject set.
    """

    if not kid.startswith(RESERVED_KID_PREFIX):
        raise PayloadCodecError(f"key id {kid!r} is not a subject-scoped key id")
    body = kid[len(RESERVED_KID_PREFIX) :]
    if not body:
        raise PayloadCodecError(f"subject-scoped key id {kid!r} names no subjects")
    subjects: list[str] = []
    for part in body.split(","):
        if not part:
            raise PayloadCodecError(f"subject-scoped key id {kid!r} has an empty subject segment")
        padding = "=" * (-len(part) % 4)
        try:
            decoded = base64.urlsafe_b64decode(part + padding).decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - any decode failure is fail-closed.
            raise PayloadCodecError(
                f"subject-scoped key id {kid!r} has an undecodable subject segment {part!r}"
            ) from exc
        # Canonical-form check: the lenient decoder accepts padded / standard-alphabet
        # segments; re-encoding must reproduce the segment EXACTLY or it is rejected.
        if _b64url_no_pad(decoded) != part:
            raise PayloadCodecError(
                f"subject-scoped key id {kid!r} has a non-canonical subject segment {part!r}"
            )
        subjects.append(decoded)
    return tuple(subjects)


def combine_subject_key(record_keys: Sequence[bytes]) -> bytes:
    """Combine an execution's per-subject record keys into ONE 32-byte AES key.

    ``SHA-256(key(s1) || key(s2) || ...)`` over the 32-byte record keys in the kid's
    order. The inputs are REAL keystore records (not a re-derivable master), so
    destroying ANY member record permanently removes an input and the combined key is
    unreconstructable — giving the erasure AND-semantics a mixed-subject execution
    needs: destroying any single member subject shreds the shared payload. A
    single-subject execution is the degenerate case (``SHA-256(key(s1))``).
    """

    if not record_keys:
        raise PayloadCodecError("cannot combine an empty set of subject key records")
    hasher = hashlib.sha256()
    for record_key in record_keys:
        if len(record_key) != KEY_LEN:
            raise PayloadCodecError(
                f"a subject key record must be exactly {KEY_LEN} bytes (got {len(record_key)})"
            )
        hasher.update(record_key)
    return hasher.digest()


# --- Subject bindings: workflow_id -> subject ids -------------------------------------


class SubjectKeyBindings:
    """Resolves ``workflow_id -> subject ids`` for the subject-scoped codec.

    Two channels, in order:

    1. **Registry** — the runtime start path calls :meth:`register` with the SAME ids
       it stamps into ``TypefluxSubjectIds`` (an empty tuple pins "no subjects"), so
       an execution started by this process resolves without any network call — and
       BEFORE it exists in visibility (the start input is encoded first).
    2. **Visibility fallback** — a registry miss describes the execution through the
       late-bound Temporal client and reads its ``TypefluxSubjectIds`` search
       attribute (the erasure index itself), caching the answer. This is how a worker
       process resolves executions another process started.

    A miss with no bound client, or a describe failure, FAILS CLOSED — subject scoping
    is never silently skipped for an execution whose subjects cannot be determined.

    **Thread-safety**: the registry/cache is guarded by a lock (codec encodes run on
    worker threads); ``bind_client`` is a single reference assignment.
    """

    def __init__(self) -> None:
        self._known: dict[str, tuple[str, ...]] = {}
        self._lock = threading.Lock()
        self._client: Any | None = None

    def register(self, workflow_id: str, subject_ids: Sequence[str]) -> None:
        """Pin an execution's subject set (called by the start path, pre-start)."""

        if not workflow_id:
            raise PayloadCodecError("cannot register a subject binding for an empty workflow id")
        from typeflux.core.subjects import normalize_subject_ids

        normalized = normalize_subject_ids(subject_ids)
        with self._lock:
            self._known[workflow_id] = normalized

    def bind_client(self, client: Any) -> None:
        """Late-bind the connected Temporal client for the visibility fallback."""

        self._client = client

    async def resolve(self, workflow_id: str) -> tuple[str, ...]:
        """Resolve an execution's subject ids: registry, then visibility, else raise."""

        with self._lock:
            known = self._known.get(workflow_id)
        if known is not None:
            return known
        client = self._client
        if client is None:
            raise PayloadCodecError(
                f"cannot resolve subject ids for workflow {workflow_id!r}: it was not "
                "registered by this process's start path and no Temporal client is bound "
                "for the visibility fallback — subject scoping fails closed rather than "
                "sealing under the shared key (#715 slice 4)"
            )
        try:
            description = await client.get_workflow_handle(workflow_id).describe()
        except Exception as exc:  # noqa: BLE001 - fail-closed, with the child-workflow hint.
            raise PayloadCodecError(
                f"cannot resolve subject ids for workflow {workflow_id!r}: the visibility "
                f"describe failed ({type(exc).__name__}). If this is a child workflow being "
                "scheduled, note that sub-workflow composition is not yet supported under "
                "payload_codec.subject_scope (#715 slice 4)"
            ) from exc
        subjects = _described_subject_ids(description)
        with self._lock:
            self._known[workflow_id] = subjects
        return subjects


def _described_subject_ids(description: Any) -> tuple[str, ...]:
    """Read ``TypefluxSubjectIds`` off a workflow describe result; absent ⇒ no subjects."""

    from temporalio.common import SearchAttributeKey

    from typeflux.core.subjects import SUBJECT_IDS_SEARCH_ATTRIBUTE

    typed = getattr(description, "typed_search_attributes", None)
    if typed is None:
        return ()
    try:
        value = typed.get(SearchAttributeKey.for_keyword_list(SUBJECT_IDS_SEARCH_ATTRIBUTE))
    except (KeyError, TypeError):
        return ()
    if not isinstance(value, (list, tuple)) or not value:
        return ()
    return tuple(str(item) for item in value)


# --- The subject-scoped codec ---------------------------------------------------------


class SubjectScopedPayloadCodec(PayloadCodec, WithSerializationContext):
    """A payload codec that seals subject-scoped executions under per-subject keys.

    Wraps the shared-key :class:`TypefluxAesGcmPayloadCodec`, a
    :class:`SubjectKeystore`, and a :class:`SubjectKeyBindings`. The Temporal SDK
    calls :meth:`with_context` before every standard encode/decode, handing this codec
    the owning execution's workflow id; per encode the bound codec resolves the
    execution's subject ids through the bindings:

    * **no subjects** → delegates to the shared-key codec, producing byte-for-byte the
      same wire bytes #188 produces today (the no-subject execution is provably
      unchanged — the conformance vectors pin it);
    * **one or more subjects** → mints/fetches each subject's record key, combines them
      (:func:`combine_subject_key`), and seals every payload of the call under that
      combined key with a ``tfsubj1:...`` subject kid;
    * **no serialization context** (an unbound encode) → FAILS CLOSED: without the
      owning execution the subject question is unanswerable, and guessing "shared key"
      could silently seal a subject's payload outside its shred domain.

    Decode routes on the kid prefix and needs NO binding resolution: subject-scoped
    payloads re-fetch the member records named by the kid (raising
    :class:`SubjectKeyShreddedError` if any was erased) and open under the combined
    key; every other payload (shared kid, or an unencrypted passthrough) goes to the
    wrapped shared-key codec.
    """

    def __init__(
        self,
        base_codec: TypefluxAesGcmPayloadCodec,
        keystore: SubjectKeystore,
        *,
        bindings: SubjectKeyBindings | None = None,
        _workflow_id: str | None = None,
    ) -> None:
        self._base = base_codec
        self._keystore = keystore
        self._bindings = bindings if bindings is not None else SubjectKeyBindings()
        self._workflow_id = _workflow_id

    @property
    def bindings(self) -> SubjectKeyBindings:
        """The shared binding registry (register/bind_client live here)."""

        return self._bindings

    @property
    def keystore(self) -> SubjectKeystore:
        """The keystore backing this codec (the erasure primitive's home)."""

        return self._keystore

    def with_context(self, context: SerializationContext) -> SubjectScopedPayloadCodec:
        """SDK seam: bind a copy to the context's owning workflow id.

        Copies SHARE the base codec, keystore, and bindings (registry/cache/client);
        only the bound workflow id differs. A context without a workflow id (e.g. an
        activity context outside a workflow) yields an unbound copy, whose encode
        fails closed.
        """

        workflow_id: str | None = None
        if isinstance(context, WorkflowSerializationContext):
            workflow_id = context.workflow_id
        elif isinstance(context, ActivitySerializationContext):
            workflow_id = context.workflow_id
        return SubjectScopedPayloadCodec(
            self._base,
            self._keystore,
            bindings=self._bindings,
            _workflow_id=workflow_id,
        )

    # -- encode ------------------------------------------------------------------------

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        if self._workflow_id is None:
            # Fail closed: no serialization context ⇒ the owning execution (and so the
            # subject set) is unknowable; guessing shared-key could leak a subject's
            # payload outside its shred domain.
            raise PayloadCodecError(
                "subject-scoped payload codec cannot encode without a serialization "
                "context naming the owning execution (#715 slice 4); this encode path "
                "did not supply one"
            )
        subject_ids = await self._bindings.resolve(self._workflow_id)
        if not subject_ids:
            # No subjects: shared-key path, byte-identical to #188.
            return await self._base.encode(payloads)
        kid = subject_kid(subject_ids)
        combined = combine_subject_key(
            [self._keystore.data_key(subject_id, create=True) for subject_id in subject_ids]
        )
        return [
            seal_payload_bytes(
                key=combined,
                kid=kid,
                nonce=os.urandom(NONCE_LEN),
                plaintext=payload.SerializeToString(),
            )
            for payload in payloads
        ]

    # -- decode ------------------------------------------------------------------------

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return self.decode_sync(payloads)

    def decode_sync(self, payloads: Sequence[Payload]) -> list[Payload]:
        from temporalio.api.common.v1 import Payload

        decoded: list[Payload] = []
        # Per-CALL combined-key cache: a batch of N payloads under one kid costs one
        # keystore fetch + combine, not N (a real KMS-backed keystore pays a network
        # round trip per fetch). The cache does NOT outlive this call, so a destroy
        # between calls is always observed (destroy-safety preserved).
        combined_by_kid: dict[str, bytes] = {}
        for payload in payloads:
            kid_bytes = payload.metadata.get(KEY_ID_METADATA_KEY)
            is_subject_scoped = (
                payload.metadata.get("encoding") == ENCRYPTED_ENCODING
                and kid_bytes is not None
                and kid_bytes.decode("utf-8").startswith(RESERVED_KID_PREFIX)
            )
            if not is_subject_scoped:
                # Shared-key encrypted, or a pre-codec passthrough: the wrapped codec
                # applies its own fail-closed / passthrough discipline.
                decoded.append(self._base.decode_sync([payload])[0])
                continue
            assert kid_bytes is not None  # narrowed by is_subject_scoped
            kid = kid_bytes.decode("utf-8")
            combined = combined_by_kid.get(kid)
            if combined is None:
                subject_ids = parse_subject_kid(kid)
                # data_key(create=False) raises SubjectKeyShreddedError for an erased
                # subject (naming the shred) or SubjectKeyUnknownError for one never
                # created — either way fail-closed, never a plaintext passthrough,
                # never a silent empty.
                combined = combine_subject_key(
                    [
                        self._keystore.data_key(subject_id, create=False)
                        for subject_id in subject_ids
                    ]
                )
                combined_by_kid[kid] = combined
            inner = Payload()
            inner.ParseFromString(open_payload_bytes(key=combined, data=payload.data))
            decoded.append(inner)
        return decoded
