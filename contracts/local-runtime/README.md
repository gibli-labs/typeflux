# Local capability fixtures

`golden/capability-cases.json` is the shared Python/TypeScript configuration matrix for
issue #89's static local-engine boundary. Each case applies its patches to `base`
and either expects a YAML error path or eligibility for later runtime assembly.
It is not an execution-conformance or policy-attestation fixture.

Both SDK test suites execute every case. Their additional closure tests cover
conditional nested workflows/maps, repeated children, reference errors and engine
mismatches. Clean installation probes exercise the same public boundary without
a Temporal SDK.


`golden/execution-cases.json` provides deterministic local SDK execution cases.
Both editions load the same workflow/child definitions and use a provider that
returns `{n: input.n + 1}`. Assertions compare validated output and provider call
order across sequences, early exits, skipped parallel branches, activity maps,
empty maps, nested workflows and workflow maps. Schemas use `models:Item`,
`models:Batch` and `models:Branches`, resolved by each SDK's existing mechanism.
This matrix proves the local subset; Temporal equivalence, CLI adoption and
process-loss journeys remain separate acceptance work in #89.
