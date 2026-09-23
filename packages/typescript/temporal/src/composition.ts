/**
 * Composition primitives for code-defined orchestration (parity Epic 3, #450;
 * Python `composition.py`, #385). Typed async helpers for DAG-shaped multi-agent
 * pipelines — usable in any async runner now, and inside a Temporal workflow once
 * the TS worker lands. Code-first: plain typed helpers, not a DSL.
 */

/**
 * Run `fn` over `items` with at most `concurrency` calls in flight, returning the
 * results in `items` order (the bounded parallel map — a production adopter's per-claim
 * "20-worker" pattern). Wrap `fn` with {@link withFallback} for per-item resilience.
 *
 * On the first rejection, `fanOut` stops scheduling new items and, once every already
 * in-flight sibling has SETTLED, rejects with that first error. NOTE (TS vs Python):
 * JavaScript has no task cancellation, so sibling calls already in flight run to completion
 * (their results are discarded) whereas Python's `fan_out` `task.cancel()`s them. Crucially,
 * `fanOut` AWAITS all workers to settle before rejecting — it never leaves a sibling running
 * detached past the rejection (a detached sibling that mutates shared workflow state after the
 * failure has propagated is a determinism/orphaned-side-effect hazard; #299 relies on this so
 * a parallel branch or a map item cannot push a compensation after the unwind has begun). No
 * NEW item starts after the failure.
 */
export async function fanOut<T, R>(
  items: readonly T[],
  fn: (item: T) => Promise<R>,
  options: { concurrency: number },
): Promise<R[]> {
  const { concurrency } = options;
  if (concurrency < 1) {
    throw new Error("fanOut concurrency must be >= 1");
  }
  const results = new Array<R>(items.length);
  let next = 0;
  let failed = false;
  let firstError: { error: unknown } | undefined;

  async function worker(): Promise<void> {
    // Pull the next index synchronously (single-threaded: no interleave between the
    // bound check and `next++`), so two workers never claim the same item.
    while (!failed && next < items.length) {
      const index = next;
      next += 1;
      try {
        results[index] = await fn(items[index] as T);
      } catch (error) {
        // Capture the FIRST error and stop scheduling, but RETURN rather than throw so the
        // other workers keep settling — the aggregate rejection is deferred until all have.
        if (firstError === undefined) {
          firstError = { error };
        }
        failed = true;
        return;
      }
    }
  }

  const workerCount = Math.min(concurrency, items.length);
  // `allSettled` (not `all`): every worker resolves (workers never throw — they capture and
  // return), so this awaits ALL of them, guaranteeing no in-flight sibling survives the throw.
  await Promise.allSettled(Array.from({ length: workerCount }, () => worker()));
  if (firstError !== undefined) {
    throw firstError.error;
  }
  return results;
}

/**
 * Run `fn()`; if it rejects with an error the `errors` predicate accepts, run
 * `fallback(error)` instead. Degrades a failing step to a fallback value (an empty
 * result, a prior result, …) rather than aborting the pipeline.
 *
 * Python's `exceptions=Exception` (a type / tuple of types) becomes the `errors`
 * predicate here (default: handle every error). Express class matching as
 * `errors: (e) => e instanceof MyError`; an error the predicate rejects re-throws.
 * The predicate must be pure — if it throws, its error replaces the original.
 */
export async function withFallback<R>(
  fn: () => Promise<R>,
  fallback: (error: unknown) => Promise<R>,
  options: { errors?: (error: unknown) => boolean } = {},
): Promise<R> {
  const handles = options.errors ?? (() => true);
  try {
    return await fn();
  } catch (error) {
    if (handles(error)) {
      return await fallback(error);
    }
    throw error;
  }
}

/**
 * Retrieve context for `input`, then fold it into a grounded input (the
 * search->prompt pattern, #399). `await search(input)` (sync or async) materializes
 * the results, then `ground(input, results)` builds the input an activity runs on —
 * so its prompt renders the retrieved context instead of relying on model recall.
 * Keeping retrieval a separate, typed step makes the grounded input auditable.
 */
export async function groundWithSearch<T, R, G>(
  input: T,
  options: {
    search: (input: T) => Iterable<R> | Promise<Iterable<R>>;
    ground: (input: T, results: R[]) => G;
  },
): Promise<G> {
  // Materialize to an array (parity with Python's `list(results)`) so `search` may
  // return any iterable — an array, a Set, a generator.
  const results = Array.from(await options.search(input));
  return options.ground(input, results);
}
