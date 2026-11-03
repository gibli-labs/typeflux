/** Shared automatic service wiring; orchestration adapters retain engine admission. */
import { runtimeEngine, validateLocalCapabilities, requireTemporalWorkflowGraph } from "./engine.js";
import { enforcePolicyPreflight, type YamlRuntimeOptions } from "./runtime-assembly.js";
import type { TypefluxYamlSpec } from "./spec.js";
import { langfuseObserverFromSpec } from "./langfuse-observer.js";
import { langfuseRegistryTransportFromSpec } from "./langfuse-registry.js";
import { langsmithObserverFromSpec } from "./langsmith-observer.js";
import { langsmithRegistryTransportFromSpec } from "./langsmith-registry.js";
import { providerTransportsFromSpec } from "./provider-transports-from-env.js";

/** The auto-observer the OOTB wiring built (kept for the graceful-exit drain). */
type AutoWiredObserver =
  | NonNullable<Awaited<ReturnType<typeof langfuseObserverFromSpec>>>
  | NonNullable<Awaited<ReturnType<typeof langsmithObserverFromSpec>>>;

/**
 * The ONE out-of-the-box options assembly (Python `build_runtime` parity), shared by
 * {@link buildRuntime} and the `typeflux-yaml-worker` entrypoint's `--preflight` (#687
 * review: preflight MUST compose exactly the wiring the live worker runs with, or the
 * generated command fails supported OOTB deployments before the readiness marker):
 *
 * - Governance pre-flight FIRST, even before the auto-observer: a non-compliant spec
 *   must refuse to build before ANYTHING is constructed — including the langfuse SDK
 *   client — and its error must be the policy violation, never a missing-peer install
 *   hint (finder). Idempotent; assembleYamlRuntime re-runs it as its own fail-closed
 *   contract. Includes the closure admission (#300/#298) — cascade + tree-wide ceilings.
 * - Out-of-the-box langfuse/langsmith OBSERVERS: a spec declaring either tracing backend
 *   traces with NO caller wiring — the official SDK is lazy-loaded and the observer
 *   built from the environment (each helper self-gates on its own type). An explicit
 *   `observer` always wins; `type: none` is unchanged.
 * - Out-of-the-box langfuse/langsmith PROMPT REGISTRIES (Python `_build_registry`):
 *   an explicit `registry`/`registryTransport` wins; `inline` self-builds in
 *   `registryFromSpec`. Unlike the observer, a declared-but-unbuildable registry THROWS
 *   inside the helper — prompts are load-bearing, so there is no untraced-style degrade.
 * - Out-of-the-box vendor PROVIDERS (Python `_build_provider`): openai/anthropic/gemini
 *   execute with NO caller wiring — the official SDK is lazy-loaded and the thin
 *   transport built from the environment. An explicit `provider`/`transports` wins.
 *   Like the registry, a missing credential THROWS: a run cannot degrade past its
 *   provider.
 *
 * Returns the assembled options plus the auto-built observer (for the drain helper).
 */
export async function autoWireRuntimeOptions<T extends YamlRuntimeOptions>(
  spec: TypefluxYamlSpec,
  options: T,
): Promise<{ options: T; autoObserver: AutoWiredObserver | undefined }> {
  if (options.policy !== undefined) {
    enforcePolicyPreflight(spec, options.policy, options.subworkflows);
  }
  if (runtimeEngine(spec) === "local") validateLocalCapabilities(spec, options.subworkflows);
  else requireTemporalWorkflowGraph(spec, options.subworkflows);
  const autoObserver =
    options.observer === undefined
      ? ((await langfuseObserverFromSpec(spec)) ?? (await langsmithObserverFromSpec(spec)))
      : undefined;
  try {
    const autoRegistryTransport =
      options.registry === undefined && options.registryTransport === undefined
        ? ((await langfuseRegistryTransportFromSpec(spec)) ?? (await langsmithRegistryTransportFromSpec(spec)))
        : undefined;
    const autoTransports =
      options.provider === undefined && options.transports === undefined
        ? await providerTransportsFromSpec(spec)
        : undefined;
    return {
      options: {
        ...options,
        ...(autoObserver !== undefined ? { observer: autoObserver } : {}),
        ...(autoRegistryTransport !== undefined ? { registryTransport: autoRegistryTransport } : {}),
        ...(autoTransports !== undefined ? { transports: autoTransports } : {}),
      },
      autoObserver: autoObserver ?? undefined,
    };
  } catch (error) {
    try { await autoObserver?.shutdown(); }
    catch (cleanupError) { console.error("observability drain failed:", cleanupError); }
    throw error;
  }
}

