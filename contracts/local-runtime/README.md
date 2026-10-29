# Local capability fixtures

`golden/capability-cases.json` is the shared Python/TypeScript configuration matrix for
issue #89's static local-engine boundary. Each case applies its patches to `base`
and either expects a YAML error path or eligibility for later runtime assembly.
It is not an execution-conformance or policy-attestation fixture.

Both SDK test suites execute every case. Their additional closure tests cover
conditional nested workflows/maps, repeated children, reference errors and engine
mismatches. Clean installation probes exercise the same public boundary without
a Temporal SDK. Cross-engine execution fixtures remain part of the executor work.
