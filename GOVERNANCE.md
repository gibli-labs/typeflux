# Governance

Typeflux Temporal is currently a **single-maintainer project**. This document
states how decisions are made today and how that evolves as the contributor
base grows — honestly, without implying a committee that does not exist.

## Roles

- **Maintainer** — merge rights, release rights, security response, and final
  decision authority. Listed in [MAINTAINERS.md](MAINTAINERS.md).
- **Contributor** — anyone who has a merged commit, a reviewed PR, an issue
  triaged, or documentation improved.

## Decision making

- Day-to-day decisions (bug fixes, refactors, docs) happen in PRs; a
  maintainer approval merges them.
- Significant changes — public API or YAML surface, contract changes under
  `contracts/`, release/versioning policy, new dependencies with licensing
  implications — require an issue first, stating the design and its
  compatibility impact, before a PR is opened.
- The maintainer has final say. Disagreements should be argued in the issue
  with technical evidence; decisions and their rationale are recorded there.

## Becoming a maintainer

Sustained, high-quality contribution over months — code, review, triage, or
security response — is the path. The current maintainer invites new
maintainers; the bar is trust with merge and release rights. When the project
has three or more maintainers, this document will be revised to a
lazy-consensus model with recorded votes for significant changes.

## Changes to governance

Changes to this document are proposed by PR and require maintainer approval;
substantive changes should be announced in the release notes.
