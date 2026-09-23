# Cache key + record contract (#391)

The key a cross-run `CacheStore` ([#398](https://github.com/gibli-labs/typeflux-temporal/issues/398),
E3), adopter cache implementations, and the TypeScript SDK all use to memoize an AI-activity
result across runs. Part of the contract bundle (see `../CONTRACT_VERSION`). The
provider-side *prefix* cache (`session_cache_identity`, #60) is a separate
concern.

Goldens (from the Python baseline): [`golden/cache_key.json`](golden/cache_key.json),
[`golden/cache_record.json`](golden/cache_record.json),
[`golden/cache_key_artifacts.json`](golden/cache_key_artifacts.json) (the #504
artifacts fold).

## CacheKey

| field | type | notes |
|---|---|---|
| `activity` | string | the activity ("node") name |
| `input_hash` | string (sha256 hex) | digest of the call-determining inputs (below) |
| `scope` | object<string,string> | tenancy/partition keys — e.g. `company_id` + `product_id`; empty `{}` = global |

`scope` is a generic string map so any deployment chooses its partition keys
while supporting the `(node, input_hash, company_id, product_id)` shape used by existing adopter caches.
`cache_key_digest(key) = sha256(canonical_json(key))` gives a single flat key
string; because `canonical_json` sorts keys it is independent of `scope` order.

## input_hash

`sha256(canonical_json({ activity, input_schema_hash, rendered_messages_hash, provider_params, artifacts? }))`.

`rendered_messages_hash` (frozen by the execution-manifest contract, #390)
encodes the prompt rendered with the input, so identical input + prompt +
behavior params → identical hash → cache hit. This reuses the manifest as the
single source of truth for "what determines a call" and mirrors the
input-hash computation of existing adopter caches.

`artifacts` is the resolved-artifact identity
([#504](https://github.com/gibli-labs/typeflux-temporal/issues/504)): the
`artifact_groups_cache_identity` shape — per group `{ name, count, artifacts }`,
per artifact `{ group, index, source_kind, kind, media_type, role, sha256,
size_bytes }` with absent fields **dropped** (never null), plus a `source`
object (`{ type, url | uri | provider + file_id | path }`, none-dropped)
whenever no `sha256` pins the bytes — an unhashed URL/object-URI/provider-file
artifact keys on its *location*, a hashed artifact on its *content*. Groups
with no artifacts are **excluded**, so an optional input that resolves to
nothing keeps the artifact-free key. An artifact part *renders* as only its
group name + preamble text, so the rendered-messages hash cannot see the
underlying bytes; without this fold, replacing a file (or the source of an
unhashed artifact) under the same group name would serve a stale cached
output. The key is **omitted from the payload when absent or empty**, so
artifact-free keys (and all previously stored entries) are unchanged. This
shape is deliberately distinct from `artifact_groups_summary` (the redacted
manifest/observability shape, which must never carry paths/URLs) — the cache
identity is only ever hashed.

## CacheRecord

`{ key, output, created_at, output_schema_hash, manifest_hash?, tokens_saved? }`
— the cached **validated** activity output plus provenance. `created_at` is
ISO-8601; `output_schema_hash` ties the entry to the output schema it was
produced against (a schema change can invalidate stale entries).
