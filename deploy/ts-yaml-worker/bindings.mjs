/**
 * Reference worker bindings for the ts-yaml-worker image (#687).
 *
 * The TS runtime resolves a spec's `schemas:` refs and its model provider from COMPILED code
 * (Python resolves them via importlib at runtime). `typeflux-yaml-worker` dynamic-imports this
 * module via `TYPEFLUX_WORKER_BINDINGS` and feeds its exports to `assembleYamlRuntime` /
 * `buildRuntime`. Fork this file to your project's schemas + provider.
 *
 * `schemas` is keyed by the manifest's full schema ref (`schemas:Note`).
 * `provider` is a scripted, offline `ModelProvider` so the reference image runs with no API key.
 */

import { z } from "zod";

export const schemas = {
  "schemas:Note": z.object({ text: z.string() }),
  "schemas:Summary": z.object({ summary: z.string() }),
};

export const provider = {
  providerName: "fake",
  // Offline scripted response — replace with your real provider (openai/anthropic/…) in a fork.
  structuredCall() {
    return { summary: "reference ts-yaml-worker: scripted response (replace this provider in your fork)" };
  },
};
