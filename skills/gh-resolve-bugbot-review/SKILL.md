---
name: gh-resolve-bugbot-review
description: Investigate and resolve Cursor Bugbot findings on GitHub PRs, recording the disposition and validation before resolving review threads.
---

# Resolve Bugbot findings

Treat a finding as a concrete review task: inspect, document a plan or supported disposition, implement when needed, validate, reply, then resolve. A false-positive claim needs evidence. A deferred valid finding remains unresolved unless the user explicitly accepts its disposition.

Inspect the current PR head, checks, reviews, and unresolved threads. Use this skill's `scripts/list_bugbot_threads.py OWNER/REPO PR` when installed, or GitHub's GraphQL `pullRequest.reviewThreads` API. Read every page; preserve thread ID, comment URL, path, and line. An outdated thread may still describe a current bug.

For each finding, reply with the intended fix and validation, or its supported false-positive rationale. Existing authorization to fix the PR covers routine review corrections; ask only when the required change materially exceeds that scope.

Prefer a focused regression that demonstrates the problem and the fix. Run the repository's maintained verification commands, not a stale skill-specific list. For Typeflux, use `docs/development.md` and its shared verifier; include relevant graph impact and actual test evidence. Runtime/observability findings require appropriate integration evidence, with environment limits stated honestly.

Batch related fixes before pushing. Do not repeatedly invoke the reviewer on unfinished work. Reply to each finding with its disposition, fixing commit when applicable, and meaningful validation evidence. Resolve only after that reply.

## GitHub API details

Cursor comments may exist while REST reply endpoints return 404. Prefer GraphQL replies:

```sh
gh api graphql -F threadId=THREAD_ID -F body=@/tmp/resolution.md \
  -f query='mutation($threadId:ID!, $body:String!) { addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$threadId, body:$body}) { comment { url } } }'
```

`-F body=@file` reads the file; `-f body=@file` posts the literal path. Preserve newlines in the file. Resolve a documented finding with:

```sh
gh api graphql -f threadId=THREAD_ID \
  -f query='mutation($threadId:ID!) { resolveReviewThread(input:{threadId:$threadId}) { thread { id isResolved } } }'
```

After updates, reread the current head, unresolved threads and required checks. New pushes invalidate old CI evidence. `BEHIND` requires a base update; `BLOCKED` may reflect checks, approvals or unresolved conversations. Report a remaining blocker precisely. Attach the PR to the Codex task when available.
