/**
 * Attach-mode smoke test for the PACKED artifact (#326 items 1+2). Spawns the built, self-contained
 * `dist/index.js` as a real subprocess in ATTACH mode (TYPEFLUX_CP_URL) against a stub control
 * plane, over stdio, and drives an MCP initialize + tools/list + a real tool call.
 *
 * What it proves:
 *  - item 1: the bundle installs/loads with no unresolvable file:/workspace deps (it runs at all).
 *  - item 2: attach mode NEVER loads the in-process serve machinery (the dynamic import of
 *    @typeflux/temporal-controlplane in managed-local.ts is only reached in managed-local mode) —
 *    the process starts and answers tools/list without ever synthesizing a registry or binding a
 *    control-plane port.
 */

import { createServer } from "node:http";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const DIST = resolve(HERE, "../dist/index.js");

const META = {
  api_version: "1",
  bundle_version: "1",
  catalog_version: "1",
  manifest_path: "/stub/typeflux.project.yaml",
  project: "stub",
  runtime: "typescript",
  capabilities: {
    can_start: false,
    can_review: false,
    can_cancel: false,
    can_refresh_project: false,
    can_resolve: true,
  },
};
const VALIDATE = { project_name: "stub", manifest_path: "/stub", ok: true, issues: [], workflows: [], resolved_workflows: [] };

function fail(message) {
  process.stderr.write(`smoke:attach FAIL — ${message}\n`);
  process.exit(1);
}

async function main() {
  // Stub control plane: answers /meta and /validate; everything else 404.
  const stub = createServer((req, res) => {
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    const send = (status, body) => {
      const payload = JSON.stringify(body);
      res.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(payload) });
      res.end(payload);
    };
    if (url.pathname === "/api/v1/meta") return send(200, META);
    if (url.pathname === "/api/v1/validate") return send(200, VALIDATE);
    return send(404, { error: "NotFound", message: url.pathname });
  });
  await new Promise((r) => stub.listen(0, "127.0.0.1", r));
  const port = stub.address().port;
  const cpUrl = `http://127.0.0.1:${port}`;

  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [DIST],
    // A MINIMAL env: only what attach mode needs. No monorepo paths, no NODE_PATH — proving the
    // packed bundle is self-contained.
    env: { PATH: process.env.PATH ?? "", TYPEFLUX_CP_URL: cpUrl, TYPEFLUX_CP_TOKEN: "smoke-token" },
    stderr: "pipe",
  });

  const client = new Client({ name: "smoke", version: "0" });
  let stderrBuf = "";
  transport.stderr?.on("data", (d) => (stderrBuf += d.toString()));

  try {
    await client.connect(transport);

    const { tools } = await client.listTools();
    const names = tools.map((t) => t.name);
    if (!names.includes("validate_project") || !names.includes("get_bundle")) {
      fail(`tools/list missing expected read tools; got: ${names.join(", ")}`);
    }

    const result = await client.callTool({ name: "validate_project", arguments: { environment_id: "local" } });
    if (result.isError) fail(`validate_project errored: ${JSON.stringify(result.content)}`);
    const report = result.structuredContent?.report;
    if (!report || report.project_name !== "stub") fail(`unexpected validate_project result: ${JSON.stringify(result.structuredContent)}`);

    // If the in-process serve had been loaded, its readiness line would appear on stderr; assert it
    // did NOT (attach mode never touches managed-local).
    if (/control plane listening on port/.test(stderrBuf)) {
      fail("attach mode unexpectedly started the in-process control plane");
    }

    process.stderr.write(`smoke:attach OK — ${names.length} tools listed; validate_project returned a real report (attach mode, self-contained bundle)\n`);
  } finally {
    await client.close().catch(() => undefined);
    await new Promise((r) => stub.close(r));
  }
}

main().catch((error) => {
  process.stderr.write(`smoke:attach threw: ${error?.stack ?? error}\n`);
  process.exit(1);
});
