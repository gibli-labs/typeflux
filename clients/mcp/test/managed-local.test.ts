/**
 * Managed-local backend unit tests (#326): registry synthesis, the readiness poll, and manifest
 * discovery over roots — all with the real ProjectRegistry but a STUBBED serve/probe so no socket
 * is opened. The real in-process serve is exercised end-to-end in e2e.test.ts.
 */

import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";

import { startManagedLocal } from "../src/managed-local.js";
import { discoverManifest } from "../src/discovery.js";

const FIXTURE_DIR = fileURLToPath(
  new URL("../../../contracts/controlplane/conformance/project/typescript", import.meta.url),
);
const FIXTURE_MANIFEST = resolve(FIXTURE_DIR, "typeflux.project.yaml");

/** A fake ControlPlaneServer that records nothing but a close() call. */
function fakeServe(port: number, onClose: () => void) {
  return vi.fn(async (options: { registry: { entries: { manifestPath: string }[] } }) => {
    // Capture the synthesized registry for assertions via the mock's calls.
    return {
      server: {} as never,
      port,
      close: async () => onClose(),
    };
  });
}

describe("startManagedLocal", () => {
  it("synthesizes a one-project registry from the manifest and binds the returned port", async () => {
    let closed = false;
    const serveFn = fakeServe(54321, () => {
      closed = true;
    });
    const handle = await startManagedLocal({
      manifestPath: FIXTURE_MANIFEST,
      runtime: "typescript",
      projectId: "local",
      serveFn: serveFn as never,
      probe: async () => true,
    });

    expect(handle.baseUrl).toBe("http://127.0.0.1:54321");
    // The serve() call received a registry-of-one pointing at the manifest.
    const passed = serveFn.mock.calls[0]![0] as { registry: { entries: { manifestPath: string; id: string }[]; defaultId: string } };
    expect(passed.registry.entries).toHaveLength(1);
    expect(passed.registry.entries[0]!.manifestPath).toBe(resolve(FIXTURE_MANIFEST));
    expect(passed.registry.defaultId).toBe("local");

    await handle.dispose();
    expect(closed).toBe(true);
  });

  it("polls readiness until the probe passes", async () => {
    let attempts = 0;
    const handle = await startManagedLocal({
      manifestPath: FIXTURE_MANIFEST,
      runtime: "typescript",
      serveFn: fakeServe(40000, () => undefined) as never,
      probe: async () => {
        attempts += 1;
        return attempts >= 3;
      },
      readinessTimeoutMs: 2000,
    });
    expect(attempts).toBeGreaterThanOrEqual(3);
    await handle.dispose();
  });

  it("fails closed (and closes the server) when readiness never passes", async () => {
    let closed = false;
    await expect(
      startManagedLocal({
        manifestPath: FIXTURE_MANIFEST,
        runtime: "typescript",
        serveFn: fakeServe(40001, () => {
          closed = true;
        }) as never,
        probe: async () => false,
        readinessTimeoutMs: 150,
      }),
    ).rejects.toThrow(/did not become ready/);
    expect(closed).toBe(true);
  });
});

describe("discoverManifest (roots)", () => {
  it("finds typeflux.project.yaml at a root directory", () => {
    const found = discoverManifest([`file://${FIXTURE_DIR}`]);
    expect(found?.manifestPath).toBe(FIXTURE_MANIFEST);
  });

  it("finds a manifest one level down", () => {
    const parent = fileURLToPath(new URL("../../../contracts/controlplane/conformance/project", import.meta.url));
    const found = discoverManifest([`file://${parent}`]);
    // typescript/ or python/ subdir holds a manifest.
    expect(found?.manifestPath).toMatch(/typeflux\.project\.yaml$/);
  });

  it("returns undefined when no manifest is present and ignores non-file roots", () => {
    expect(discoverManifest(["https://example.com/repo"])).toBeUndefined();
    const empty = fileURLToPath(new URL(".", import.meta.url)); // the test dir — no manifest
    expect(discoverManifest([`file://${empty}`])).toBeUndefined();
  });
});
