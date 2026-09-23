# Policy-governed review — TS project-governance example

A runnable demonstration of **project governance** (#454, parity with Python's
`project/` policy subsystem): a composed org + tenant policy enforced as a
**fail-closed pre-flight** in `buildRuntime`, so a non-compliant workflow refuses
to start.

What it exercises end to end:

- **Policy composition** ([policies/org.yaml](./policies/org.yaml) +
  [policies/tenant.yaml](./policies/tenant.yaml)) via `composeProjectPolicies` —
  the most-restrictive merge: allow-lists **intersect** (the tenant drops `gpt-4o`
  and `anthropic`), bounded numerics take the stricter side (`max_concurrent` 2 <
  the org's 4). The composed policy carries a deterministic `policyHash`.
- **The `policy` pre-flight** on `buildRuntime` / `assembleYamlRuntime`: the spec
  is validated against the composed policy **before** any provider, activity map,
  or worker is built. A violation throws `ProjectPolicyEnforcementError` carrying
  the failed checks.
- **Per-dimension enforcement** — provider/model allow-list, required
  observability backend + PII redaction, and `provider_limits` bounds. Dimensions
  a policy does not constrain are reported `skipped`.

The example runs three workflows against the composed policy:

| Spec | Outcome |
| --- | --- |
| [typeflux.yaml](./typeflux.yaml) — openai/gpt-4o-mini, redaction on, limits within bounds | ✓ builds + runs live |
| [typeflux.rogue.yaml](./typeflux.rogue.yaml) — openai/**gpt-4o**, redaction **off** | ✗ refused by the pre-flight (never starts) |
| [typeflux.moderated.yaml](./typeflux.moderated.yaml) — a moderator that reports a forbidden category | ✓ passes admission, then ✗ **blocked at the runtime checkpoint** mid-run |

## Two enforcement layers

- **Admission** (the `buildRuntime` pre-flight) validates the static spec before the
  workflow starts — provider/model, observability/redaction, `provider_limits`. The
  rogue spec is caught here.
- **Runtime** ([`RuntimePolicyGuard`](../../src/policy-enforcement.ts), #454 slice 3)
  runs *inside* each activity: it checks the model a call actually resolved to (a
  backend-prompt or code-defined override an admission check can't see) and escalates
  the moderator's reported `categories`/`score_threshold` to a block. The moderated
  workflow passes admission (`semantics.categories` is a runtime control, reported
  `skipped` at admission) but its output is blocked mid-run — the moderator only
  *flags*, yet the policy forbids the reported category, so the guard blocks it
  (a non-retryable `ModerationBlockedError`). The moderation **verdict**
  (`{decision, categories, max_score, moderator}`) is recorded on the activity
  trace as redaction-exempt audit evidence — it lands even for the blocked output,
  and the example prints it after the block.

## Governance dimensions in play

The composed policy (`acme-org` ∩ `acme-eu-tenant`) governs:

- `providers.allowed` → only `openai/gpt-4o-mini` survives the intersection.
- `observability.required` + `observability.redaction.required` → the spec must
  declare a backend with redaction on. (The offline example declares
  `type: langfuse` to satisfy the posture but does not wire a real trace
  transport; a production run passes `observerFromSpec(spec, transport)` as
  `observer`.)
- `runtime.provider_limits.default.max_concurrent` → the runtime tier may only be
  tighter than the composed bound.
- `semantics.categories` → a **runtime** control: a moderator that reports one of
  these categories is escalated to a block at the output checkpoint.

The rogue spec violates the provider/model allow-list **and** the redaction
requirement, so the pre-flight fails on both dimensions at once.

`runtime.imports` (Python's importlib policy) is reported `skipped`: the TS SDK
injects providers/registries/observers, so there is no module-loading surface to
police (#496).

## Run it

Offline apart from the Temporal dev server — the scripted provider
([fakes.ts](./fakes.ts)) needs no API key:

```sh
temporal server start-dev            # in a separate terminal
pnpm install && pnpm -r build        # from the repo root
pnpm --filter @typeflux/temporal-yaml example:policy
```

It prints the composed policy hash + allowed models, the per-dimension policy
report for each workflow, the compliant workflow's `Assessment` result, the
rejection message for the rogue workflow, and — for the moderated workflow — the
runtime block reason after it starts on the server.

The example is also executed on every CI run — without a Temporal server — by
[`test/example-policy-governed-review.test.ts`](../../test/example-policy-governed-review.test.ts),
which composes the same policies and asserts the compliant spec assembles while
the rogue spec is refused fail-closed.
