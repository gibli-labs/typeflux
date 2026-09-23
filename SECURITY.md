# Security Policy

## Reporting a vulnerability

**Do not open a public issue for security reports.**

Report vulnerabilities privately via
[GitHub Security Advisories](../../security/advisories/new)
("Report a vulnerability" on the repository's Security tab). If that route is
unavailable to you, email <joseph.s.gibli@gmail.com> with the subject prefix
`[SECURITY]`.

Please include: the affected package/component and version, reproduction
steps or a proof of concept, and impact assessment if you have one.

## What to expect

- **Acknowledgement** within 2 business days.
- **Triage decision** (accepted / needs info / declined) within 5 business
  days.
- Fixes for accepted reports are developed privately and released with an
  advisory; you will be credited unless you prefer otherwise.
- Please give us reasonable time to remediate before public disclosure.

## Supported versions

The SDK surface is in **0.x (beta)**; the generated control-plane client is
versioned in lockstep with its contract major. Only the latest release of
each package receives security fixes; there are no long-term support
branches.

| Package | Supported |
|---|---|
| `typeflux-mcp` (npm) | latest published 0.x release |
| `@gibli-labs/control-plane-client` (GitHub Packages) | latest published release of the current contract major |
| `typeflux` (Python SDK) | not yet released to PyPI — the `main` branch tip is the supported revision |
| `@typeflux/*` (npm SDK/tooling packages) | not yet published — the `main` branch tip is the supported revision |

As packages reach their public registries, this table moves them to
"latest published 0.x release".

## Dependency vulnerabilities

CI blocks pull requests that introduce dependencies with known
critical/high advisories (dependency-review) and Dependabot files upgrade
PRs for the ecosystems in `.github/dependabot.yml`. A critical/high
advisory against an existing dependency is dispositioned within the triage
targets above: upgraded, mitigated, or — only with a written rationale in
the tracking issue — explicitly accepted for a bounded period.

## Scope notes

- Secrets in example/fixture files are synthetic by policy; if you find
  something that looks real, report it privately as above.
- The control plane and console are designed for deployment behind your own
  network boundary; reports assuming a hardened public-internet deployment
  should state that assumption.
