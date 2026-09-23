#!/usr/bin/env node
/**
 * CLI entry for the TS control-plane HTTP server (#620), published as the
 * `typeflux-controlplane` bin (#807): `typeflux-controlplane serve --registry <path> --port <n>`
 * (the leading `serve` subcommand mirrors the Python CLI; it is optional for the bare
 * `node dist/http/serve.js` invocation). There is deliberately no `openapi` subcommand —
 * the Python control plane is the contract's normative generator. Loads a `typeflux.projects.yaml` registry and serves its projects over the read tier
 * (no auth in this slice). The embedding server supplies activity IO schemas for bundle/catalog; the
 * `--conformance-schemas` flag injects the canonical conformance fixture project's schemas so the
 * conformance harness can black-box this server without a real host wiring up schemas.
 *
 * Flags:
 *   --registry <path>        (required) the typeflux.projects.yaml to serve
 *   --port <n>               (required) TCP port to listen on
 *   --host <addr>            interface to bind (default 127.0.0.1)
 *   --conformance-schemas    inject the conformance fixture project's activity IO schemas
 *   --auth-token NAME:PERMS:TOKEN   bearer-token grant (repeatable; PERMS comma-separated or *)
 *   --trust-proxy-auth       trust X-Typeflux-Actor / X-Typeflux-Permissions from a reverse proxy
 *   --langfuse               inject the fetch-based langfuse reader transport (#573) so the
 *                            connections probe + prompt-status drift go LIVE (creds from
 *                            LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST env)
 *
 * There is deliberately NO `--github` flag (#727 F3): the github-provenance surface only reaches its
 * reader when a git source is recorded, and this edition's registry serves LOCAL checkouts only
 * (`repoSource` is structurally null) — so a `--github` flag would be a pure NO-OP (an operator
 * setting the token would get silent nothing). The `githubFor` seam stays injection-only (tests)
 * until a Git-source slice adds BOTH the transport and the registry source deliberately.
 *
 * Auth is OPEN by default (local server trusts its caller). --auth-token and --trust-proxy-auth
 * are mutually exclusive, matching the Python CLI.
 */

import { fetchLangfuseTransport } from "../langfuse-transport.js";
import { buildAuthorizer } from "./auth.js";
import { CONFORMANCE_SCHEMAS } from "./conformance-schemas.js";
import { loadProjectRegistry } from "./registry.js";
import { serve } from "./server.js";

interface ParsedArgs {
  registry: string | undefined;
  port: number | undefined;
  host: string | undefined;
  conformanceSchemas: boolean;
  authTokens: string[];
  trustProxyAuth: boolean;
  langfuse: boolean;
}

function parseArgs(rawArgv: readonly string[]): ParsedArgs {
  // The optional leading `serve` subcommand (#807): the bin's canonical spelling mirrors
  // the Python CLI (`typeflux-controlplane serve ...`); bare flags keep working for the
  // pre-bin `node dist/http/serve.js` invocation. Any OTHER subcommand is an error below.
  const argv = rawArgv[0] === "serve" ? rawArgv.slice(1) : rawArgv;
  const parsed: ParsedArgs = {
    registry: undefined,
    port: undefined,
    host: undefined,
    conformanceSchemas: false,
    authTokens: [],
    trustProxyAuth: false,
    langfuse: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]!;
    switch (arg) {
      case "--registry":
        parsed.registry = argv[(index += 1)];
        break;
      case "--port":
        parsed.port = Number(argv[(index += 1)]);
        break;
      case "--host":
        parsed.host = argv[(index += 1)];
        break;
      case "--conformance-schemas":
        parsed.conformanceSchemas = true;
        break;
      case "--auth-token": {
        const spec = argv[(index += 1)];
        if (spec === undefined) throw new Error("--auth-token requires a NAME:PERMS:TOKEN value");
        parsed.authTokens.push(spec);
        break;
      }
      case "--trust-proxy-auth":
        parsed.trustProxyAuth = true;
        break;
      case "--langfuse":
        parsed.langfuse = true;
        break;
      default:
        throw new Error(`unknown argument: ${arg}`);
    }
  }
  return parsed;
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));
  if (args.registry === undefined) {
    throw new Error(
      "usage: typeflux-controlplane serve --registry <typeflux.projects.yaml> --port <n> [--conformance-schemas]",
    );
  }
  if (args.port === undefined || !Number.isInteger(args.port) || args.port < 0) {
    throw new Error("usage: --port <n> is required and must be a non-negative integer");
  }

  const registry = loadProjectRegistry(args.registry);
  // The injected-schemas seam (#620): a real host supplies its projects' activity IO schemas; the
  // conformance harness uses the built-in fixture schemas via the flag. No flag ⇒ no schemas, and a
  // bundle/catalog request 422s on its first referenced ref (honest — the CP holds no activity code).
  const schemas = args.conformanceSchemas ? CONFORMANCE_SCHEMAS : undefined;
  // null ⇒ the open default; a bad token spec / mode combination fails at startup (exit 2).
  const authorizer = buildAuthorizer(args.authTokens, { trustProxy: args.trustProxyAuth });
  // --langfuse lights up the live connections probe + prompt-status drift (#573). One transport for
  // every project (creds/host from env); absent ⇒ both tiers degrade honestly.
  const langfuse = args.langfuse ? fetchLangfuseTransport() : undefined;
  // No github wiring here (#727 F3): `githubFor` is injection-only (tests) — a CLI flag would be a
  // NO-OP on this local-checkout edition (the registry records no git source), so it is omitted.

  const started = await serve({
    registry,
    schemasFor: () => schemas,
    ...(langfuse !== undefined ? { langfuseFor: () => langfuse } : {}),
    ...(authorizer !== null ? { authorizer } : {}),
    port: args.port,
    ...(args.host !== undefined ? { host: args.host } : {}),
  });
  // A single readiness line on stdout; the conformance runner polls /api/v1/meta, not this.
  process.stdout.write(`typeflux control plane listening on port ${started.port}\n`);

  const shutdown = (): void => {
    void started.close().then(() => process.exit(0));
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

main().catch((error: unknown) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exit(2);
});
