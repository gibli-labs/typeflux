# Session-cache review — provider prefix cache across a fan-out

Showcases the **session cache** (#478): when a `map` fans a review over many
items that share a large *stable* prefix (a policy document, a rubric, long
instructions), the provider can cache that prefix **once per fan-out** instead of
re-sending it with every item. The activity opts in with a `cache:` block, and
the workflow brackets the fan-out automatically:

```
review_item.__prepare_cache__     ← once, before any item (caches the stable prefix)
  ↓ threads one CachedSessionHandle to every call
structuredCall(item A, handle)
structuredCall(item B, handle)    ← each per-item call reuses the cached prefix
structuredCall(item C, handle)
  ↓
review_item.__release_cache__     ← once, after the fan-out (frees a reference cache)
```

## Two cache styles

- **Reference style** (Gemini-shaped, this example's provider) — prep uploads the
  prefix and returns a server-side `cache_id`; per-item calls reference it; the
  release step frees it.
- **Prefix style** (Anthropic/OpenAI) — prep marks `cache_control` breakpoints
  with no id; the provider caches implicitly and there's nothing to release.

A `cache: reference` **artifact** (a stable document — policy PDF, rubric) joins
the cached prefix in a style-specific way (#362). Reference style uploads it once
into the cache object. Prefix style has no object, so the per-item assembly lifts
the reference artifact to the **front** of the conversation — after the system
prefix, before the varying per-item turn — and re-sends it each call at cache-read
rates (Anthropic marks the breakpoint on the last stable reference turn; OpenAI
caches the byte-prefix implicitly). The artifact must be **identical across all
items**: divergent *presence* is unrepresentable — an optional reference artifact
must give `attach` a static `text` (its message then emits every item), a textless
one must be `required`, and the optional-textless shape load-rejects. Divergent
*bytes* are not runtime-verified — they miss the cache (billing waste, not
corruption). On Anthropic a *document* cannot ride the text-only system prompt, so
reference documents must attach as `user`/`assistant` turns. Uncached and
reference-style runs keep the plain append-last message order.

Either way the workflow shape is identical, and the cache is **fail-soft**: a
provider without the capability returns an unengaged handle and every item still
runs with the full context — only cost/latency differ.

The handle carries only an **identity hash** of the cached content (never the
content), so it rides safely through Temporal history — byte-identical to the
Python SDK's for aligned shapes.

## Run it

Offline apart from the dev server — [fakes.ts](./fakes.ts) is a reference-style
recording provider (no network):

```sh
temporal server start-dev            # in a separate terminal
pnpm install && pnpm -r build
pnpm --filter @typeflux/temporal-yaml example:session-cache
```

It prints the reviews and the recorded lifecycle: **one** prepare, the same
`cache_id` threaded to every item, **one** release.

The session-cache plan derivation + the companion activity registration are
exercised on every CI run (without a server) by
[`test/example-session-cache-review.test.ts`](../../test/example-session-cache-review.test.ts).
