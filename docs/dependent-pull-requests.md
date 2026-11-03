# Dependent pull requests

Typeflux supports accepted pull requests targeting `main`. Keep dependencies
explicit and merge prerequisites before their dependents. A feature-branch base
can make a diff easier to read, but is not a supported path to merge acceptance:
CodeQL is filtered to `main`, and trusted CI acceptance requires one open PR with
the current head targeting this repository's `main`.

## Prepare and merge a dependency

1. Finish and validate the prerequisite locally using the
   [development loop](development.md), then open its PR to `main`.
2. Develop the dependent change in an isolated branch or worktree. Prefer keeping
   it local until the prerequisite merges. If an early PR is necessary, make it a
   draft targeting `main`, link the prerequisite, and identify its included commits
   and what remains incomplete. Reviewers should assess the prerequisite first;
   the combined diff and CI do not replace separate prerequisite review.
3. Merge the prerequisite only after its required checks and review pass. Do not
   merge the dependent first or treat a draft's earlier checks as final evidence.
4. Fetch current main. Because merges normally squash, rebuild the dependent on
   `origin/main` with only its own changes. Either rebase the dependent commits
   with a known boundary (`git rebase --onto origin/main LAST_PREREQUISITE_COMMIT`)
   or create a fresh branch from main and cherry-pick only those commits. Inspect
   the resulting diff for duplicated prerequisite code, dropped changes and new
   conflicts. Coordinate any published-branch rewrite; use a lease rather than an
   unconditional force-push. A new branch needs a new main-target PR.
5. Rerun local validation and the graph impact report against the updated starting
   revision. Finish any requested local review on this completed diff before
   pushing the coherent update. Update the PR's dependency notes and mark it ready
   only when its requested scope is complete and locally validated.
6. Inspect fresh required checks and review for the updated head and base, then
   merge the dependent. Repeat this order for each dependency in the chain.

No prerequisite merge, rebase or retarget carries passing evidence forward by
itself. If main advances again, refresh integration evidence before merging.

## Refresh and inspect evidence

Changing only a PR's base is insufficient. The repository's PR workflows use the
default `opened`, `synchronize` and `reopened` event types, which do not include
base edits. A completed branch update produces a `synchronize` event. If the head
does not need to change after retargeting, close and reopen the still-unmerged PR
after setting its base to `main` to request fresh PR workflows, then verify that
they actually ran. Do not repeatedly push unfinished changes just to wake bots.
See [GitHub's PR event reference](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request).

A rerun keeps the original event's SHA and ref; it does not establish that a new
base was tested. Use reruns only when that original source is still the intended
integration revision. Obtain a fresh PR event after a base change. A manual `CI`
dispatch on a feature branch is also insufficient: non-PR acceptance requires a
main revision. See [GitHub's rerun semantics](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs).

Start inspection with these read-only commands, replacing `PR_NUMBER` and `RUN_ID`:

```sh
gh pr view PR_NUMBER --json baseRefName,headRefOid,mergeStateStatus,statusCheckRollup
gh api repos/gibli-labs/typeflux/pulls/PR_NUMBER --jq '{base: .base.sha, head: .head.sha}'
gh run view RUN_ID --json databaseId,headSha,event,attempt,status,conclusion,jobs,url
```

Check the current head, base commit, CI run ID and attempt together. Inspect the
native `evidence-*` receipts and `accepted-ci-*` result, not only green job names.
For a PR, trusted acceptance checks that the tested integration commit's parents
are exactly the PR base SHA reported by GitHub at validation time followed by its
head SHA. Strict required-check protection separately enforces being up to date
with main; acceptance alone is not proof of the latest main tip. It reads candidate
Git objects and bounded artifact JSON as data; its executable validator and
required registry come from the default branch. Base and candidate job/test
obligations both apply. An unreviewed feature branch cannot supply that authority.

All required contexts remain mandatory: `CI acceptance`, Dependency Review,
DCO sign-off, and Python, JavaScript/TypeScript and Actions CodeQL analysis. Missing,
skipped, cancelled, stale, failed or mismatched evidence is not a pass. A green
acceptance check from before a base update is insufficient. Release still requires
accepted main CI for the exact release revision; PR merge evidence cannot certify
the later squash commit. See [CI acceptance and release](development.md#ci-acceptance-and-release).

## Refresh review separately

CI completion does not prove that an automated reviewer inspected the final diff.
After changing the head or base, inspect the review's commit/check identity and
the current diff. Address or explicitly reconcile earlier threads, including
outdated ones; an outdated comment is not a resolved finding.

The repository carries `.cursor/BUGBOT.md`, but no committed Bugbot trigger config.
External app/team settings are not established by those rules. Cursor documents
automatic PR-update reviews and base-branch configuration; do not infer that a
base-only edit refreshed review. Inspect the actual check/review result. If a
fresh review is absent, finish the diff first, then have an authorized maintainer
request review using the app's supported path (`cursor review` for Bugbot), or
record a fresh independent review with its base and head identities. Do not claim
that an older review covered the changed diff. Follow the repository's review
requirements and [Bugbot documentation](https://cursor.com/docs/bugbot); do not
manually trigger reviews on work in progress.

## Observed GitHub evidence

The #98/#99 sequence demonstrates the boundary and recovery path:

- [CI run 40732792764](https://github.com/gibli-labs/typeflux/actions/runs/40732792764)
  passed ordinary jobs for head `e9dc419b32be14efd1ef2af2814ce9c47c0191f8`, but
  [acceptance rejected it](https://github.com/gibli-labs/typeflux/runs/122975741777)
  with `CI run must identify one current pull request to main`.
- [Prerequisite #99](https://github.com/gibli-labs/typeflux/pull/99) merged before
  [dependent #98](https://github.com/gibli-labs/typeflux/pull/98). The latter's final
  head was `84c91af062e19a27030d4901513844ed305857f6`, targeting `main`.
  [Fresh CI 40963528246](https://github.com/gibli-labs/typeflux/actions/runs/40963528246),
  all three [CodeQL analyses](https://github.com/gibli-labs/typeflux/actions/runs/40963528200),
  and [trusted acceptance](https://github.com/gibli-labs/typeflux/runs/123764034710)
  passed. Its native graph receipt identifies tested integration revision
  `73e541f390997bfc16c33968ac92dcc2cde8e467`, run `40963528246`, attempt `1`.
- The recorded Cursor review on #98 references earlier head
  `29e5bdd8e9195310d4d9abf6b90de9c1616a51f5`. This is evidence of earlier review,
  not proof that retargeting refreshed it or that it covered the final head.

These are historical workflow observations. They cannot replace fresh checks or
review on a subsequent PR, establish deployed behavior, or authorize publication.
