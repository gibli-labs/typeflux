/**
 * Code-bindings module import — the ONE loader the `typeflux-yaml-worker` entrypoint
 * and the `typeflux-project erase` CLI share (#715 slice-5 fix round, item 3).
 *
 * A bindings module supplies the code-defined pieces a manifest cannot (schema
 * resolvers, providers, the shared `subjectKeystore`, the `cacheStore`, ...). It may
 * export them either as NAMED exports or under a single `export default { ... }` —
 * both consumers must accept both shapes, or a deployed worker could USE a backend
 * that a differently-shaped reader (the erase CLI) reports as absent: the erasure
 * receipt would then claim an honest "skipped" while the deployment under-erases.
 * Sharing the unwrap here makes that divergence structurally impossible.
 */

import { resolve as resolvePath } from "node:path";
import { pathToFileURL } from "node:url";

/**
 * Dynamic-import `modulePath` and unwrap a `default` export when present
 * (`default ?? module`, the worker-entry contract): the returned record is the
 * surface consumers read their named pieces from.
 */
export async function importBindingsModule(modulePath: string): Promise<Record<string, unknown>> {
  const imported: unknown = await import(pathToFileURL(resolvePath(modulePath)).href);
  const moduleExports = imported as Record<string, unknown>;
  const defaulted = moduleExports["default"];
  return (defaulted ?? moduleExports) as Record<string, unknown>;
}
