/**
 * Sub-workflow observability composition (#756, at the #748 altitude).
 *
 * A composed worker builds ONE observer — from the PARENT spec — and threads it into every
 * registered activity, parent and children alike (exactly like the ONE registry #748
 * composes). Before this check, a child declaring a DIFFERENT observability backend than the
 * parent's was silently ignored: admission passes it (its backend is not "none"), but the
 * runtime never resolves or credential-checks it — parent langfuse + child langsmith ran the
 * child's activities against the parent's langfuse observer with the langsmith declaration
 * dead config (the finder's gap on the #756 review round).
 *
 * {@link assertConsistentComposedObservability} makes divergence a LOUD load-time error,
 * mirroring the #748 registry-type conflict: every composed workflow must either declare the
 * parent's effective backend or declare none at all.
 *
 * ABSENT ≡ `none` ≡ INHERITS: admission's `validateObservability` collapses an absent block
 * and an explicit `type: none` to the same "none" backend (`type || "none"`), so this check
 * treats them identically — both mean "no own declaration" and inherit the parent's observer
 * (under a required-observability policy, admission's closure validation already rejects a
 * none/absent child, so a governed closure never reaches the inherit path silently). Only a
 * child declaring a REAL backend different from the parent's effective backend conflicts.
 * With consistency guaranteed, the parent's backend IS the closure's single effective
 * backend, which is exactly what the #756 required-credential gate evaluates.
 */

import type { RegistrySource } from "./registry-composition.js";

/** Raised when the specs in a composition closure declare observability that cannot compose (#756). */
export class ObservabilityCompositionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ObservabilityCompositionError";
  }
}

/** The spec's effective declared backend — admission's `type || "none"` collapse (empty-string safe). */
function effectiveBackend(source: RegistrySource): string {
  return source.spec.runtime.observability?.type || "none";
}

/**
 * Require every (transitively) referenced child's observability declaration to compose with
 * the parent's: same effective backend, or none/absent (inherits). A divergence is a
 * load-time error naming both workflows and both types — never a silent "use the parent's".
 */
export function assertConsistentComposedObservability(
  parent: RegistrySource,
  children: readonly RegistrySource[],
): void {
  const parentBackend = effectiveBackend(parent);
  for (const child of children) {
    const childBackend = effectiveBackend(child);
    if (childBackend === "none" || childBackend === parentBackend) {
      continue; // Absent/none inherits the parent's observer; a matching declaration agrees.
    }
    throw new ObservabilityCompositionError(
      `sub-workflow observability composition: workflow ${JSON.stringify(child.id)} declares ` +
        `${JSON.stringify(childBackend)} observability but the composed worker builds the parent ` +
        `${JSON.stringify(parent.id)}'s ${JSON.stringify(parentBackend)} observer — a composed worker ` +
        "builds ONE observer, so every composed workflow must declare the parent's observability " +
        "backend (or none, which inherits it) (#756)",
    );
  }
}
