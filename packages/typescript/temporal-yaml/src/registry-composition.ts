/**
 * Sub-workflow registry composition (#748).
 *
 * A composed worker serves ONE runtime prompt registry, and every sub-workflow it runs
 * (`workflow:` / `map.workflow`, transitively) resolves its prompts against that single
 * registry. Before #748 that forced every child prompt to be DUPLICATED into the parent's
 * `runtime.registry` — a drift hazard the SDK's own composition example (and an adopter's
 * consumer) had to police with a hand-rolled parity test.
 *
 * {@link composeRuntimeRegistry} makes the platform guarantee: the served registry is the
 * MERGE of the parent's registry and every (transitively) referenced child's registry.
 *   - A prompt name present in only one spec is merged in.
 *   - A prompt name present in several is merged ONCE when the entries are byte-identical
 *     (canonical-JSON equality); a genuine conflict is a LOUD load-time error naming the
 *     prompt, both source workflows, and the first differing field.
 *   - INLINE registries are merged by prompt map. For non-inline registries (langfuse/
 *     langsmith/custom) there is nothing to merge — the backend serves prompts by name —
 *     but a composed worker still serves ONE registry, so a child declaring a DIFFERENT
 *     registry type or backend config (label/host) than the parent is a load-time error:
 *     conflicting external registry configs must fail loudly, not silently pick the parent's.
 */

import {
  canonicalJson,
  InlinePromptRegistry,
  type InlinePromptValue,
  type PromptRegistry,
  type RegistryTransport,
} from "@typeflux/temporal";

import { inlinePromptValueFromSpec, registryFromSpec } from "./build-activities.js";
import type { InlinePromptSpec, TypefluxYamlSpec } from "./spec.js";

/** A spec that contributes to the composed registry, labelled for error messages. */
export interface RegistrySource {
  /** The workflow's manifest id (or name) — what a conflict error points at. */
  id: string;
  spec: TypefluxYamlSpec;
}

export interface ComposeRuntimeRegistryOptions {
  /** The parent workflow, whose registry type/config is authoritative for the served registry. */
  parent: RegistrySource;
  /**
   * The (transitively) referenced sub-workflows, in resolution order. Empty ⇒ the composed
   * registry is exactly the parent's (byte-identical to the pre-#748 single-spec behavior).
   */
  children: readonly RegistrySource[];
  /** The registry transport for a non-inline parent registry (langfuse/langsmith/custom). */
  transport?: RegistryTransport | undefined;
  /** A directly-injected registry, the fallback when a non-inline registry builds no transport. */
  injected?: PromptRegistry | undefined;
}

/** Raised when the specs in a composition closure declare registries that cannot be merged (#748). */
export class RegistryCompositionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RegistryCompositionError";
  }
}

/**
 * Compose the single registry a composed worker serves from the parent's registry and every
 * referenced child's registry (see the module doc). With no children the result is exactly
 * `registryFromSpec(parent.spec, transport) ?? injected` — unchanged single-spec behavior.
 */
export function composeRuntimeRegistry(options: ComposeRuntimeRegistryOptions): PromptRegistry | undefined {
  const { parent, children, transport, injected } = options;
  // Every child must declare a registry compatible with the ONE the worker serves. This
  // runs even for an inline parent, so a child declaring a non-inline registry (which the
  // inline worker could never serve) fails loudly instead of being silently dropped.
  for (const child of children) {
    assertCompatibleRegistryConfig(parent, child);
  }
  if (parent.spec.runtime.registry.type === "inline") {
    // Inline: merge the prompt maps (byte-identical dedupe, loud conflicts). The injected
    // registry is ignored exactly as `registryFromSpec` ignores it for an inline spec.
    return new InlinePromptRegistry(mergeInlinePrompts(parent, children));
  }
  // Non-inline: one backend serves every workflow's prompts by name; the config-consistency
  // check above already guaranteed the closure agrees on that backend. Build it as the
  // single-spec path does.
  return registryFromSpec(parent.spec, transport) ?? injected;
}

/**
 * A composed worker serves ONE registry, so a child's registry must be of the SAME type as
 * the parent's, and for a non-inline (external) backend the SAME config (label/host). A
 * divergence is a load-time error — never a silent "use the parent's".
 */
function assertCompatibleRegistryConfig(parent: RegistrySource, child: RegistrySource): void {
  const parentRegistry = parent.spec.runtime.registry;
  const childRegistry = child.spec.runtime.registry;
  if (parentRegistry.type !== childRegistry.type) {
    throw new RegistryCompositionError(
      `sub-workflow registry composition: workflow ${JSON.stringify(child.id)} declares a ` +
        `${JSON.stringify(childRegistry.type)} registry but the composed worker serves the parent ` +
        `${JSON.stringify(parent.id)}'s ${JSON.stringify(parentRegistry.type)} registry — a composed worker ` +
        "serves ONE registry, so every composed workflow must declare the same registry type (#748)",
    );
  }
  if (parentRegistry.type === "inline") {
    return; // Inline configs never conflict at the config level — only per-prompt (handled below).
  }
  // External backend identity: the fields that select which backend/label serves the prompts.
  // `prompts` is inline-only, so it is intentionally excluded from the backend-config compare.
  const parentConfig = canonicalJson({ label: parentRegistry.label ?? null, host: parentRegistry.host ?? null });
  const childConfig = canonicalJson({ label: childRegistry.label ?? null, host: childRegistry.host ?? null });
  if (parentConfig !== childConfig) {
    throw new RegistryCompositionError(
      `sub-workflow registry composition: workflow ${JSON.stringify(child.id)} declares a ` +
        `${JSON.stringify(childRegistry.type)} registry with a DIFFERENT backend config (label/host) than ` +
        `the parent ${JSON.stringify(parent.id)} — a composed worker serves ONE registry, so conflicting ` +
        "external registry configs must be reconciled, not silently resolved to the parent's (#748)",
    );
  }
}

/**
 * Merge the parent's and every child's inline prompt map into one. First declaration of a
 * name wins the built value; a later declaration of the SAME name is tolerated only when it
 * is byte-identical (canonical JSON) — a conflict is a loud error naming both workflows and
 * the first differing field.
 */
function mergeInlinePrompts(parent: RegistrySource, children: readonly RegistrySource[]): Record<string, InlinePromptValue> {
  const merged: Record<string, InlinePromptValue> = Object.create(null);
  const provenance = new Map<string, { source: string; canonical: string; value: string | InlinePromptSpec }>();
  for (const source of [parent, ...children]) {
    for (const [name, value] of Object.entries(source.spec.runtime.registry.prompts ?? {})) {
      const canonical = canonicalJson(value);
      const prior = provenance.get(name);
      if (prior === undefined) {
        provenance.set(name, { source: source.id, canonical, value });
        merged[name] = inlinePromptValueFromSpec(name, value);
        continue;
      }
      if (prior.canonical === canonical) {
        continue; // Byte-identical duplicate — already merged once.
      }
      throw new RegistryCompositionError(
        `sub-workflow registry composition: prompt ${JSON.stringify(name)} is declared with DIFFERENT ` +
          `definitions by workflows ${JSON.stringify(prior.source)} and ${JSON.stringify(source.id)} ` +
          `(first differing field: ${firstDifferingField(prior.value, value, "", [prior.source, source.id])}) — ` +
          "a composed worker serves ONE registry, so a shared prompt name must be byte-identical across " +
          "composed workflows or use a distinct name (#748)",
      );
    }
  }
  return merged;
}

/**
 * `canonicalJson` with ABSENT (`undefined`) as a first-class comparable state: the walk
 * below compares union-of-keys / union-of-indexes, so one side is legitimately absent when
 * an optional field (`model`, `provider_params`, …) is set in only one spec — and
 * `canonicalJson(undefined)` itself throws. The NUL-prefixed sentinel can never collide
 * with a real canonical-JSON string.
 */
function canonicalOrAbsent(value: unknown): string {
  return value === undefined ? "\u0000absent" : canonicalJson(value);
}

/**
 * The path (dotted keys / `[i]` indices) of the first place two prompt values diverge, for
 * the conflict message. Walks objects in sorted-key order so the report is deterministic;
 * returns `"(value)"` when the divergence is the top-level value itself (e.g. two different
 * bare prompt strings). A field present in only ONE source is a diff in its own right,
 * reported as `path (set in "A", absent in "B")` — `labels` names the two sources in
 * `[a, b]` order (generic wording when omitted).
 */
export function firstDifferingField(
  a: unknown,
  b: unknown,
  path = "",
  labels?: readonly [string, string],
): string {
  const here = path === "" ? "(value)" : path;
  if (canonicalOrAbsent(a) === canonicalOrAbsent(b)) {
    return here; // No divergence at or below this node — report the node itself.
  }
  // Exactly one side absent: there is nothing to descend into, so this IS the diff.
  if (a === undefined || b === undefined) {
    const [aLabel, bLabel] = labels ?? ["one workflow", "the other"];
    const quote = (label: string): string => (labels !== undefined ? JSON.stringify(label) : label);
    const [setIn, absentIn] = a === undefined ? [bLabel, aLabel] : [aLabel, bLabel];
    return `${here} (set in ${quote(setIn)}, absent in ${quote(absentIn)})`;
  }
  if (isPlainRecord(a) && isPlainRecord(b)) {
    const keys = [...new Set([...Object.keys(a), ...Object.keys(b)])].sort();
    for (const key of keys) {
      const childPath = path === "" ? key : `${path}.${key}`;
      const av = a[key];
      const bv = b[key];
      if (canonicalOrAbsent(av) !== canonicalOrAbsent(bv)) {
        return firstDifferingField(av, bv, childPath, labels);
      }
    }
    return here;
  }
  if (Array.isArray(a) && Array.isArray(b)) {
    const len = Math.max(a.length, b.length);
    for (let i = 0; i < len; i += 1) {
      const av = i < a.length ? a[i] : undefined;
      const bv = i < b.length ? b[i] : undefined;
      if (canonicalOrAbsent(av) !== canonicalOrAbsent(bv)) {
        return firstDifferingField(av, bv, `${path}[${i}]`, labels);
      }
    }
    return here;
  }
  return here;
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
