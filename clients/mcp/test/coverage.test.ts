/**
 * The Phase-0 conformance test (#326; design §10). Parses the control-plane contract and asserts
 * the coverage ledger and the contract agree EXACTLY: every control-plane operation is either a
 * Phase-0 surface (wrapped) or a conscious exclusion, and no operation is missing or invented. So
 * the MCP surface can never silently fall behind the contract, and no operate-tier write can slip
 * into the read tier.
 */

import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { COVERAGE_LEDGER, DEFERRED_NON_CONTRACT_TOOLS, wrappedToolNames } from "../src/coverage.js";
import { operateToolNames } from "../src/operate.js";
import { liveResourceLedgerUris } from "../src/resources.js";
import { readToolNames } from "../src/tools.js";

/** A registered live resource with no 1:1 contract op — a documented projection, not a drift. */
const PROJECTIONS = new Set(["typeflux://{project}/workflows/{workflow_id}/topology"]);

const CONTRACT_PATH = fileURLToPath(
  new URL("../../../contracts/controlplane/openapi.v1.json", import.meta.url),
);

interface Op {
  method: "GET" | "POST";
  path: string;
}

/** Load the contract's operations, collapsing the /projects/{project} dual mount to its default form. */
function contractOperations(): Op[] {
  const contract = JSON.parse(readFileSync(CONTRACT_PATH, "utf8")) as {
    paths: Record<string, Record<string, unknown>>;
  };
  const allPaths = new Set(Object.keys(contract.paths));

  const canonical = (path: string): string => {
    const match = path.match(/^\/api\/v1\/projects\/\{project\}\/(.+)$/);
    if (match) {
      const unprefixed = `/api/v1/${match[1]}`;
      // Collapse only when the unprefixed dual actually exists (refresh has no unprefixed form).
      if (allPaths.has(unprefixed)) return unprefixed;
    }
    return path;
  };

  const ops = new Map<string, Op>();
  for (const [path, methods] of Object.entries(contract.paths)) {
    for (const method of Object.keys(methods)) {
      const upper = method.toUpperCase();
      if (upper !== "GET" && upper !== "POST") continue;
      const op: Op = { method: upper, path: canonical(path) };
      ops.set(`${op.method} ${op.path}`, op);
    }
  }
  return [...ops.values()];
}

const key = (op: Op): string => `${op.method} ${op.path}`;

describe("coverage ledger vs the control-plane contract", () => {
  const contractOps = contractOperations();
  const ledgerKeys = new Set(COVERAGE_LEDGER.map((entry) => key(entry)));
  const contractKeys = new Set(contractOps.map((op) => key(op)));

  it("the contract exposes operations to account for", () => {
    expect(contractOps.length).toBeGreaterThan(15);
  });

  it("every contract operation is in the ledger (nothing silently unwrapped)", () => {
    const missing = [...contractKeys].filter((k) => !ledgerKeys.has(k)).sort();
    expect(missing).toEqual([]);
  });

  it("the ledger invents no operation absent from the contract", () => {
    const extra = [...ledgerKeys].filter((k) => !contractKeys.has(k)).sort();
    expect(extra).toEqual([]);
  });

  it("every GET is WRAPPED by a tool or a resource", () => {
    const badGets = COVERAGE_LEDGER.filter(
      (entry) => entry.method === "GET" && entry.kind !== "wrapped",
    ).map(key);
    expect(badGets).toEqual([]);
    for (const entry of COVERAGE_LEDGER) {
      if (entry.method === "GET" && entry.kind === "wrapped") {
        expect(Boolean(entry.tool) || Boolean(entry.resource)).toBe(true);
      }
    }
  });

  it("every POST is WRAPPED by an operate tool or consciously EXCLUDED (no write slips in unaccounted)", () => {
    for (const entry of COVERAGE_LEDGER) {
      if (entry.method !== "POST") continue;
      if (entry.kind === "wrapped") {
        expect(entry.tool, `${key(entry)} must name the operate tool that wraps it`).toBeTruthy();
      } else {
        expect(entry.reason.length).toBeGreaterThan(0);
      }
    }
  });

  it("the five §6.3 operate POSTs are wrapped; only migrate stays excluded", () => {
    const wrappedPosts = COVERAGE_LEDGER.filter((e) => e.method === "POST" && e.kind === "wrapped").map(key);
    const excludedPosts = COVERAGE_LEDGER.filter((e) => e.method === "POST" && e.kind === "excluded").map(key);
    expect(wrappedPosts.sort()).toEqual(
      [
        "POST /api/v1/projects/{project}/refresh",
        "POST /api/v1/workflows/{workflow_id}/cancel",
        "POST /api/v1/workflows/{workflow_id}/repin",
        "POST /api/v1/workflows/{workflow_id}/review",
        "POST /api/v1/workflows/{workflow_id}/start",
      ].sort(),
    );
    expect(excludedPosts).toEqual(["POST /api/v1/workflows/{workflow_id}/migrate"]);
  });

  it("the registered read + operate tools are exactly the ledger's wrapped tool entries", () => {
    const registered = [...readToolNames(), ...operateToolNames()].sort();
    expect(registered).toEqual([...wrappedToolNames()].sort());
  });

  it("every ledger resource URI is actually registered (no addressing drift)", () => {
    const registered = new Set(liveResourceLedgerUris());
    const ledgerResources = COVERAGE_LEDGER.flatMap((entry) =>
      entry.kind === "wrapped" && entry.resource ? [entry.resource] : [],
    );
    const unregistered = ledgerResources.filter((uri) => !registered.has(uri)).sort();
    expect(unregistered).toEqual([]);
  });

  it("every registered live resource is a ledger op or a documented projection (no orphans)", () => {
    const ledgerResources = new Set(
      COVERAGE_LEDGER.flatMap((entry) =>
        entry.kind === "wrapped" && entry.resource ? [entry.resource] : [],
      ),
    );
    const orphans = liveResourceLedgerUris().filter(
      (uri) => !ledgerResources.has(uri) && !PROJECTIONS.has(uri),
    );
    expect(orphans).toEqual([]);
  });

  it("the full §6.2 trace surface stays deferred (no trace route in the contract)", () => {
    // Re-verified in Phase 2: the contract has no trace/observability path at all.
    const tracePaths = contractOps.filter((op) => /trace|observ|inspect|export|diff|search/i.test(op.path));
    expect(tracePaths).toEqual([]);
    // All five design §6.2 observability tools are recorded as deferred (blocked on a CP surface).
    expect([...DEFERRED_NON_CONTRACT_TOOLS].sort()).toEqual(
      ["trace_diff", "trace_export", "trace_inspect", "trace_list", "trace_search"].sort(),
    );
    // ... and none is registered as a read tool or an operate tool.
    const registeredTools = new Set([...readToolNames(), ...operateToolNames()]);
    for (const deferred of DEFERRED_NON_CONTRACT_TOOLS) {
      expect(registeredTools.has(deferred)).toBe(false);
    }
  });

  it("the contract file exists (guards a silent path drift)", () => {
    expect(existsSync(CONTRACT_PATH)).toBe(true);
  });
});
