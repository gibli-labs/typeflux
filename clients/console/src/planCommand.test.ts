import { describe, expect, it } from "vitest";

import { isDigestPinned, planCommand } from "./planCommand";

describe("isDigestPinned", () => {
  it("accepts a digest-pinned image", () => {
    expect(isDigestPinned(`ghcr.io/org/worker@sha256:${"a".repeat(64)}`)).toBe(true);
  });
  it("rejects a mutable tag", () => {
    expect(isDigestPinned("ghcr.io/org/worker:latest")).toBe(false);
    expect(isDigestPinned("ghcr.io/org/worker")).toBe(false);
  });
  it("rejects a malformed digest", () => {
    expect(isDigestPinned("ghcr.io/org/worker@sha256:abc")).toBe(false);
  });
  it("accepts an uppercase-hex digest (case-insensitive)", () => {
    expect(isDigestPinned(`ghcr.io/org/worker@sha256:${"A".repeat(64)}`)).toBe(true);
  });
});

describe("planCommand", () => {
  const base = {
    manifest: "/repo/typeflux.project.yaml",
    workflow: "claims",
    environment: "local",
    policies: [],
    image: `ghcr.io/org/worker@sha256:${"a".repeat(64)}`,
  };

  it("assembles a complete deploy --plan-out command", () => {
    const cmd = planCommand(base);
    expect(cmd).toContain("--environment local");
    expect(cmd).toContain("--workflow claims");
    expect(cmd).toContain("--plan-out deployments/");
    expect(cmd).toContain("'/repo/typeflux.project.yaml'");
  });

  it("shell-quotes the manifest and image so a pasted command is safe", () => {
    const cmd = planCommand({
      ...base,
      manifest: "/My Repo/typeflux.project.yaml",
      image: "ghcr.io/org/worker; rm -rf /",
    });
    // Spaces and metacharacters stay inside one quoted argument.
    expect(cmd).toContain("'/My Repo/typeflux.project.yaml'");
    expect(cmd).toContain("--image 'ghcr.io/org/worker; rm -rf /'");
  });

  it("escapes an embedded single quote in a quoted value", () => {
    const cmd = planCommand({ ...base, image: "ghcr.io/o'rg/worker" });
    expect(cmd).toContain("--image 'ghcr.io/o'\\''rg/worker'");
  });

  it("emits one --policy per selected policy", () => {
    const cmd = planCommand({ ...base, policies: ["base", "regulated"] });
    expect(cmd).toContain("--policy base");
    expect(cmd).toContain("--policy regulated");
  });

  it("falls back to a quoted placeholder when the image is empty", () => {
    expect(planCommand({ ...base, image: "  " })).toContain("--image '<digest-pinned-image>'");
  });
});
