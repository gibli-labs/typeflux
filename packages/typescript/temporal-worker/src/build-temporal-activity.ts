/**
 * Activity-registration adapter (parity Epic 3, #450) — the TS equivalent of
 * Python `build_temporal_activity`. Wraps a typed Typeflux activity descriptor +
 * its execution options into a plain `(input) => Promise<output>` async function a
 * Temporal worker can register on a task queue.
 *
 * It is a thin boundary over the core's `executeActivity` (which already runs the
 * full pipeline: validate input -> resolve/render prompt -> provider call ->
 * validate output -> hook -> moderation -> cache), so it carries NO `@temporalio/*`
 * dependency and is fully testable with a scripted provider. The worker bootstrap
 * (`@temporalio/worker`) and the activity-context enrichment from the Temporal
 * runtime land in PR2.
 */

import { ApplicationFailure, CancelledFailure } from "@temporalio/common";
import {
  ActivityCancelledError,
  type ActivityContextOverrides,
  type ActivityDescriptor,
  ActivityValidationError,
  type CachedSessionHandle,
  cachedSessionHandleSchema,
  cachePrepActivityName,
  cacheReleaseActivityName,
  type ExecuteActivityOptions,
  executeActivity,
  ModerationBlockedError,
  PromptResolutionError,
  prepareSessionCache,
  ProviderCacheUnavailableError,
  ProviderConfigError,
  ProviderPolicyError,
  UnstableCachePrefixError,
} from "@typeflux/temporal";
import { z } from "zod";

/**
 * A Temporal-registrable activity function: validated input in, validated output out.
 * The optional second argument is a map fan-out's cached-session handle (#478), a
 * deserialized wire payload parsed here at the boundary. The optional third argument
 * is the run's subject id(s) (#715 slice 1), threaded by the workflow interpreter so
 * the activity context — and through it the cross-run cache record — carries them.
 */
export type TemporalActivityFn<In extends z.ZodType, Out extends z.ZodType> = (
  input: z.infer<In>,
  cachedSession?: unknown,
  subjectIds?: unknown,
) => Promise<z.infer<Out>>;

/**
 * Per-call runtime hooks the worker registration wires into a built activity; each keeps
 * `@temporalio/*` imports out of this adapter, and each is optional so a standalone activity
 * (tests, direct calls) runs unchanged. An options object rather than positional parameters so a
 * new injectable extends it without threading `undefined` holes through call sites (as #487's
 * `cancellationSignal` did).
 */
export interface TemporalActivityInjections {
  /**
   * Invoked per call to enrich the hook's `ActivityContext` with runtime fields (e.g. the Temporal
   * workflowId/attempt); returns `undefined` when there is no ambient context.
   */
  contextProvider?: () => ActivityContextOverrides | undefined;
  /**
   * Invoked per call to start a background heartbeat loop while the activity body runs (#484);
   * returns a stop function (called in `finally`), or `undefined` when no heartbeat is needed
   * (not in a worker activity, or no heartbeat timeout).
   */
  heartbeater?: () => (() => void) | undefined;
  /**
   * Invoked per call to obtain a warning sink for fail-soft degradation notices (#478) —
   * the worker registration wires the Temporal activity logger; `undefined` falls back to
   * the core default (`console.warn`).
   */
  warner?: () => ((message: string) => void) | undefined;
  /**
   * Invoked per call to obtain the activity's cancellation `AbortSignal` (#487) — threaded into
   * `executeActivity` so checkpoints abort and the in-flight provider call is dropped on workflow
   * cancel; `undefined` when not in a worker activity (standalone calls run uncancelled).
   */
  cancellationSignal?: () => AbortSignal | undefined;
}


/**
 * Terminal error classes: the same input deterministically reproduces them, so Temporal retries
 * only multiply provider spend (parity with Python `_raise_temporal_non_retryable_if_needed`) —
 * a malformed provider call/config, a blocked moderation verdict, and output validation that
 * already exhausted its repair retries. Prompt-resolution failures are classified separately by
 * their own `retryable` flag (see `asNonRetryableIfTerminal`).
 */
/** The subject-ids wire shape (#715 slice 1): a non-empty array of strings. */
const subjectIdsWireSchema = z.array(z.string());

const TERMINAL_ERRORS = [
  ProviderConfigError,
  ModerationBlockedError,
  ActivityValidationError,
  // A per-call project-policy violation (#454): the resolved model is not allowed.
  // Deterministic — the same call reproduces it — so retrying only wastes attempts.
  ProviderPolicyError,
  // A templated system prefix on a session-cached activity (#478): a permanent
  // misconfiguration — retrying is pointless backoff before the identical failure.
  UnstableCachePrefixError,
  // A vanished session cache that the one-shot uncached re-run did NOT recover
  // (prefix-style/unengaged handle, or a second raise): Temporal re-running the
  // identical call with the same fixed handle is doomed (Python retryable=False).
  // The recovery catch runs BEFORE this classification, so recoverable cases never
  // reach it.
  ProviderCacheUnavailableError,
] as const;

/**
 * Translate a terminal Typeflux error into a non-retryable `ApplicationFailure` so Temporal's
 * RetryPolicy fails the activity on attempt 1 instead of re-running a doomed call. The original
 * error rides along as `cause` and its class name as the failure `type`. Anything else (a
 * transient provider error, an unexpected throw) passes through untouched for the RetryPolicy to
 * act on. `ApplicationFailure` is inert data from `@temporalio/common` — no runtime context is
 * required, so activities built here still run standalone.
 */
function asNonRetryableIfTerminal(error: unknown): unknown {
  // Cancellation is control flow, not failure: rethrow Temporal's own CancelledFailure (the
  // signal's reason, carried as the cause) so the activity is recorded CANCELLED — parity with
  // Python's ActivityCancelled subclassing CancelledError. A standalone abort (plain
  // AbortController) keeps the typed ActivityCancelledError.
  if (error instanceof ActivityCancelledError) {
    return (error as { cause?: unknown }).cause instanceof CancelledFailure ? (error as { cause?: unknown }).cause : error;
  }
  // Prompt-resolution failures carry their own retryability (Python prompts/errors.py):
  // not-found/auth/config errors are deterministic and fail on attempt 1, while a
  // registry outage (PromptRegistryUnavailableError, retryable=true) passes through
  // for the RetryPolicy — a class list can't express that split, the flag can.
  if (error instanceof PromptResolutionError && !error.retryable) {
    return ApplicationFailure.create({
      message: error.message,
      type: error.name,
      nonRetryable: true,
      cause: error,
    });
  }
  if (error instanceof Error && TERMINAL_ERRORS.some((cls) => error instanceof cls)) {
    return ApplicationFailure.create({
      message: error.message,
      type: error.name,
      nonRetryable: true,
      cause: error,
    });
  }
  return error;
}

/**
 * Build the async activity function for `descriptor`. Each invocation first parses
 * the inbound value against `descriptor.input`, then runs it through
 * `executeActivity` with the supplied `options` (provider, prompt `registry` or
 * pre-rendered `messages`, cache store, deps/tenant, …).
 *
 * The input parse matters at the Temporal boundary (parity with Python's
 * `_coerce_activity_input`): a worker receives a *deserialized* payload, so the
 * compile-time `z.infer<In>` type is erased — without this an off-shape payload
 * would reach prompt rendering / the provider (and with pre-rendered `messages`
 * could even return a result). A bad payload rejects with a `ZodError` instead.
 * The function is `async` so that parse failure surfaces as a rejected promise.
 *
 * The returned function's `name` is set to `descriptor.name` so a worker that keys
 * its activity map by function name — and stack traces — use the contract name.
 *
 * `injections` carries the {@link TemporalActivityInjections} runtime hooks (context enrichment,
 * heartbeating, cooperative cancellation); the worker registration (`buildTemporalActivities`)
 * wires the Temporal-aware ones.
 */
export function buildTemporalActivity<In extends z.ZodType, Out extends z.ZodType>(
  descriptor: ActivityDescriptor<In, Out>,
  options: ExecuteActivityOptions,
  injections: TemporalActivityInjections = {},
): TemporalActivityFn<In, Out> {
  const activity: TemporalActivityFn<In, Out> = async (input, cachedSession, subjectIds) => {
    const validated = descriptor.input.parse(input) as z.infer<In>;
    // The map fan-out's handle arrives as a deserialized payload (#478): parse it at
    // the boundary like the input (an off-shape handle rejects instead of silently
    // running uncached or half-cached).
    const handle = cachedSession != null ? cachedSessionHandleSchema.parse(cachedSession) : undefined;
    // The workflow-threaded subject ids (#715 slice 1) arrive as a deserialized
    // payload too: boundary-parse them (an off-shape value rejects loudly rather
    // than silently writing subjects-less cache records).
    const subjects = subjectIds != null ? subjectIdsWireSchema.parse(subjectIds) : undefined;
    const provided = injections.contextProvider?.();
    const context =
      subjects !== undefined ? { ...(provided ?? {}), subjectIds: subjects } : provided;
    const signal = injections.cancellationSignal?.();
    const stopHeartbeat = injections.heartbeater?.();
    const runOnce = (session: CachedSessionHandle | undefined): Promise<z.infer<Out>> =>
      executeActivity(descriptor, validated, {
        ...options,
        ...(context !== undefined ? { context } : {}),
        ...(signal !== undefined ? { cancellationSignal: signal } : {}),
        ...(session !== undefined ? { cachedSession: session } : {}),
      });
    try {
      try {
        return await runOnce(handle);
      } catch (error) {
        // The referenced session cache vanished mid-fan-out (expired/deleted, #368).
        // The per-item messages were built WITHOUT the cached reference artifacts
        // (they lived in the cache), so recover by re-running this item ONCE uncached
        // — strip the handle so preparation re-includes the full context. No loop:
        // an uncached run carries no cache reference and cannot raise this again.
        if (
          error instanceof ProviderCacheUnavailableError &&
          handle?.supported === true &&
          handle.style === "reference"
        ) {
          return await runOnce(undefined);
        }
        throw error;
      }
    } catch (error) {
      throw asNonRetryableIfTerminal(error);
    } finally {
      stopHeartbeat?.();
    }
  };
  Object.defineProperty(activity, "name", { value: descriptor.name, configurable: true });
  return activity;
}

/**
 * Build the Temporal activity that prepares a cached session once before a map
 * fan-out (#478; Python `build_cache_prep_activity`). Receives a REPRESENTATIVE
 * item so it can resolve the activity's `cache: "reference"` artifacts (identical
 * across items) for the cached prefix. Stamps `created_at` HERE — activity
 * wall-clock, recorded once in history, replay-stable. The core `prepareSessionCache`
 * is fail-soft for runtime conditions; the one loud failure (a templated system
 * prefix) is terminal, so `asNonRetryableIfTerminal` fails it on attempt 1 instead
 * of letting the map's retry policy back off before the identical failure.
 */
export function buildCachePrepActivity<In extends z.ZodType, Out extends z.ZodType>(
  descriptor: ActivityDescriptor<In, Out>,
  options: ExecuteActivityOptions,
  injections: TemporalActivityInjections = {},
): (input: unknown) => Promise<CachedSessionHandle> {
  const name = cachePrepActivityName(descriptor.name);
  const prepare = async (input: unknown): Promise<CachedSessionHandle> => {
    const validated = descriptor.input.parse(input);
    const warn = injections.warner?.();
    try {
      return await prepareSessionCache(descriptor, {
        provider: options.provider,
        ...(options.registry !== undefined ? { registry: options.registry } : {}),
        ...(options.messages !== undefined ? { messages: options.messages } : {}),
        ...(options.model !== undefined ? { model: options.model } : {}),
        ...(options.providerParams !== undefined ? { providerParams: options.providerParams } : {}),
        createdAt: new Date().toISOString(),
        inputValue: validated,
        ...(options.artifactResolver !== undefined ? { artifactResolver: options.artifactResolver } : {}),
        // The prep call is a real provider call — carry the policy guard (#454).
        ...(options.providerModelGuard !== undefined ? { providerModelGuard: options.providerModelGuard } : {}),
        ...(warn !== undefined ? { onWarning: warn } : {}),
      });
    } catch (error) {
      throw asNonRetryableIfTerminal(error);
    }
  };
  Object.defineProperty(prepare, "name", { value: name, configurable: true });
  return prepare;
}

/**
 * Build the Temporal activity that releases a reference-style cache after a map
 * fan-out (#478; Python `build_cache_release_activity`). Best-effort: the provider's
 * `releaseCachedSession` already swallows its errors (the cache TTL-expires
 * regardless), and the capability is probed defensively — a provider without it is
 * a no-op, never a failure.
 */
export function buildCacheReleaseActivity<In extends z.ZodType, Out extends z.ZodType>(
  descriptor: ActivityDescriptor<In, Out>,
  options: ExecuteActivityOptions,
): (handle: unknown) => Promise<void> {
  const name = cacheReleaseActivityName(descriptor.name);
  const release = async (handle: unknown): Promise<void> => {
    const parsed = cachedSessionHandleSchema.parse(handle);
    await options.provider.releaseCachedSession?.(parsed);
  };
  Object.defineProperty(release, "name", { value: name, configurable: true });
  return release;
}
