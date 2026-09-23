"""Language-neutral Typeflux contracts (#384).

The provider-safe JSON Schema profile and its mappers (#389) live here. Other
cross-SDK contracts (prompt-ref, manifest, trace, cache key) land alongside as
the contracts epic progresses.
"""

from __future__ import annotations

from typeflux.contracts.cache import (
    CacheKey,
    cache_input_hash,
    cache_key_digest,
    cache_record,
)
from typeflux.contracts.schema import (
    ProviderSchemaError,
    Violation,
    lint_provider_safe,
    pydantic_provider_schema,
    to_provider_safe,
)

__all__ = [
    "CacheKey",
    "ProviderSchemaError",
    "Violation",
    "cache_input_hash",
    "cache_key_digest",
    "cache_record",
    "lint_provider_safe",
    "pydantic_provider_schema",
    "to_provider_safe",
]
