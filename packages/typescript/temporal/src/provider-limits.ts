/**
 * Provider call limits (#529 PR B; Python `execution/controls.py` parity): a
 * per-provider/per-model admission layer around provider calls — `maxConcurrent`
 * (a semaphore) and `minIntervalSeconds` (calls spaced at least this far apart).
 * The limiter protects a shared upstream quota across everything one worker
 * process runs, so `executeActivity` holds a slot for the whole generation loop
 * (all validation attempts + transient retries of one activity execution).
 */

const WAIT_METADATA_THRESHOLD_SECONDS = 0.001;

/** Monotonic seconds (Python `time.monotonic` parity — wall-clock jumps must not skew spacing). */
function monotonicSeconds(): number {
  return performance.now() / 1000;
}

export interface ProviderCallLimits {
  maxConcurrent?: number;
  minIntervalSeconds?: number;
}

function validateLimits(limits: ProviderCallLimits): ProviderCallLimits {
  if (limits.maxConcurrent !== undefined && limits.maxConcurrent < 1) {
    throw new Error("provider limits maxConcurrent must be >= 1");
  }
  if (limits.minIntervalSeconds !== undefined && limits.minIntervalSeconds < 0) {
    throw new Error("provider limits minIntervalSeconds must be >= 0");
  }
  return limits;
}

/** How long one call waited on admission (Python `ProviderCallWait`). */
export interface ProviderCallWait {
  queuedSeconds: number;
  throttledSeconds: number;
  maxConcurrent?: number;
  minIntervalSeconds?: number;
}

/** Observation-metadata view of a wait (Python `ProviderCallWait.to_metadata`). */
export function providerCallWaitMetadata(wait: ProviderCallWait): Record<string, unknown> {
  return {
    queued: wait.queuedSeconds >= WAIT_METADATA_THRESHOLD_SECONDS,
    throttled: wait.throttledSeconds >= WAIT_METADATA_THRESHOLD_SECONDS,
    queued_seconds: wait.queuedSeconds,
    throttled_seconds: wait.throttledSeconds,
    ...(wait.maxConcurrent !== undefined ? { max_concurrent: wait.maxConcurrent } : {}),
    ...(wait.minIntervalSeconds !== undefined ? { min_interval_seconds: wait.minIntervalSeconds } : {}),
  };
}

function abortReason(signal: AbortSignal): unknown {
  return signal.reason ?? new Error("provider-limits wait aborted");
}

function abortableSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortReason(signal));
      return;
    }
    const onAbort = (): void => {
      clearTimeout(timer);
      reject(abortReason(signal as AbortSignal));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/** A plain promise semaphore — JS is single-threaded, so the counter needs no lock. */
class Semaphore {
  private available: number;
  private readonly waiters: (() => void)[] = [];

  constructor(permits: number) {
    this.available = permits;
  }

  /** Aborting while queued removes the waiter (its would-be permit passes on) and rejects. */
  async acquire(signal?: AbortSignal): Promise<void> {
    if (signal?.aborted) {
      throw abortReason(signal);
    }
    if (this.available > 0) {
      this.available -= 1;
      return;
    }
    await new Promise<void>((resolve, reject) => {
      const waiter = (): void => {
        signal?.removeEventListener("abort", onAbort);
        resolve();
      };
      const onAbort = (): void => {
        const index = this.waiters.indexOf(waiter);
        if (index >= 0) {
          this.waiters.splice(index, 1);
          reject(abortReason(signal as AbortSignal));
        }
        // Already handed a permit (removed by release): resolve won the race.
      };
      this.waiters.push(waiter);
      signal?.addEventListener("abort", onAbort, { once: true });
    });
  }

  release(): void {
    const next = this.waiters.shift();
    if (next !== undefined) {
      next(); // hand the permit straight to the next waiter
    } else {
      this.available += 1;
    }
  }
}

/**
 * Serializes provider calls per the configured limits (Python `ProviderCallLimiter`).
 * `limit(fn)` runs `fn` once admitted, passing how long admission took.
 */
export class ProviderCallLimiter {
  readonly limits: ProviderCallLimits;
  private readonly semaphore: Semaphore | undefined;
  private nextCallAt = 0;

  constructor(limits: ProviderCallLimits = {}) {
    this.limits = validateLimits(limits);
    this.semaphore = limits.maxConcurrent !== undefined ? new Semaphore(limits.maxConcurrent) : undefined;
  }

  get enabled(): boolean {
    return this.limits.maxConcurrent !== undefined || Boolean(this.limits.minIntervalSeconds);
  }

  /**
   * Admission is cancellation-aware (Python parity — asyncio task cancellation
   * interrupts its semaphore/sleep waits for free): aborting rejects promptly
   * with the signal's reason and releases any held permit, so a cancelled
   * activity never sits out a quota delay or blocks live work.
   */
  async limit<T>(fn: (wait: ProviderCallWait) => Promise<T>, options?: { signal?: AbortSignal }): Promise<T> {
    const queuedStart = monotonicSeconds();
    if (this.semaphore !== undefined) {
      await this.semaphore.acquire(options?.signal);
    }
    try {
      const queuedSeconds = monotonicSeconds() - queuedStart;
      let throttledSeconds = 0;
      if (this.limits.minIntervalSeconds) {
        throttledSeconds = await this.reserveRateSlot(this.limits.minIntervalSeconds, options?.signal);
      }
      return await fn({
        queuedSeconds,
        throttledSeconds,
        ...(this.limits.maxConcurrent !== undefined ? { maxConcurrent: this.limits.maxConcurrent } : {}),
        ...(this.limits.minIntervalSeconds !== undefined
          ? { minIntervalSeconds: this.limits.minIntervalSeconds }
          : {}),
      });
    } finally {
      this.semaphore?.release();
    }
  }

  /** Reserve the next interval slot; the reservation itself is synchronous, only the wait sleeps. */
  private async reserveRateSlot(minIntervalSeconds: number, signal?: AbortSignal): Promise<number> {
    const now = monotonicSeconds();
    const waitSeconds = Math.max(0, this.nextCallAt - now);
    this.nextCallAt = Math.max(now, this.nextCallAt) + minIntervalSeconds;
    if (waitSeconds > 0) {
      await abortableSleep(waitSeconds * 1000, signal);
    }
    return waitSeconds;
  }
}

export type ProviderPolicySource = "model" | "provider" | "default" | "none";

/** Which limits govern one call, and why (Python `ProviderPolicySelection`). */
export interface ProviderPolicySelection {
  providerName: string;
  providerModel: string | undefined;
  policySource: ProviderPolicySource;
  policyKey: string;
  limits: ProviderCallLimits | undefined;
}

/** Observation-metadata view of a selection (Python `ProviderPolicySelection.to_metadata`). */
export function providerPolicySelectionMetadata(selection: ProviderPolicySelection): Record<string, unknown> {
  return {
    provider_name: selection.providerName,
    policy_source: selection.policySource,
    policy_key: selection.policyKey,
    ...(selection.providerModel !== undefined ? { provider_model: selection.providerModel } : {}),
    ...(selection.limits?.maxConcurrent !== undefined ? { max_concurrent: selection.limits.maxConcurrent } : {}),
    ...(selection.limits?.minIntervalSeconds !== undefined
      ? { min_interval_seconds: selection.limits.minIntervalSeconds }
      : {}),
  };
}

export interface ProviderRateLimitProviderPolicy {
  limits?: ProviderCallLimits;
  models?: Record<string, ProviderCallLimits>;
}

/**
 * Per-provider rate-limit policy (Python `ProviderRateLimitPolicy`): the most
 * specific configuration wins — model > provider > default. (Python's fourth
 * `legacy_default` tier maps its pre-policy worker option, which the TS SDK
 * never had.)
 */
export interface ProviderRateLimitPolicy {
  default?: ProviderCallLimits;
  providers?: Record<string, ProviderRateLimitProviderPolicy>;
}

export function selectProviderPolicy(
  policy: ProviderRateLimitPolicy,
  { providerName, providerModel }: { providerName: string; providerModel?: string },
): ProviderPolicySelection {
  const providerPolicy = policy.providers?.[providerName];
  const modelLimits = providerModel !== undefined ? providerPolicy?.models?.[providerModel] : undefined;
  if (modelLimits !== undefined) {
    return {
      providerName,
      providerModel,
      policySource: "model",
      policyKey: `provider:${providerName}/model:${providerModel}`,
      limits: modelLimits,
    };
  }
  if (providerPolicy?.limits !== undefined) {
    return {
      providerName,
      providerModel,
      policySource: "provider",
      policyKey: `provider:${providerName}`,
      limits: providerPolicy.limits,
    };
  }
  if (policy.default !== undefined) {
    return { providerName, providerModel, policySource: "default", policyKey: "default", limits: policy.default };
  }
  return {
    providerName,
    providerModel,
    policySource: "none",
    policyKey: `none:${providerName}:${providerModel ?? "*"}`,
    limits: undefined,
  };
}

/**
 * Owns one shared `ProviderCallLimiter` per policy key (Python
 * `ProviderRateLimitController`): two activities selecting the same key contend
 * on the SAME semaphore/interval, which is the whole point of the limits.
 */
export class ProviderRateLimitController {
  readonly policy: ProviderRateLimitPolicy;
  private readonly limiters = new Map<string, ProviderCallLimiter>();

  constructor(policy: ProviderRateLimitPolicy) {
    this.policy = policy;
    // Fail loud at construction, not first call (Python validates in __post_init__).
    if (policy.default !== undefined) validateLimits(policy.default);
    for (const provider of Object.values(policy.providers ?? {})) {
      if (provider.limits !== undefined) validateLimits(provider.limits);
      for (const limits of Object.values(provider.models ?? {})) validateLimits(limits);
    }
  }

  select(params: { providerName: string; providerModel?: string }): ProviderPolicySelection {
    return selectProviderPolicy(this.policy, params);
  }

  /** The shared limiter for a selection; undefined when no limits govern the call. */
  limiterFor(selection: ProviderPolicySelection): ProviderCallLimiter | undefined {
    if (selection.limits === undefined) {
      return undefined;
    }
    let limiter = this.limiters.get(selection.policyKey);
    if (limiter === undefined) {
      limiter = new ProviderCallLimiter(selection.limits);
      this.limiters.set(selection.policyKey, limiter);
    }
    return limiter.enabled ? limiter : undefined;
  }
}
