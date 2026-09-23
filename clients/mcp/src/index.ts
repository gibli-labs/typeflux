/**
 * `typeflux-mcp` bin entry (#326). Starts the Typeflux MCP server on the configured transport:
 *
 *   - stdio (default) — one local editor session (config.ts: no TYPEFLUX_MCP_TRANSPORT).
 *   - Streamable HTTP (design §8.2) — a shared/hosted server for a team, bearer-authed with
 *     per-session isolation (TYPEFLUX_MCP_TRANSPORT=http; a token is MANDATORY, enforced in config).
 *
 * Mode (attach | managed-local) is orthogonal, chosen from the environment as before. Nothing here
 * prints a secret; diagnostics go to stderr so they never corrupt the stdio JSON-RPC stream.
 */

import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";

import { loadConfig } from "./config.js";
import { startHttpTransport } from "./http-transport.js";
import { createTypefluxMcpServer, SERVER_ID, SERVER_VERSION } from "./server.js";

async function runStdio(config: ReturnType<typeof loadConfig>): Promise<void> {
  const { server, prepare, dispose } = createTypefluxMcpServer(config);
  const shutdown = (): void => {
    void dispose().finally(() => process.exit(0));
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);

  const transport = new StdioServerTransport();
  await server.connect(transport);
  // Resolve the backend and apply capability gating now that roots are available (§9 degrade).
  await prepare();
  process.stderr.write(
    `${SERVER_ID}-mcp ${SERVER_VERSION} listening on stdio (mode: ${config.mode})\n`,
  );
}

async function runHttp(config: ReturnType<typeof loadConfig>): Promise<void> {
  // A fresh, ISOLATED server (its own backend) per session (design §8.2 per-session isolation).
  const handle = await startHttpTransport(config.http!, () => createTypefluxMcpServer(config));
  const shutdown = (): void => {
    void handle.close().finally(() => process.exit(0));
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
  process.stderr.write(
    `${SERVER_ID}-mcp ${SERVER_VERSION} listening on http://${handle.host}:${handle.port}/mcp ` +
      `(mode: ${config.mode}; bearer auth required, per-session isolation)\n`,
  );
}

async function main(): Promise<void> {
  const config = loadConfig();
  if (config.transport === "http") {
    await runHttp(config);
  } else {
    await runStdio(config);
  }
}

main().catch((error: unknown) => {
  process.stderr.write(`typeflux-mcp failed to start: ${error instanceof Error ? error.stack : String(error)}\n`);
  process.exit(1);
});
