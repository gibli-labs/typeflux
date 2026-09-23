"""Cross-run cache: a pluggable CacheStore + the activity cache key (#398).

Memoizes an AI activity's validated LLM output across runs, keyed by the
language-neutral cache contract (``typeflux.contracts.cache``, #391). A
deployment registers a ``CacheStore`` via ``TypefluxWorker(cache_store=...)``;
the executor does check-before-call / write-after-full-acceptance (#745): the
pre-hook output is written only after the repair loop (schema parse + input-aware
``output_check``), the hook, and the moderation checkpoint all accepted it, and a
hit re-runs the ``output_check`` (a failing hit regenerates) then the
hook/moderation. Distinct from the provider-side prefix cache (#60).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from typeflux.contracts.cache import (
    CacheKey,
    cache_input_hash,
    cache_key_digest,
    cache_record,
)
from typeflux.core.artifacts import artifact_groups_cache_identity
from typeflux.core.contracts import CacheConfig

__all__ = [
    "CACHE_ERASURE_COVERAGE_CAVEAT",
    "CacheStore",
    "InMemoryCacheStore",
    "SubjectCacheErasureReport",
    "SubjectErasableCacheStore",
    "activity_cache_key",
    "build_cache_record",
    "cache_reads_bypassed",
    "erase_subject_from_cache",
]

# The honest coverage boundary of a subject-scoped cache erasure (#715 slice 3).
# The write-time index only sees records that carried a ``subjects`` field when
# they were written (slice 1 made that field real). Entries written BEFORE
# subject plumbing, or by a workflow that touched this subject's data through a
# path that did not declare the subject, carry no subject and are invisible here;
# erasing them requires a full store flush. Mirrors the design's audit-honesty
# requirement (docs/design/design-erasure-715.md §4.3, §7 warnings).
CACHE_ERASURE_COVERAGE_CAVEAT = (
    "cache erasure only covers records written with a subject index (#715 slice 1+): "
    "entries written before subject plumbing, or by workflows that touched this "
    "subject's data through an un-declared path, carry no subject and are invisible "
    "to a per-subject erase. A full store flush is the only way to guarantee their "
    "removal."
)


@dataclass(frozen=True)
class SubjectCacheErasureReport:
    """Result of a subject-scoped cache erasure (#715 slice 3).

    The store-level erasure primitive's report; the slice-5 ``ErasureReceipt`` folds
    it into the cross-surface audit record. Carries ids/counts only — ``key_digests``
    are opaque ``sha256`` cache-key hashes (NOT sensitive, so they are included for
    audit) and never the cached output or any subject PII.

    ``dry_run`` reports what WOULD be deleted without mutating the store, so
    ``keys_deleted`` is ``0`` on a dry run. ``supported`` is ``False`` when the store
    lacks the :class:`SubjectErasableCacheStore` capability — then
    ``full_flush_fallback`` names the blunt fallback and the counts are ``0`` because
    the store has no subject index to consult. ``warnings`` always carries the
    :data:`CACHE_ERASURE_COVERAGE_CAVEAT` so a receipt never over-claims completeness.
    """

    subject_id: str
    dry_run: bool
    store_class: str
    supported: bool = True
    keys_found: int = 0
    keys_deleted: int = 0
    key_digests: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    full_flush_fallback: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "subject_id": self.subject_id,
            "dry_run": self.dry_run,
            "store_class": self.store_class,
            "supported": self.supported,
            "keys_found": self.keys_found,
            "keys_deleted": self.keys_deleted,
            "key_digests": list(self.key_digests),
            "failures": list(self.failures),
            "warnings": list(self.warnings),
        }
        if self.full_flush_fallback is not None:
            result["full_flush_fallback"] = self.full_flush_fallback
        return result

    @classmethod
    def not_supported(
        cls, store: object, subject_id: str, *, dry_run: bool
    ) -> SubjectCacheErasureReport:
        """A clear not-supported outcome naming the store class + the flush fallback.

        A plain :class:`CacheStore` without the erase capability cannot do
        per-subject invalidation (opaque digests, no reverse index), so the erase op
        reports this instead of silently succeeding. The fallback is documented,
        never automatic — the operator chooses to flush the whole store or accept
        stale-but-inert entries (a record holds validated output, not raw PII).
        """

        store_class = type(store).__qualname__
        return cls(
            subject_id=subject_id,
            dry_run=dry_run,
            store_class=store_class,
            supported=False,
            full_flush_fallback=(
                f"{store_class} has no subject index (does not implement "
                "SubjectErasableCacheStore); per-subject cache invalidation is "
                "unavailable. Flush the whole store to erase this subject's entries, "
                "or accept stale-but-inert cached outputs."
            ),
            warnings=(CACHE_ERASURE_COVERAGE_CAVEAT,),
        )


@runtime_checkable
class CacheStore(Protocol):
    """Cross-run store for memoized AI-activity outputs.

    Records are the ``CacheRecord`` dicts from the #391 contract. Implementations
    (in-memory, SQL, an adopter's LLM cache) back the same two methods; ``set`` is
    an idempotent upsert keyed by the cache key.

    Both methods run inline on the executor's calling thread (including the async
    executor's event loop), so they must be fast / non-blocking — back a remote
    store behind a quick lookup rather than doing slow network I/O here.
    """

    def get(self, key: CacheKey) -> dict[str, Any] | None: ...

    def set(self, key: CacheKey, record: dict[str, Any]) -> None: ...


@runtime_checkable
class SubjectErasableCacheStore(Protocol):
    """Optional :class:`CacheStore` capability: per-subject invalidation (#715 slice 3).

    A store opts in by implementing ``erase_subject`` alongside ``get``/``set`` and
    maintaining a subject→key index at write time from each record's ``subjects``
    field (slice 1 made that field real). Capability-detected via ``isinstance``
    (``runtime_checkable``); drive it through :func:`erase_subject_from_cache`, which
    returns a not-supported report naming the store class + the full-flush fallback
    for a plain store. Mirrors the opt-in ``backend_filter`` capability pattern.

    ``erase_subject`` with ``dry_run=True`` reports the affected keys WITHOUT deleting;
    ``dry_run=False`` deletes and reports what was removed.

    **Thread-safety contract**: implementors MUST make ``erase_subject`` atomic with
    respect to ``set``. Both are compound operations over the record store and the
    subject index (unindex-old → write → reindex on ``set``; read-index → delete →
    prune on erase), and stores are called from real OS threads (sync providers run
    via ``asyncio.to_thread``; map fan-outs run concurrently). An erase interleaved
    with an overwrite of the same key can otherwise delete a fresh record belonging
    to a DIFFERENT subject while reporting a clean erase of the requested one.
    """

    def get(self, key: CacheKey) -> dict[str, Any] | None: ...

    def set(self, key: CacheKey, record: dict[str, Any]) -> None: ...

    def erase_subject(self, subject_id: str, *, dry_run: bool) -> SubjectCacheErasureReport: ...


def erase_subject_from_cache(
    store: object, subject_id: str, *, dry_run: bool
) -> SubjectCacheErasureReport:
    """Erase a subject's cache entries if the store supports it; else report not-supported.

    Capability-detects :class:`SubjectErasableCacheStore` and delegates, or returns
    :meth:`SubjectCacheErasureReport.not_supported` naming the store class + the
    documented full-flush fallback for a plain store.
    """

    if isinstance(store, SubjectErasableCacheStore):
        return store.erase_subject(subject_id, dry_run=dry_run)
    return SubjectCacheErasureReport.not_supported(store, subject_id, dry_run=dry_run)


class InMemoryCacheStore:
    """Process-local reference CacheStore (a dict keyed by the flat key digest).

    For tests and single-process use; real deployments back the same protocol
    with Postgres/Redis.

    Implements the :class:`SubjectErasableCacheStore` capability via a write-time
    subject→digest index (#715 slice 3): ``set`` indexes each record under every id
    in its ``subjects`` field, re-indexing on overwrite so a stale subject entry can
    never leak, and ``erase_subject`` consults the index for dry-run/execute erasure.
    Records without ``subjects`` are never indexed — correct: they carry no subject
    data, so a per-subject erase leaves them untouched (a subjects-less record is
    either pre-slice-1 or from a workflow that declared no subject for this call).

    **Thread-safety**: ``set`` and ``erase_subject`` are compound operations over the
    records dict and the subject index, and cache stores are called from real OS
    threads (sync providers via ``asyncio.to_thread``; concurrent map items). A single
    lock is held across each WHOLE operation, so an erase can never interleave with an
    overwrite of the same key and pop a fresh record written for a different subject.
    ``get`` stays lock-free: it is a single atomic dict read of a record that is
    treated as immutable once stored (records are replaced wholesale, never mutated
    in place).
    """

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        # subject id -> set of key digests written under that subject.
        self._subject_index: dict[str, set[str]] = {}
        # Guards the compound set() (unindex-old → write → reindex) and
        # erase_subject() (read-index → delete → prune) sections as atomic units.
        self._lock = threading.Lock()

    def get(self, key: CacheKey) -> dict[str, Any] | None:
        # Lock-free: a single atomic dict read; stored records are immutable.
        return self._records.get(cache_key_digest(key))

    def set(self, key: CacheKey, record: dict[str, Any]) -> None:
        digest = cache_key_digest(key)
        with self._lock:
            # Overwrite: drop the prior record's subject entries first, so re-writing
            # a key under a different subject set never leaves a stale index pointer.
            prior = self._records.get(digest)
            if prior is not None:
                self._unindex(digest, prior.get("subjects", ()))
            self._records[digest] = record
            for subject_id in record.get("subjects", ()):
                self._subject_index.setdefault(subject_id, set()).add(digest)

    def erase_subject(self, subject_id: str, *, dry_run: bool) -> SubjectCacheErasureReport:
        store_class = type(self).__qualname__
        with self._lock:
            digests = sorted(self._subject_index.get(subject_id, set()))
            if dry_run:
                return SubjectCacheErasureReport(
                    subject_id=subject_id,
                    dry_run=True,
                    store_class=store_class,
                    keys_found=len(digests),
                    keys_deleted=0,
                    key_digests=tuple(digests),
                    warnings=(CACHE_ERASURE_COVERAGE_CAVEAT,),
                )
            deleted = 0
            for digest in digests:
                record = self._records.pop(digest, None)
                if record is None:
                    continue
                deleted += 1
                # A record may carry several subjects; deleting it erases the shared
                # entry, so prune the digest from EVERY subject it was indexed under
                # (not just this one) to keep the index consistent.
                self._unindex(digest, record.get("subjects", ()))
        return SubjectCacheErasureReport(
            subject_id=subject_id,
            dry_run=False,
            store_class=store_class,
            keys_found=len(digests),
            keys_deleted=deleted,
            key_digests=tuple(digests),
            warnings=(CACHE_ERASURE_COVERAGE_CAVEAT,),
        )

    def _unindex(self, digest: str, subjects: Any) -> None:
        """Remove ``digest`` from each subject's index set, dropping emptied sets."""
        for subject_id in subjects:
            keys = self._subject_index.get(subject_id)
            if keys is None:
                continue
            keys.discard(digest)
            if not keys:
                self._subject_index.pop(subject_id, None)


def activity_cache_key(
    prepared: Any,
    tenant: Mapping[str, str],
    cached_session: Any = None,
) -> CacheKey:
    """Build the cache key for a prepared activity execution.

    Reuses the frozen execution-manifest hashes (#390) and the #391 input-hash
    recipe, so identical input + prompt + behavior params + scope hit the cache.
    The key also folds in what the rendered messages do NOT capture but which
    still determines the output: the resolved model + temperature (``behavior_dict``
    drops them by default), the provider session identity (reference-cached
    artifacts live in the session), and the resolved ATTACHED artifacts' identity
    (#504 — an artifact part renders as only its group name + preamble text, so
    swapping the underlying bytes would otherwise serve a stale cached output).
    """

    execution_manifest = prepared.initial_execution_manifest
    behavior = dict(prepared.provider_params.behavior_dict())
    behavior["__model"] = prepared.provider_model
    behavior["__temperature"] = prepared.provider_params.temperature
    if cached_session is not None:
        behavior["__session_identity"] = cached_session.identity_hash
    input_hash = cache_input_hash(
        activity=prepared.activity.name,
        input_schema_hash=execution_manifest.input_schema_hash,
        rendered_messages_hash=execution_manifest.rendered_messages_hash,
        provider_params=behavior,
        artifacts=artifact_groups_cache_identity(prepared.artifacts),
    )
    return CacheKey(activity=prepared.activity.name, input_hash=input_hash, scope=dict(tenant))


def build_cache_record(key: CacheKey, output: BaseModel, prepared: Any) -> dict[str, Any]:
    """Build the CacheRecord dict to store for a freshly validated output.

    Carries the execution's subject id(s) (#715 slice 1) onto the record — sourced
    from the invocation context's ``subject_ids`` — so a per-subject invalidation
    can later find every entry a subject's data produced. Subjects live on the
    record, not the key, so the cache-key digest is unchanged (see ``cache_record``).
    """

    execution_manifest = prepared.initial_execution_manifest
    invocation_context = getattr(prepared, "invocation_context", None)
    subjects = getattr(invocation_context, "subject_ids", ()) if invocation_context else ()
    return cache_record(
        key=key,
        output=output.model_dump(mode="json"),
        created_at=datetime.now(UTC).isoformat(),
        output_schema_hash=execution_manifest.output_schema_hash,
        manifest_hash=execution_manifest.manifest_hash,
        subjects=subjects,
    )


def cache_reads_bypassed(config: CacheConfig) -> bool:
    """True when the activity's ``bypass_reads_env`` is set in the environment —
    cache reads are skipped (writes still happen) to force regeneration."""

    return bool(config.bypass_reads_env) and config.bypass_reads_env in os.environ
