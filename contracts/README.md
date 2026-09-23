# Typeflux contracts (language-neutral)

The cross-language contracts that let "a Typeflux run" mean the same thing in the
Python and TypeScript SDKs (epic
[#384](https://github.com/gibli-labs/typeflux-temporal/issues/384)). This is the
**executable definition of parity** and the cross-epic definition-of-done gate
([#392](https://github.com/gibli-labs/typeflux-temporal/issues/392)).

`CONTRACT_VERSION` pins the bundle version (currently `1`). Goldens are generated
from the Python SDK baseline (the locked conformance source); every other SDK
**reproduces** them.

**Canonical hashing.** All content-addressed hashes (manifest hashes, cache
keys) are `sha256` over **canonical JSON**: keys sorted by Unicode code point, no
whitespace, `ensure_ascii` escaping, and numbers normalized to a language-neutral
form — integral floats are emitted as integers (`0.0` → `0`) to match
`JSON.stringify`. A TS reproduction applies the same normalization
(`canonicalJson`) to compute identical hashes.

## Contract areas

| Area | Issue | What it pins |
|---|---|---|
| [`schema-profile/`](schema-profile/) | #389 | the provider-safe JSON Schema subset (`to_provider_safe`) both SDKs map to |
| [`prompt-ref/`](prompt-ref/) | #390 | the serialized `{ name, version, label }` prompt reference (`prompt_type` is a resolve-time hint, **not** serialized) |
| [`manifest/`](manifest/) | #390 | the activity- and workflow-execution manifest shapes + their content hashes |
| [`trace/`](trace/) | #390 | the trace record + observation shape (`model_dump(exclude_none=True)`) |
| [`cache-key/`](cache-key/) | #391 | the cross-run cache key + record (`cache_input_hash` / `cache_key_digest` / `cache_record`) |

Each area's `golden/` holds the expected output; some also hold the raw **input**
fixture the output is derived from (e.g. `schema-profile/golden/review_packet_input.json`,
`manifest/golden/{input,output}_schema.json`).

## Interface contracts

Areas pin *reproduction parity* (every SDK reproduces the goldens). **Interface
contracts** ([#616](https://github.com/gibli-labs/typeflux-temporal/issues/616))
are the second contract class: a normative surface document that servers
*conform to* and clients are *generated from*. They are registered under
`interfaces` in [`conformance.json`](conformance.json) — the index, not
directory shape, classifies a contract, so an interface may also carry
fixtures (e.g. #617's HTTP conformance suite) without becoming an area.

| Interface | Issue | What it pins |
|---|---|---|
| [`controlplane/`](controlplane/) | #616 | the control-plane HTTP API (OpenAPI 3.1) — Python server conforms, TS client is generated from it |
| [`temporal-binding/`](temporal-binding/) | #618 | the operate-tier Temporal wire conventions: shared memo/signal/query surface + the two deliberately divergent execution ABIs as binding profiles |
| [`resolver/`](resolver/) | #619 | the transport-shaped resolution interface — the one language-bound layer; responses are control-plane contract DTOs, cross-checked by the integrity gates |

Changes to an interface document are reviewed as interface changes, never as
regen noise — see each interface's README for its change process.

## Conformance suite

[`conformance.json`](conformance.json) is the machine-readable parity index:
every area → its `golden`/`input` fixtures, the canonical operation, and per-SDK
`coverage`. **Conformance = both SDKs reproduce every golden from its input.**

- **Python** (the baseline): `cd packages/python && uv run pytest -m "not live" tests/test_contracts_*.py`
- **TypeScript** (reproduces the goldens): `pnpm --filter @typeflux/temporal test`

Both run in CI (the `Tests` and `TypeScript SDK` jobs). A suite-integrity gate in
each SDK (`tests/test_contracts_conformance.py`, `test/conformance-suite.test.ts`)
asserts `conformance.json` stays in sync with `CONTRACT_VERSION` and the fixtures
on disk — every `contracts/` directory is indexed as exactly one of an area or
an interface — so the contract surface can't drift silently.

### Coverage

The TypeScript SDK reproduces the cache-key, schema-profile, prompt-ref, trace,
and **manifest leaf** hashes (`schema_hash` / `messages_hash`). The composite
manifest hashes (`activity_manifest_hash`, execution `manifest_hash`,
`workflow_contract_hash`) + full manifest `to_dict` are tracked in
[#425](https://github.com/gibli-labs/typeflux-temporal/issues/425); `conformance.json`
marks `manifest` as `typescript: partial` until then.
