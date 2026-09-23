/** The auth boundary (#620 slice 3): Python `controlplane/auth.py` parity. */

import { describe, expect, it } from "vitest";

import {
  Actor,
  ALL_PERMISSIONS,
  buildAuthorizer,
  OpenAuthorizer,
  parsePermissions,
  parseTokenSpec,
  ProjectControlPlaneError,
  ProxyHeaderAuthorizer,
  requirePermission,
  TokenAuthorizer,
} from "../src/index.js";

describe("parsePermissions", () => {
  it("parses comma/space separated names, case-insensitively", () => {
    expect(parsePermissions("inspect, START review")).toEqual(new Set(["inspect", "start", "review"]));
  });

  it("* and all expand to every permission", () => {
    expect(parsePermissions("*")).toEqual(new Set(ALL_PERMISSIONS));
    expect(parsePermissions("inspect all")).toEqual(new Set(ALL_PERMISSIONS));
  });

  it("ignores unknown names when tolerant (a proxy may forward unmodeled roles)", () => {
    expect(parsePermissions("inspect banana")).toEqual(new Set(["inspect"]));
  });

  it("throws on unknown names when strict (token config is fail-closed)", () => {
    expect(() => parsePermissions("inspect banana", { strict: true })).toThrow(
      /unknown permission: 'banana'/,
    );
  });

  it("empty input grants nothing", () => {
    expect(parsePermissions("").size).toBe(0);
  });
});

describe("parseTokenSpec", () => {
  it("parses NAME:PERMS:TOKEN; the token is the remainder and may contain ':'", () => {
    const { token, grant } = parseTokenSpec("ops:start,cancel:v1:secret:with:colons");
    expect(token).toBe("v1:secret:with:colons");
    expect(grant.name).toBe("ops");
    expect(grant.permissions).toEqual(new Set(["start", "cancel"]));
  });

  it("fails closed on a malformed spec, empty fields, or an empty grant set", () => {
    expect(() => parseTokenSpec("just-a-token")).toThrow(/NAME:PERMS:TOKEN/);
    expect(() => parseTokenSpec(":inspect:tok")).toThrow(/NAME:PERMS:TOKEN/);
    expect(() => parseTokenSpec("name:inspect:")).toThrow(/NAME:PERMS:TOKEN/);
    expect(() => parseTokenSpec("name::tok")).toThrow(/grants no permissions/);
    expect(() => parseTokenSpec("name:banana:tok")).toThrow(/unknown permission/);
  });
});

describe("buildAuthorizer", () => {
  it("returns null for the open default (no specs, no proxy)", () => {
    expect(buildAuthorizer()).toBeNull();
    expect(buildAuthorizer(["  "])).toBeNull();
  });

  it("proxy and token modes are mutually exclusive", () => {
    expect(() => buildAuthorizer(["r:inspect:tok"], { trustProxy: true })).toThrow(
      /cannot be combined/,
    );
  });

  it("rejects a duplicate token value across specs (privilege-escalation footgun)", () => {
    expect(() => buildAuthorizer(["a:inspect:tok", "b:*:tok"])).toThrow(
      /token for 'b' is already configured for 'a'/,
    );
  });
});

describe("TokenAuthorizer", () => {
  const authorizer = buildAuthorizer(["reader:inspect:read-tok", "operator:*:op-tok"]) as TokenAuthorizer;

  it("matches by exact bearer value; scheme is case-insensitive", () => {
    expect(authorizer.authorize({ authorization: "Bearer read-tok" }).id).toBe("reader");
    expect(authorizer.authorize({ authorization: "bearer op-tok" }).permissions).toEqual(
      new Set(ALL_PERMISSIONS),
    );
  });

  it("missing, empty, non-bearer, or unknown tokens yield a permissionless actor (fail closed)", () => {
    for (const headers of [
      {},
      { authorization: "" },
      { authorization: "Bearer " },
      { authorization: "Basic read-tok" },
      { authorization: "Bearer not-configured" },
    ]) {
      const actor = authorizer.authorize(headers);
      expect(actor.id).toBeNull();
      expect(actor.permissions.size).toBe(0);
    }
  });
});

describe("ProxyHeaderAuthorizer", () => {
  const authorizer = new ProxyHeaderAuthorizer();

  it("reads actor + permissions from the trusted headers", () => {
    const actor = authorizer.authorize({
      "x-typeflux-actor": "alice",
      "x-typeflux-permissions": "inspect,review",
    });
    expect(actor.id).toBe("alice");
    expect(actor.permissions).toEqual(new Set(["inspect", "review"]));
  });

  it("empty-string headers mean absent (Python `or None` truthiness)", () => {
    const actor = authorizer.authorize({ "x-typeflux-actor": "", "x-typeflux-permissions": "" });
    expect(actor.id).toBeNull();
    expect(actor.permissions.size).toBe(0);
  });

  it("fails closed on a DUPLICATED actor header — identity null, never first-wins/joined (#577)", () => {
    // A string[] value is the adapter's headersDistinct read of >1 occurrence: a proxy that
    // appends rather than replaces let a client-smuggled copy through, so nothing is trusted.
    const actor = authorizer.authorize({
      "x-typeflux-actor": ["mallory", "alice"],
      "x-typeflux-permissions": "inspect",
    });
    expect(actor.id).toBeNull();
    // Grants fail independently — the single permissions header still reads.
    expect(actor.permissions).toEqual(new Set(["inspect"]));
  });

  it("fails closed on a DUPLICATED permissions header — no grants, never a union (#577)", () => {
    // Pre-hardening, the joined "inspect, *" would have parsed to EVERY permission —
    // guaranteed escalation under the same proxy misconfig. Duplicates read as absent.
    const actor = authorizer.authorize({
      "x-typeflux-actor": "alice",
      "x-typeflux-permissions": ["inspect", "*"],
    });
    expect(actor.permissions.size).toBe(0);
    expect(actor.id).toBe("alice");
  });

  it("trims the actor value; whitespace-only is absent, matching Python (#577)", () => {
    const blank = authorizer.authorize({ "x-typeflux-actor": "   ", "x-typeflux-permissions": "inspect" });
    expect(blank.id).toBeNull();
    const padded = authorizer.authorize({ "x-typeflux-actor": "  alice  ", "x-typeflux-permissions": "inspect" });
    expect(padded.id).toBe("alice");
  });
});

describe("Actor", () => {
  it("any operate grant implies inspect; an empty grant set implies nothing", () => {
    expect(new Actor("s", new Set(["start"])).can("inspect")).toBe(true);
    expect(new Actor(null, new Set()).can("inspect")).toBe(false);
    expect(new Actor("r", new Set(["inspect"])).can("start")).toBe(false);
  });

  it("canOperate is true only for state-changing grants", () => {
    expect(new Actor("r", new Set(["inspect"])).canOperate()).toBe(false);
    expect(new Actor("c", new Set(["cancel"])).canOperate()).toBe(true);
  });
});

describe("requirePermission", () => {
  it("403 Forbidden with Python's exact detail; token validity is never disclosed", () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      requirePermission(new Actor(null, new Set()), "inspect");
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(403);
    expect(caught?.errorName).toBe("Forbidden");
    expect(caught?.message).toBe("operation requires the 'inspect' permission");
  });

  it("the open authorizer grants everything", () => {
    const actor = new OpenAuthorizer().authorize();
    expect(requirePermission(actor, "project.refresh")).toBe(actor);
  });
});
