/**
 * Activity-map assembly (parity Epic 3, #450). Turns a list of activity
 * registrations into the `{ [name]: fn }` map a Temporal worker passes to
 * `Worker.create({ activities })`. Each function is built with the ambient Temporal
 * context provider, so a worker-run activity sees workflowId/runId/attempt/… in its
 * hook context (and a standalone call does not).
 */

import { cachePrepActivityName, cacheReleaseActivityName, type ActivityDescriptor, type ExecuteActivityOptions } from "@typeflux/temporal";
import type { z } from "zod";

import {
  buildCachePrepActivity,
  buildCacheReleaseActivity,
  buildTemporalActivity,
  type TemporalActivityFn,
} from "./build-temporal-activity.js";
import {
  currentActivityCancellationSignal,
  currentActivityContext,
  currentActivityHeartbeater,
  currentActivityWarner,
} from "./temporal-context.js";

/**
 * One activity to register: its descriptor plus the per-activity execution options.
 *
 * The descriptor is typed `ActivityDescriptor<any, any>` on purpose: an activity's
 * `hook` is contravariant in its input, so a concretely-typed hooked descriptor
 * (`ActivityDescriptor<{...}, {...}>`) is NOT assignable to a single
 * `ActivityDescriptor<z.ZodType, z.ZodType>` element type under `strictFunctionTypes`
 * — which would reject the very context-aware hooks this map exists to register. A
 * heterogeneous registry has no safe common supertype, so `any` erases the variance
 * at this boundary; the produced functions are still `(input: unknown) => …`.
 */
export interface TemporalActivityRegistration {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  descriptor: ActivityDescriptor<any, any>;
  options: ExecuteActivityOptions;
}

/**
 * Build the activities map keyed by `descriptor.name`. Throws on a duplicate name
 * (Temporal registers by name, so a silent overwrite would drop an activity).
 */
export function buildTemporalActivities(
  registrations: readonly TemporalActivityRegistration[],
): Record<string, TemporalActivityFn<z.ZodType, z.ZodType>> {
  // A null-prototype map so a name like "__proto__" is an ordinary own key — a plain
  // `{}` would route it through Object.prototype's accessor: the dedupe guard would
  // miss and the assignment would silently drop the activity.
  const activities: Record<string, TemporalActivityFn<z.ZodType, z.ZodType>> = Object.create(null);
  for (const { descriptor, options } of registrations) {
    if (Object.hasOwn(activities, descriptor.name)) {
      throw new Error(`duplicate activity name: ${JSON.stringify(descriptor.name)}`);
    }
    activities[descriptor.name] = buildTemporalActivity(descriptor, options, {
      contextProvider: currentActivityContext,
      heartbeater: currentActivityHeartbeater,
      cancellationSignal: currentActivityCancellationSignal,
    });
    // A session-cached activity registers its prep/release companions (#478): the map
    // step brackets the fan-out with `<name>.__prepare_cache__` / `<name>.__release_cache__`.
    // Only when ENABLED (a disabled cache never schedules them), and with the same
    // duplicate guard as main activities — a user activity that happens to carry a
    // companion name must collide loudly, never be silently shadowed.
    if (descriptor.sessionCache?.enabled === true) {
      const companions: [string, TemporalActivityFn<z.ZodType, z.ZodType>][] = [
        [
          cachePrepActivityName(descriptor.name),
          buildCachePrepActivity(descriptor, options, { warner: currentActivityWarner }) as TemporalActivityFn<
            z.ZodType,
            z.ZodType
          >,
        ],
        [
          cacheReleaseActivityName(descriptor.name),
          buildCacheReleaseActivity(descriptor, options) as TemporalActivityFn<z.ZodType, z.ZodType>,
        ],
      ];
      for (const [name, fn] of companions) {
        if (Object.hasOwn(activities, name)) {
          throw new Error(`duplicate activity name: ${JSON.stringify(name)}`);
        }
        activities[name] = fn;
      }
    }
  }
  return activities;
}
