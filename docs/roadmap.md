# Roadmap

The public plan for taking Typeflux Temporal from beta to a stable 1.0.
Sequencing is deliberate; dates are not promised. Items move only when their
gate evidence exists — see [GOVERNANCE.md](../GOVERNANCE.md) for how
decisions land.

## Now — public beta hardening

- Public package availability without credentials: PyPI (`typeflux`),
  npm (`@typeflux/*`, `typeflux-mcp`), and an unauthenticated path for the
  generated control-plane client.
- Package-manager-first onboarding: clean-account quickstarts for the Python
  SDK, TypeScript SDK, MCP server, control plane, and console — including a
  local/fake-provider path that needs no paid credentials.
- Release engineering: trusted publishing, release evidence (checksums,
  SBOMs, provenance once public), rollback drills, protected release
  environments.
- CI/supply-chain baseline: CodeQL on PRs, SHA-pinned actions, dependency
  automation, OpenSSF Scorecard, dependency-license policy.

## Next — stabilization (first ~30 days after beta)

- Daily triage; security reports acknowledged within the SECURITY.md
  targets; weekly patch cadence while the tail of beta issues burns down.
- Cross-platform install/upgrade verification on the supported matrix.
- Documentation gaps found by real onboarding get fixed with priority.

## Later — the 1.0 decision

1.0 is an **evidence gate, not a date** ([release-policy](release-policy.md)
defines what changes at 1.0 — real deprecation windows and a support
policy). The gate includes:

- Independent external deployments succeeding without maintainer help.
- Release and rollback procedures exercised for every package.
- Cross-edition and contract compatibility suites protecting supported
  behavior.
- A security-disclosure rehearsal completed.
- Multiple consecutive weeks without a release-blocking packaging or CI
  incident.

## Known preview limitations

The TypeScript edition has documented parity gaps (CLI, trace reading,
OTLP) — [editions.md](editions.md) is the authoritative table. Open-source
availability is not a regulated-production certification;
[production-readiness](production-readiness.md) and
[compliance-readiness](compliance-readiness.md) remain authoritative.

## Input

Feature requests go through the issue forms; significant surface changes
start as design issues per [CONTRIBUTING.md](../CONTRIBUTING.md).
