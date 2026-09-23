"""Cross-run cache key + record contract (#391).

Defines the key a cross-run ``CacheStore`` (E3, #398), adopter cache implementations, and
the TypeScript SDK all use to memoize an AI-activity result across runs. The
provider-side *prefix* cache (``session_cache_identity``, #60) is a separate
concern and is not involved here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from typeflux.manifests._common import canonical_json

__all__ = [
    "CacheKey",
    "cache_input_hash",
    "cache_key_digest",
    "cache_record",
]


@dataclass(frozen=True)
class CacheKey:
    """Identifies a cached AI-activity result.

    ``activity`` is the activity ("node") name; ``input_hash`` is the digest of
    the call-determining inputs (see :func:`cache_input_hash`); ``scope`` is a
    generic string map for tenancy/partitioning (e.g.
    ``{"company_id": ..., "product_id": ...}`` — empty means global). Keeping
    ``scope`` generic lets any deployment choose its partition keys while
    supporting the ``(node, input_hash, company_id, product_id)`` shape used by adopter caches.
    """

    activity: str
    input_hash: str
    scope: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "activity": self.activity,
            "input_hash": self.input_hash,
            "scope": dict(self.scope),
        }

    def digest(self) -> str:
        """A single stable key string for stores that want a flat key."""
        return cache_key_digest(self)


def cache_key_digest(key: CacheKey) -> str:
    """``sha256`` over the key's canonical JSON. ``canonical_json`` sorts keys,
    so the digest is independent of ``scope`` insertion order."""

    return sha256(canonical_json(key.to_dict()).encode("utf-8")).hexdigest()


def cache_input_hash(
    *,
    activity: str,
    input_schema_hash: str,
    rendered_messages_hash: str,
    provider_params: Mapping[str, Any] | None = None,
    artifacts: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Digest of the inputs that determine an AI-activity call.

    Reuses the frozen execution-manifest hashes (#390): ``rendered_messages_hash``
    encodes the prompt rendered with the input, so identical input + prompt +
    behavior params yield the same hash — one source of truth for "what
    determines a call". Mirrors an adopter cache's input hash (content hash of files
    + prompt + input).

    ``artifacts`` is the resolved-artifact identity (#504) in the
    ``artifact_groups_cache_identity`` shape (per artifact: group/index/
    source_kind/kind/media_type/role/sha256/size_bytes with ``None`` dropped,
    plus the SOURCE — url/uri/provider+file_id — whenever no sha256 pins the
    bytes; empty groups excluded). An artifact part RENDERS as only its group
    name + preamble text, so the rendered-messages hash cannot see the
    underlying bytes — without this fold, replacing a file (or the source
    location of an unhashed artifact) under the same group name would serve a
    stale cached output. Omitted from the payload when absent or empty, so keys
    for artifact-free calls (and all previously stored entries) are unchanged.
    """

    payload: dict[str, Any] = {
        "activity": activity,
        "input_schema_hash": input_schema_hash,
        "rendered_messages_hash": rendered_messages_hash,
        "provider_params": dict(provider_params) if provider_params else {},
    }
    if artifacts:
        payload["artifacts"] = [dict(group) for group in artifacts]
    return sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def cache_record(
    *,
    key: CacheKey,
    output: Any,
    created_at: str,
    output_schema_hash: str,
    manifest_hash: str | None = None,
    tokens_saved: int | None = None,
    subjects: Sequence[str] | None = None,
) -> dict[str, Any]:
    """A stored cache entry: the cached validated output plus provenance.

    ``output`` is the activity's validated output (JSON-serializable);
    ``created_at`` is an ISO-8601 timestamp; ``output_schema_hash`` ties the
    record to the output schema it was produced against (so a schema change can
    invalidate stale entries). ``manifest_hash`` / ``tokens_saved`` are optional
    provenance.

    ``subjects`` (#715 slice 1) records the subject id(s) whose data produced
    this entry, so a later per-subject cache invalidation can find it. It lives on
    the RECORD, NOT in ``CacheKey`` — folding it into the key would change the
    digest and over-partition scope-keyed (``{company_id, product_id}``) entries,
    wrecking the hit rate. Present-only: absent when empty, so records for
    subject-free calls (and every previously stored entry) are byte-unchanged and
    the key digest is untouched.
    """

    record: dict[str, Any] = {
        "key": key.to_dict(),
        "output": output,
        "created_at": created_at,
        "output_schema_hash": output_schema_hash,
    }
    if manifest_hash is not None:
        record["manifest_hash"] = manifest_hash
    if tokens_saved is not None:
        record["tokens_saved"] = tokens_saved
    if subjects:
        record["subjects"] = list(subjects)
    return record
