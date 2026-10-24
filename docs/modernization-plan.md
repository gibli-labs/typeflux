# Runtime, compiler, provider, and observability modernization

Execution plan, 2026-10-22. Tracks #21, #63, and #71. The user authorized planning and execution together.

## Delivery

Use two cohesive implementation PRs, each prepared and validated locally before its initial commit/push. Keep one primary commit per PR and squash at merge. Batch necessary review corrections. Do not open a separate proposal PR or generate individual dependency PRs for this work.

1. **Toolchain, runtime coverage, and providers** (#21, closes #63): upgrade all seven TypeScript packages to stable 7.0.2; isolate both OpenAPI generators in one private tool with their required TypeScript 5 compiler; remove the product compiler hold. Use Node 24 LTS (minimum 24.15) and Node 24 types throughout; remove older provider major ranges. Test Python 3.11–3.15 while retaining the existing minimum, and move reference deployment images to validated current runtimes. Update pnpm, npm, uv, provider major ranges, and compatible lock entries together. Replace Instructor with native OpenAI structured outputs: Instructor 1.17 requires jiter<0.15, blocking OpenAI 3.36 (jiter>=0.16). Remove the old injection/retry options; native client injection is `openai_client`, with SDK retries disabled so Typeflux/Temporal own retries. Verify packages, generated contracts, native Temporal imports, real SDK HTTP transport behavior, and release workflow assumptions.
2. **Langfuse adapters** (closes #71): move the Python adapter to SDK 5 and the TypeScript adapter from legacy `langfuse` to current scoped SDKs and OpenTelemetry. Replace legacy trace-list reads with bounded cursor-paginated observations, and use root-observation input/output. Preserve subject filters, ownership checks, deduplication, truncation handling, deletion fail-closed behavior, observation parentage, prompt linkage and shutdown/flush behavior. Document server prerequisites and the November 16 Cloud cutoff.

The second migration can be prepared locally while the first awaits CI, but is rebased onto its merged result before opening the second PR. Keep one active implementation PR at a time. No package publishing, production deployment or destructive live erasure is part of this plan. A live telemetry smoke is attempted only if a configured test service is available; transport tests and explicit rollout evidence must identify any unavailable live validation.

## Compatibility and invariants

There are no existing users to migrate. Prefer direct replacement and current SDK APIs over compatibility shims, legacy-data backfills or dual-version support. Keep the Python minimum only where it adds no implementation constraint; existing CI lanes remain useful validation. Breaking runtime requirements are documented and the published MCP package receives a minor version bump.

- Resolve and lock stable versions. Treat resolution, type-checking, runtime testing and service compatibility as distinct evidence.
- Isolate the legacy generator compiler instead of requiring the entire application to stay on it. Generated HTTP types must remain byte-identical unless a separately justified contract change is required.
- Keep supported runtime minimums separate from the runtime used by CI and reference images. Published type declarations must work on the supported Node baseline.
- Update consumers and producer tooling together: manifests, lockfiles, CI, Dockerfiles, package checks, examples and release documentation.
- Reuse the typed metadata contributors. No new metadata exemptions, PII fields or high-cardinality search tags. The workflow execution manifest remains unchanged because this is a tooling/provider/backend migration, not an execution-shape change.
- Retain upstream-owned transitive constraints and security overrides until their original conditions no longer apply. Explicitly document any necessary temporary compatibility exception and its removal condition.

## Validation and rollback

Run focused real-SDK transport and generator tests first. Then run Ruff, formatting, mypy, Python package build and the non-live suite; all SDK builds/typechecks/tests; generated client drift/build; MCP build/pack verification; and both control-plane conformance and browser lanes. Validate new Python/Node runtimes and release artifact import/byte-identity assumptions. Keep required CI check names stable. Fix failures before pushing, then wait for CI and review on the final commit.

A regression rolls back the affected consolidated PR with its manifest/lock/code changes together. Langfuse deployment requires Cloud or self-hosted server 4.59.0+; finish migration before the Cloud legacy API removal on 2026-11-16. No rollback may restore deprecated Cloud endpoints after that cutoff.

## Upstream guidance applied

- [TypeScript 7 release](https://devblogs.microsoft.com/typescript/announcing-typescript-7-0/): distinguish the native compiler from tools that consume the old JavaScript compiler API; validate declaration output and generation separately.
- [Node release policy](https://nodejs.org/en/about/previous-releases): use supported LTS for production defaults, with an explicit compatibility matrix.
- [uv locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/): lock updates are explicit; frozen installs reproduce the validated dependency set.
- [Dependabot configuration](https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference): group related updates, keep security discovery, and scope compatibility holds to the actual blocked dependency.
- [OpenAI SDK reference](https://developers.openai.com/api/reference/python): SDK internals and static types can change independently of runtime behavior; test the public client over intercepted HTTP instead of relying only on fake method objects.
- [Langfuse Python migration](https://langfuse.com/docs/observability/sdk/upgrade-path/python-v4-to-v5), [TypeScript migration](https://langfuse.com/docs/observability/sdk/upgrade-path/js-v3-to-v4), [current JS migration](https://langfuse.com/docs/observability/sdk/upgrade-path/js-v5-to-v6), and [API migration](https://langfuse.com/faq/all/deprecated-api-migration): replace removed APIs and legacy ingestion, preserve cursor/filter semantics, and coordinate server prerequisites with SDK upgrades.

## Execution notes

- PR #73 merged after all required checks and Bugbot passed: native TypeScript 7,
  Node 24, Python 3.11–3.15 coverage, current provider SDKs and isolated generator.
- Langfuse JS 6 requires OpenTelemetry 2; pin compatible exporters to 0.223.x
  because exporter 0.300 uses OTel 3. The upstream `sdk-trace-base` peer remains
  transitive; Typeflux uses its replacement `sdk-trace`. Remove this constraint
  when Langfuse supports OTel 3, updating processor/API/exporters together.
- Live Langfuse validation was not run for this migration. Real SDK HTTP
  contracts and in-memory OTel exports validate the adapters offline; verify one
  redacted workflow, prompt lookup, control-plane correlation and dry-run erasure
  against the target service before production rollout.
