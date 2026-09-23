/**
 * The `runtime.temporal.tls` → `@temporalio/*` TLS options mapping (#685; Python
 * `yaml/tls.py` `build_temporal_tls_config`). One place resolves the structured TLS
 * block (custom CA / client mTLS certs) to connect-time options, so every connection
 * surface — the control plane's Temporal tier and any caller constructing a
 * `Connection`/`NativeConnection` from a parsed spec — maps identically.
 *
 * A boolean passes through untouched (`tls: true` = system trust roots, `tls: false`
 * = plaintext); `undefined` materializes Python's `tls = False` default. A structured
 * block resolves each cert slot from its inline secret reference
 * (`value_from: {env|file}`) or its `*_file` path — the spec forbids both per slot —
 * reading file BYTES with `~` expansion and failing loudly on a missing, unreadable,
 * or empty source.
 *
 * The result is STRUCTURAL (`TLSConfig` shape: `serverNameOverride`,
 * `serverRootCACertificate`, `clientCertPair: {crt, key}` — verified against
 * `@temporalio/common`'s `tls-config.d.ts`) so this module carries no `@temporalio`
 * dependency.
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

import { type SecretValueSpec, temporalTlsSpec, type TemporalTlsSpec } from "./spec.js";

/** The `@temporalio/common` `TLSConfig` shape (structural — no dependency). */
export interface TemporalTlsOptions {
  serverNameOverride?: string;
  serverRootCACertificate?: Buffer;
  clientCertPair?: { crt: Buffer; key: Buffer };
}

/** `~`-expansion for a configured path (the ts driver's secret-file rule). */
const expandHome = (path: string): string =>
  path.startsWith("~") ? join(homedir(), path.slice(1).replace(/^\//, "")) : path;

/** Python `_read_optional_tls_file`: file BYTES, `~` expanded, loud on failure. */
function readTlsFile(path: string | undefined, fieldName: string): Buffer | undefined {
  if (path === undefined) return undefined;
  try {
    return readFileSync(expandHome(path));
  } catch {
    throw new Error(`could not read runtime.temporal.tls.${fieldName}: ${path}`);
  }
}

/** Python `_resolve_secret_bytes`: env text (trimmed, utf-8) or file bytes; a
 * required-but-missing/empty source is a loud config error, optional yields nothing. */
function resolveSecretBytes(
  value: SecretValueSpec | undefined,
  runtimePath: string,
  environment: Record<string, string | undefined>,
): Buffer | undefined {
  if (value === undefined) return undefined;
  const { env, file, required } = value.value_from;
  const isRequired = required ?? true;
  if (env !== undefined) {
    // The INJECTED environment, not process.env: the CP's env override must
    // govern TLS secrets exactly as it governs the api_key (codex).
    const resolved = environment[env];
    const normalized = resolved?.trim();
    if (normalized === undefined || normalized === "") {
      if (isRequired) {
        throw new Error(
          resolved === undefined
            ? `missing required secret for ${runtimePath}: env ${env} is not set`
            : `secret for ${runtimePath} from env ${env} is empty`,
        );
      }
      return undefined;
    }
    return Buffer.from(normalized, "utf8");
  }
  if (file !== undefined) {
    const secretFile = expandHome(file);
    let raw: Buffer;
    try {
      raw = readFileSync(secretFile);
    } catch {
      if (isRequired) {
        throw new Error(`missing required secret for ${runtimePath}: file ${file} does not exist`);
      }
      return undefined;
    }
    if (raw.length === 0) {
      if (isRequired) throw new Error(`secret for ${runtimePath} from file ${file} is empty`);
      return undefined;
    }
    return raw;
  }
  return undefined;
}

/** One cert slot: the inline secret reference wins, else the `*_file` path (the spec
 * forbids configuring both). */
function certSlot(
  inline: SecretValueSpec | undefined,
  filePath: string | undefined,
  slot: string,
  environment: Record<string, string | undefined>,
): Buffer | undefined {
  return (
    resolveSecretBytes(inline, `runtime.temporal.tls.${slot}`, environment) ??
    readTlsFile(filePath, `${slot}_file`)
  );
}

/**
 * Map the spec's `tls` value to what `Connection.connect` / `NativeConnection.connect`
 * take as `tls`. Booleans pass through; a structured block is re-validated (so a raw,
 * unparsed mapping still fails loudly on unknown keys or an incomplete mTLS pair) and
 * resolved to real cert bytes.
 */
export function temporalTlsOptions(
  tls: boolean | TemporalTlsSpec | undefined,
  environment: Record<string, string | undefined> = process.env,
): boolean | TemporalTlsOptions {
  if (tls === undefined) return false; // Python TemporalSpec default: tls = False
  if (typeof tls === "boolean") return tls;
  const parsed = temporalTlsSpec.safeParse(tls);
  if (!parsed.success) {
    throw new Error(
      `invalid runtime.temporal.tls block: ${parsed.error.issues.map((issue) => issue.message).join("; ")}`,
    );
  }
  const block = parsed.data;
  const serverRootCACertificate = certSlot(
    block.server_root_ca_cert,
    block.server_root_ca_cert_file,
    "server_root_ca_cert",
    environment,
  );
  const crt = certSlot(block.client_cert, block.client_cert_file, "client_cert", environment);
  const key = certSlot(
    block.client_private_key,
    block.client_private_key_file,
    "client_private_key",
    environment,
  );
  // The spec pairs the DECLARATIONS; this pairs the RESOLUTIONS — a half-resolved
  // mTLS pair (e.g. one side optional-and-unset) must not silently weaken to
  // server-auth-only TLS.
  if ((crt !== undefined) !== (key !== undefined)) {
    throw new Error(
      "runtime.temporal.tls resolved a client certificate without its private key " +
        "(or vice versa) — an mTLS pair must resolve together",
    );
  }
  return {
    ...(block.domain !== undefined ? { serverNameOverride: block.domain } : {}),
    ...(serverRootCACertificate !== undefined ? { serverRootCACertificate } : {}),
    ...(crt !== undefined && key !== undefined ? { clientCertPair: { crt, key } } : {}),
  };
}

/** Spec-derived Temporal connection options (the CP `executions.ts` twin; Python `binding_ts.py`). */
export interface TemporalConnectionOptions {
  address: string;
  namespace: string;
  /** A boolean, or the resolved structured TLS options (custom CA / mTLS certs, #685). */
  tls: boolean | TemporalTlsOptions;
  apiKey?: string | undefined;
}

/**
 * Map a spec's `runtime.temporal` block to what `Connection.connect` /
 * `NativeConnection.connect` take, failing CLOSED on anything the connection cannot honor
 * (#687 review — a Temporal Cloud/mTLS deployment must never silently downgrade to a
 * plaintext-unauthenticated dial while its rendered manifest declares TLS):
 *
 * - `tls` resolves through {@link temporalTlsOptions} — an invalid block or unreadable
 *   cert file is a config error, never a silently-weakened `tls: true`.
 * - `api_key` resolves from its literal or `value_from: {env|file}` secret reference; a
 *   required-but-unset source is a config error, never an anonymous connection.
 *
 * The control plane's `temporalConnectionOptions` (`temporal-controlplane/executions.ts`)
 * is this mapping wrapped in the CP's 422 error type.
 */
export function temporalConnectionOptions(
  temporal: {
    address?: string | undefined;
    namespace?: string | undefined;
    tls?: boolean | TemporalTlsSpec | undefined;
    api_key?: string | SecretValueSpec | undefined;
  },
  environment: Record<string, string | undefined> = process.env,
): TemporalConnectionOptions {
  const tls = temporalTlsOptions(temporal.tls, environment);
  let apiKey: string | undefined;
  const rawKey = temporal.api_key;
  if (typeof rawKey === "string") {
    apiKey = rawKey === "" ? undefined : rawKey;
  } else if (rawKey !== undefined) {
    // `value_from: {env | file, required?}` — the ts-driver's secret rules (binding_ts.py):
    // env reads the INJECTED environment (a deployment secret; environment-file values
    // arrive via ${VAR} interpolation at spec load); file reads + trims, with ~ expansion.
    const { env: envName, file: fileName, required } = rawKey.value_from;
    const isRequired = required ?? true;
    if (envName !== undefined) {
      const resolved = environment[envName];
      const normalized = resolved?.trim();
      if (normalized === undefined || normalized === "") {
        if (isRequired) {
          throw new Error(
            `environment variable '${envName}' is required by runtime.temporal.api_key and is not set`,
          );
        }
      } else {
        apiKey = normalized;
      }
    } else if (fileName !== undefined) {
      const secretFile = expandHome(fileName);
      let raw: string | undefined;
      try {
        raw = readFileSync(secretFile, "utf8").trim();
      } catch {
        if (isRequired) {
          throw new Error(
            `secret file '${secretFile}' required by runtime.temporal.api_key does not exist`,
          );
        }
      }
      if (raw !== undefined) {
        if (raw === "" && isRequired) {
          throw new Error(`secret file '${secretFile}' for runtime.temporal.api_key is empty`);
        }
        apiKey = raw === "" ? undefined : raw;
      }
    }
  }
  return {
    address: temporal.address ?? "localhost:7233",
    namespace: temporal.namespace ?? "default",
    tls,
    apiKey,
  };
}
