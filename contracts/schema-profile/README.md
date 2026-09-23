# Provider-safe JSON Schema profile (#389)

The neutral cross-SDK form for structured output is **JSON Schema (draft
2020-12)**. The **provider-safe profile** is the subset accepted by every
supported structured-output mode — Gemini `response_schema`, OpenAI strict
`json_schema`, Anthropic `json_schema`. Both SDKs map their native types to
draft-2020-12 and then run the shared normalizer, so the same typed shape
yields equivalent provider-safe output everywhere.

- Python: `typeflux.contracts.to_provider_safe` /
  `pydantic_provider_schema` / `lint_provider_safe`
  (`packages/python/src/typeflux/contracts/schema.py`).
- TypeScript: the Zod mapper (`z.toJSONSchema()` → `to_provider_safe`) reuses
  the same rules — tracked in #400.
- Golden fixtures (generated from the Python baseline, the locked conformance
  source) live in `golden/`; the cross-SDK conformance runner is #392.

## Rules the normalizer enforces

| Rule | Why |
|---|---|
| Inline `$ref`/`$defs`; drop the `$defs` block | Gemini does not support `$ref` — it is the binding constraint |
| Every object gets `additionalProperties: false` | OpenAI strict requires closed objects; Gemini rejects open ones |
| Every property is `required`; originally-optional ones become a nullable union `anyOf: [T, {type: null}]` | OpenAI strict requires all keys present; nullability carries optionality on the wire |
| Flatten single-branch `allOf` (Pydantic's `{allOf: [ref]}`) into the parent | keeps the object rules applicable after inlining |
| `const` → single-member `enum` | Gemini has no `const` keyword |
| `oneOf` (+ `discriminator`) → `anyOf` | tagged/discriminated unions; `anyOf` is the provider-safe and generation-equivalent form |
| Strip `$schema`, `$id`, `$anchor`, `$comment`, `default`, `examples` | annotation-only; not part of the structured-output contract |
| Drop `format` values outside an allowlist (`date-time`, `date`, `time`, `duration`, `uri`, `uri-reference`, `email`, `uuid`) | providers reject unknown formats |

## Constructs the linter rejects (not normalizable)

multi-branch `allOf`, `not`, `if`/`then`/`else`, `patternProperties`,
`dependentSchemas`/`dependentRequired`/`dependencies`, tuple `prefixItems`,
**open objects/maps** (`additionalProperties: true`, or a schema value such as
`dict[str, X]` — strict modes require closed objects; model maps explicitly or
as a list of `{key, value}` entries), unresolved `$ref`, and **recursive
models** (a `$ref` cycle — no provider-safe schema can express recursion).

`to_provider_safe` raises `ProviderSchemaError` (carrying the `Violation`s) when
any of these survive; `lint_provider_safe` returns them without raising.

## Out of scope here (follow-ups)

- Wiring the providers to call `to_provider_safe` before dispatch — behavior-
  affecting, so it lands separately where it can be diffed against today's
  per-SDK schema handling.
- The TypeScript Zod mapper (#400) and the cross-SDK conformance runner (#392).
