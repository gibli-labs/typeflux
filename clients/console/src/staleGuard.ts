/**
 * A last-write-wins guard for manual (button-triggered) fetches that live outside TanStack
 * Query (#786). Query discards out-of-order responses for reactive fetches; an imperative
 * check needs the same property or a response that raced a selection change lands as if it
 * belonged to the new selection.
 *
 * `next()` marks a new request and returns an `isCurrent` predicate for its callbacks;
 * `invalidate()` marks every in-flight request stale without starting a new one (call it when
 * the selection the results are keyed by changes).
 */
export interface StaleGuard {
  next(): () => boolean;
  /**
   * Observe without arming: the returned predicate stays true until ANY later `next()` or
   * `invalidate()`. For deferred follow-ups (an auto-refresh after an await) that must be
   * skipped when the request identity changed — or a newer check ran — while they waited.
   */
  observe(): () => boolean;
  invalidate(): void;
}

export function createStaleGuard(): StaleGuard {
  let seq = 0;
  return {
    next() {
      const mine = ++seq;
      return () => seq === mine;
    },
    observe() {
      const seen = seq;
      return () => seq === seen;
    },
    invalidate() {
      seq += 1;
    },
  };
}
