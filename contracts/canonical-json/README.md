# canonical-json (#420)

The cross-SDK `canonical_json` number-formatting contract — the primitive every
other area's hashes are built on.

`golden/numbers.json` is a list of `{value, canonical}` pairs: `value` parsed from
JSON, `canonical` the byte-exact `canonical_json(value)` string produced by Python
(`manifests._common.canonical_json` = `json.dumps(_neutralize_numbers(x),
sort_keys=True, separators=(",", ":"))`). Both SDKs must reproduce `canonical` from
`value`:

- Python — `tests/test_contracts_canonical_json.py`
- TypeScript — `packages/typescript/temporal/test/canonical-json.test.ts`

The fixture spans the magnitudes where naive `JSON.stringify` and Python `repr`
diverge: non-integral floats below `1e-4` (Python scientific, signed >=2-digit
zero-padded exponent), and integral floats at/above `1e21` (Python full integer vs
JS `1e+21`). Integral floats collapse to ints (`_neutralize_numbers`); `-0 -> 0`.
