/**
 * Bundle LINKS (#575; Python `project/bundle.py` `_bundle_links` minus the live-Langfuse
 * derivation): operator deep links into external UIs. `TEMPORAL_UI_URL` /
 * `LANGFUSE_PROJECT_URL` are read from the environment (validated http(s); anything else is
 * warned about and hidden — a bad URL must never render as a clickable link), and the
 * Temporal Web base is derived from the resolved Temporal address when the env var doesn't
 * pin one (cloud address -> Temporal Cloud, localhost -> the local dev UI).
 *
 * The registry-derived Langfuse project URL (Python `_langfuse_project_url`) needs a live
 * Langfuse API call and stays deferred to #573 — the env var is the only source here.
 *
 * Env reads are live: run under the selected environment's overlay
 * (`withEnvironmentContext`), like every other configured-now check in the bundle.
 */

import type { TypefluxYamlSpec } from "@typeflux/temporal-yaml";

/** Python `BundleLinks` (both optional; the whole object is omitted when empty). */
export interface ApiBundleLinks {
  temporal_ui?: string;
  langfuse_project?: string;
}

const EXTERNAL_LINK_ENV_VARS = {
  temporal_ui: "TEMPORAL_UI_URL",
  langfuse_project: "LANGFUSE_PROJECT_URL",
} as const;

/** Python `_temporal_ui_url`: cloud address -> Temporal Cloud; local -> the dev UI; else nothing. */
function temporalUiUrl(address: string | undefined): string | undefined {
  if (address === undefined || address === "") return undefined;
  const host = (address.split(":", 1)[0] ?? "").trim().toLowerCase();
  if (host.includes("tmprl.cloud")) return "https://cloud.temporal.io";
  if (host === "localhost" || host === "127.0.0.1" || host === "0.0.0.0") return "http://localhost:8233";
  return undefined;
}

function validExternalUrl(raw: string, variable: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    console.warn(`ignoring ${variable}: not an http(s) URL; external links stay hidden`);
    return false;
  }
  if ((parsed.protocol !== "http:" && parsed.protocol !== "https:") || parsed.host === "") {
    console.warn(`ignoring ${variable}: not an http(s) URL; external links stay hidden`);
    return false;
  }
  // The bundle is secret-safe: a URL carrying userinfo (`https://token@host`) would publish
  // the credential through the control-plane API — reject it whole (fail closed, like the
  // scheme check) rather than stripping and guessing at the operator's intent.
  if (parsed.username !== "" || parsed.password !== "") {
    console.warn(`ignoring ${variable}: URL embeds credentials; external links stay hidden`);
    return false;
  }
  return true;
}

/** Build the bundle's `links`, or `undefined` when nothing resolves (the field is then omitted). */
export function buildBundleLinks(spec: TypefluxYamlSpec): ApiBundleLinks | undefined {
  const values: ApiBundleLinks = {};
  for (const [field, variable] of Object.entries(EXTERNAL_LINK_ENV_VARS) as [
    keyof ApiBundleLinks,
    string,
  ][]) {
    const raw = (Object.hasOwn(process.env, variable) ? (process.env[variable] ?? "") : "").trim();
    if (raw === "" || !validExternalUrl(raw, variable)) continue;
    values[field] = raw;
  }
  // The Temporal Web base is per-environment: derive it from the resolved temporal address so a
  // cloud environment links to Temporal Cloud and a localhost one to the local UI. An explicit
  // TEMPORAL_UI_URL wins (self-hosted UIs).
  if (values.temporal_ui === undefined) {
    const derived = temporalUiUrl(spec.runtime.temporal.address ?? "localhost:7233");
    if (derived !== undefined) values.temporal_ui = derived;
  }
  return Object.keys(values).length > 0 ? values : undefined;
}
