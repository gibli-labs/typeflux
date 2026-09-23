import { cpSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { request as httpRequest } from "node:http";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  buildAuthorizer,
  buildRoutes,
  type EnforcementTraceRecord,
  type GithubProvenanceTransport,
  type GithubReadResult,
  type LangfuseControlPlaneTransport,
  loadProjectRegistry,
  ProjectControlPlaneError,
  type RegistryContext,
  serve,
  type RouteResponse,
} from "../src/index.js";
import { CONFORMANCE_SCHEMAS } from "../src/http/conformance-schemas.js";

const HERE = dirname(fileURLToPath(import.meta.url));
// The canonical conformance fixture project's TS binding — the same registry serve-typescript.sh runs.
const REGISTRY_PATH = resolve(
  HERE,
  "../../../../contracts/controlplane/conformance/project/typescript/typeflux.projects.yaml",
);

const tempDirs: string[] = [];

const context = () => ({
  registry: loadProjectRegistry(REGISTRY_PATH),
  schemasFor: () => CONFORMANCE_SCHEMAS,
});

/** Run the handler for a path (matching by method + segment count + literal/param) against a fresh route table. */
async function request(
  method: "GET" | "POST",
  path: string,
  query: Record<string, string | string[]> = {},
  headers: Record<string, string> = {},
  routeContext: RegistryContext = context(),
  body: unknown = undefined,
): Promise<RouteResponse> {
  const routes = buildRoutes(routeContext);
  const segments = path.split("/").filter((s) => s.length > 0);
  for (const route of routes) {
    if (route.method !== method || route.segments.length !== segments.length) continue;
    const params: Record<string, string> = {};
    let matched = true;
    for (let i = 0; i < route.segments.length; i += 1) {
      const pattern = route.segments[i]!;
      const actual = segments[i]!;
      if (pattern.startsWith("{") && pattern.endsWith("}")) params[pattern.slice(1, -1)] = actual;
      else if (pattern !== actual) {
        matched = false;
        break;
      }
    }
    if (matched) return await route.handler({ params, query, body: () => body, headers });
  }
  throw new Error(`no route matched ${method} ${path}`);
}

function get(path: string, query: Record<string, string | string[]> = {}): Promise<RouteResponse> {
  return request("GET", path, query);
}

describe("registry loader", () => {
  it("loads the conformance TS registry: default is the TS-runtime project, plus a foreign python entry", async () => {
    const registry = loadProjectRegistry(REGISTRY_PATH);
    expect(registry.defaultId).toBe("conformance");
    const summaries = registry.summaries();
    const conformance = summaries.find((s) => s.id === "conformance")!;
    expect(conformance.runtime).toBe("typescript");
    expect(conformance.resolvable).toBe(true);
    expect(conformance.default).toBe(true);
    expect(conformance.source).toBe("local");
    expect(conformance.repo_url).toBeNull();
    const foreign = summaries.find((s) => s.id === "py-project")!;
    expect(foreign.runtime).toBe("python");
    expect(foreign.resolvable).toBe(false);
  });

  it("fails closed on an empty/unknown runtime (never silently the default)", async () => {
    const dir = mkdtempSync(join(tmpdir(), "tf-registry-"));
    tempDirs.push(dir);
    const registryFile = join(dir, "typeflux.projects.yaml");
    writeFileSync(
      registryFile,
      "version: '1'\nprojects:\n  - id: x\n    manifest: m.yaml\n    runtime: ''\n",
      "utf8",
    );
    let caught: ProjectControlPlaneError | undefined;
    try {
      loadProjectRegistry(registryFile);
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(422);
    expect(caught?.message).toMatch(/runtime.*must be 'python' or 'typescript'/);
  });

  it("fails closed on a present-but-null runtime key (only an ABSENT key defaults to python)", async () => {
    const dir = mkdtempSync(join(tmpdir(), "tf-registry-"));
    tempDirs.push(dir);
    const registryFile = join(dir, "typeflux.projects.yaml");
    // `runtime:` with no value parses as YAML null — Python's `item.get("runtime", "python")`
    // returns None for it and fails closed; `?? "python"` would silently default it.
    writeFileSync(
      registryFile,
      "version: '1'\nprojects:\n  - id: x\n    manifest: m.yaml\n    runtime:\n",
      "utf8",
    );
    let caught: ProjectControlPlaneError | undefined;
    try {
      loadProjectRegistry(registryFile);
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(422);
    expect(caught?.message).toMatch(/runtime.*must be 'python' or 'typescript'/);
  });
});

describe("route table — read tier over the conformance TS project", () => {
  it("meta reports runtime typescript with all capabilities true (operate tier served, #563)", async () => {
    const { status, body } = await get("/api/v1/meta");
    expect(status).toBe(200);
    // The default project is TS (resolvable + operable) and the open authorizer grants everything,
    // so every capability is TRUE now the operate tier is real — start/review/cancel/refresh routes
    // exist and work. can_resolve tracks the read tier (validate/bundle/catalog).
    expect(body).toMatchObject({
      project: "conformance-fixture",
      runtime: "typescript",
      // Open authorizer: no trusted proxy identity, so caller_identity is null (#577).
      caller_identity: null,
      capabilities: { can_resolve: true, can_start: true, can_review: true, can_cancel: true, can_refresh_project: true, enforcement_events: true, github_provenance: false },
    });
  });

  it("meta for the foreign python project reports runtime python; only project-refresh stays available", async () => {
    const { body } = await get("/api/v1/projects/py-project/meta");
    // A python-runtime project is neither resolvable nor operable on this TS-resolving server, so
    // start/review/cancel/resolve are false — a client is never steered into a route that would
    // 501. `can_refresh_project` is NOT operability-gated (Python `ApiCapabilities.for_actor`):
    // refresh re-fetches source for ANY registered project, so it stays true under the open actor.
    expect(body).toMatchObject({
      runtime: "python",
      capabilities: { can_resolve: false, can_start: false, can_review: false, can_cancel: false, can_refresh_project: true, enforcement_events: false, github_provenance: false },
    });
  });

  it("meta.caller_identity echoes the principal ONLY under a trusted proxy authorizer (#577)", async () => {
    // Under --trust-proxy-auth the X-Typeflux-Actor header is a vouched-for identity — echo it.
    const proxyContext = (): RegistryContext => ({
      registry: loadProjectRegistry(REGISTRY_PATH),
      schemasFor: () => CONFORMANCE_SCHEMAS,
      authorizer: buildAuthorizer([], { trustProxy: true })!,
    });
    const present = await request("GET", "/api/v1/meta", {}, { "x-typeflux-actor": "alice", "x-typeflux-permissions": "inspect" }, proxyContext());
    expect(present.body).toMatchObject({ caller_identity: "alice" });

    // Empty-string header maps to null in BOTH editions (the truthiness porting trap): an empty
    // actor header is no identity, not the literal "".
    const empty = await request("GET", "/api/v1/meta", {}, { "x-typeflux-actor": "", "x-typeflux-permissions": "inspect" }, proxyContext());
    expect((empty.body as { caller_identity: string | null }).caller_identity).toBeNull();

    // Token mode: actor.id is a grant NAME (config, not an identity) and must NEVER be echoed.
    const tokenContext = (): RegistryContext => ({
      registry: loadProjectRegistry(REGISTRY_PATH),
      schemasFor: () => CONFORMANCE_SCHEMAS,
      authorizer: buildAuthorizer(["operator:*:conformance-operator-token"])!,
    });
    const token = await request("GET", "/api/v1/meta", {}, { authorization: "Bearer conformance-operator-token" }, tokenContext());
    expect((token.body as { caller_identity: string | null }).caller_identity).toBeNull();
  });

  it("workflows lists the declared workflows in order", async () => {
    const { body } = await get("/api/v1/workflows") as { body: { workflows: Array<{ id: string }> } };
    expect(body.workflows.map((w) => w.id)).toEqual([
      "workflow",
      "broken",
      "composition",
      "child_assessment",
      "subworkflow",
    ]);
  });

  it("annotations serves the in-repo insight-ack ledger (pure-YAML, not resolution-bound) (#733)", async () => {
    const { status, body } = await get("/api/v1/annotations") as {
      status: number;
      body: { annotations: Array<Record<string, unknown>> };
    };
    expect(status).toBe(200);
    // The conformance project's `.typeflux/annotations.yaml` — an open ack with a tracking issue and
    // an already-expired ack (served WITH its expiry; rendering is the console's job). Byte-identical
    // to the Python edition's golden (the shared annotations.json fixture).
    expect(body.annotations).toEqual([
      {
        insight_id_pattern: "policy.drift.base",
        reason: "tracked upstream; not actionable until the provider ships the fix",
        tracked_in: "https://github.com/typeflux/conformance/issues/1",
      },
      {
        insight_id_pattern: "runtime.pin.*",
        reason: "accepted for the migration window; revisit after the cutover",
        expires: "2026-01-01",
      },
    ]);
  });

  it("annotations is served for a foreign-runtime (unresolvable) project — empty when absent (#733)", async () => {
    // Not resolution-bound: the python stub (no `.typeflux/annotations.yaml`) still answers, empty.
    const { status, body } = await get("/api/v1/projects/py-project/annotations") as {
      status: number;
      body: { annotations: unknown[] };
    };
    expect(status).toBe(200);
    expect(body.annotations).toEqual([]);
  });

  it("environment detail carries the used_by reverse index and variable names", async () => {
    const { body } = await get("/api/v1/environments/local") as {
      body: { id: string; used_by: string[]; variable_names: string[] };
    };
    expect(body.id).toBe("local");
    expect(body.used_by).toEqual(["workflow"]);
    expect(body.variable_names).toEqual(["TYPEFLUX_ENVIRONMENT"]);
  });

  it("a missing required query param is a 422 RequestValidationError with the exact message", async () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      await get("/api/v1/workflows/workflow/bundle");
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(422);
    expect(caught?.errorName).toBe("RequestValidationError");
    expect(caught?.message).toBe("1 validation error: query.environment_id: Field required");
  });

  it("an unknown workflow bundle is a 404 NotFound", async () => {
    await expect(get("/api/v1/workflows/nope/bundle", { environment_id: "local" })).rejects.toThrow(
      /unknown project workflow: nope/,
    );
  });

  it("a workflow selecting an undeclared profile is a 422 ProjectProfileError (discriminant parity)", async () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      await get("/api/v1/workflows/broken/bundle", { environment_id: "local" });
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(422);
    expect(caught?.errorName).toBe("ProjectProfileError");
    expect(caught?.message).toBe("unknown project provider profile: missing-profile");
  });

  it("a workflow selecting an unknown profile KIND is a 422 ProjectProfileError, not silently dropped", async () => {
    // Python's bundle path runs `validate_profile_selection` before resolving ids — a typo'd kind
    // (e.g. `providerr:`) must fail closed, never build a bundle that ignores the intended override.
    const dir = mkdtempSync(join(tmpdir(), "tf-kind-"));
    tempDirs.push(dir);
    const fixture = dirname(REGISTRY_PATH);
    cpSync(join(fixture, "workflow.yaml"), join(dir, "workflow.yaml"));
    mkdirSync(join(dir, "environments"));
    cpSync(join(fixture, "environments", "local.yaml"), join(dir, "environments", "local.yaml"));
    writeFileSync(
      join(dir, "typeflux.project.yaml"),
      "version: '1'\nname: kind-typo\nworkflows:\n  - id: wf\n    path: workflow.yaml\n" +
        "    profiles:\n      providerr: anthropic-prod\nenvironments:\n  local: environments/local.yaml\n",
      "utf8",
    );
    writeFileSync(
      join(dir, "typeflux.projects.yaml"),
      "version: '1'\nprojects:\n  - id: kind-typo\n    manifest: typeflux.project.yaml\n    runtime: typescript\n",
      "utf8",
    );
    const routes = buildRoutes({
      registry: loadProjectRegistry(join(dir, "typeflux.projects.yaml")),
      schemasFor: () => CONFORMANCE_SCHEMAS,
    });
    const bundle = routes.find(
      (route) => route.segments.join("/") === "api/v1/workflows/{workflow_id}/bundle",
    )!;
    let caught: ProjectControlPlaneError | undefined;
    try {
      bundle.handler({
        params: { workflow_id: "wf" },
        query: { environment_id: "local" },
        body: () => undefined,
        headers: {},
      });
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(422);
    expect(caught?.errorName).toBe("ProjectProfileError");
    expect(caught?.message).toBe(
      "workflow 'wf' profile selection selects unknown profile kind 'providerr'; valid kinds: provider, registry, runtime",
    );
  });

  it("connections renders the probe result and follows the shared guard order (#652)", async () => {
    const { status, body } = await request("GET", "/api/v1/workflows/workflow/connections", {
      environment_id: "local",
    });
    expect(status).toBe(200);
    expect(body).toMatchObject({
      workflow_id: "workflow",
      environment_id: "local",
      registry: { type: "inline", reachable: true },
      observability: { type: "none", reachable: true },
    });
    // Missing environment_id is the FastAPI-parity 422; a foreign runtime is 501.
    await expect(request("GET", "/api/v1/workflows/workflow/connections")).rejects.toThrow(
      /query\.environment_id: Field required/,
    );
    await expect(
      request("GET", "/api/v1/projects/py-project/workflows/flow/connections", { environment_id: "local" }),
    ).rejects.toThrow(/cannot resolve/);
    // An INVALID profile selection is the 422 ProjectProfileError, matching bundle (codex) —
    // never a 200 reporting the unprofiled backends.
    await expect(
      request("GET", "/api/v1/workflows/broken/connections", { environment_id: "local" }),
    ).rejects.toThrow(/unknown project provider profile: missing-profile/);
  });

  it("deployment_image yields deployment_preview; its absence yields deployment_preview_reference (#687 D687-5)", async () => {
    // No image: the bundle carries the copyable promote command, and NO preview.
    const plain = (await get("/api/v1/workflows/workflow/bundle", { environment_id: "local" }))
      .body as Record<string, unknown>;
    expect(plain["deployment_preview_reference"]).toMatch(/^typeflux-project deploy .* --workflow workflow --image <digest-pinned-image>$/);
    expect(plain).not.toHaveProperty("deployment_preview");

    // With an image: the secret-free preview replaces the reference. The conformance fixture
    // project fails its structural preflight (duplicate workflow name), so the ADVISORY preview
    // records that as `{error}` — never a 422 that drops the whole bundle.
    const previewed = (await get("/api/v1/workflows/workflow/bundle", {
      environment_id: "local",
      deployment_image: "registry.example/image@sha256:" + "a".repeat(64),
    }).then((r) => r.body)) as Record<string, unknown>;
    expect(previewed).toHaveProperty("deployment_preview");
    expect(previewed).not.toHaveProperty("deployment_preview_reference");
    const preview = previewed["deployment_preview"] as Record<string, unknown>;
    // Either a rendered worker set or an advisory error object — both are valid preview shapes.
    expect("workers" in preview || "error" in preview).toBe(true);
  });

  it("prompt-status: an undeclared registry profile is a 422 ProjectProfileError; a DECLARED one is HONORED (#568)", async () => {
    // A selected registry profile flips runtime.registry after composition, which is now REAL: a
    // VALID selection changes prompt-status's `registry_type` (the honored profiled registry); an
    // INVALID selection is the same 422 the bundle path emits — a config mistake never reads as a
    // server limitation (Bugbot). The workflow's base registry is `inline`; the profile is `langfuse`.
    const setup = (declare: boolean): ReturnType<typeof context> => {
      const dir = mkdtempSync(join(tmpdir(), "tf-regprof-"));
      tempDirs.push(dir);
      const fixture = dirname(REGISTRY_PATH);
      cpSync(join(fixture, "workflow.yaml"), join(dir, "workflow.yaml"));
      mkdirSync(join(dir, "environments"));
      cpSync(join(fixture, "environments", "local.yaml"), join(dir, "environments", "local.yaml"));
      if (declare) {
        mkdirSync(join(dir, "profiles", "registry"), { recursive: true });
        writeFileSync(
          join(dir, "profiles", "registry", "langfuse-prod.yaml"),
          "version: '1'\nname: langfuse-prod\nkind: registry\nruntime:\n  registry:\n    type: langfuse\n",
          "utf8",
        );
      }
      writeFileSync(
        join(dir, "typeflux.project.yaml"),
        "version: '1'\nname: reg-prof\nworkflows:\n  - id: wf\n    path: workflow.yaml\n" +
          "    profiles:\n      registry: langfuse-prod\nenvironments:\n  local: environments/local.yaml\n" +
          (declare ? "profiles:\n  registry:\n    langfuse-prod: profiles/registry/langfuse-prod.yaml\n" : ""),
        "utf8",
      );
      writeFileSync(
        join(dir, "typeflux.projects.yaml"),
        "version: '1'\nprojects:\n  - id: reg-prof\n    manifest: typeflux.project.yaml\n    runtime: typescript\n",
        "utf8",
      );
      return { registry: loadProjectRegistry(join(dir, "typeflux.projects.yaml")), schemasFor: () => CONFORMANCE_SCHEMAS };
    };

    // An UNDECLARED selection fails closed with the 422 ProjectProfileError the bundle path emits.
    let undeclared: ProjectControlPlaneError | undefined;
    try {
      await request("GET", "/api/v1/workflows/wf/prompt-status", { environment_id: "local" }, {}, setup(false));
    } catch (error) {
      undeclared = error as ProjectControlPlaneError;
    }
    expect(undeclared?.status).toBe(422);
    expect(undeclared?.errorName).toBe("ProjectProfileError");
    expect(undeclared?.message).toBe("unknown project registry profile: langfuse-prod");

    // A DECLARED selection is HONORED: composition merges runtime.registry.type: langfuse, so the
    // report's registry_type is the profiled `langfuse` (not the workflow's base `inline`).
    const { status, body } = await request(
      "GET",
      "/api/v1/workflows/wf/prompt-status",
      { environment_id: "local" },
      {},
      setup(true),
    );
    expect(status).toBe(200);
    expect((body as { registry_type: string }).registry_type).toBe("langfuse");
  });

  it("a present-but-EMPTY required environment_id is 404 on the unknown id, not a 422 missing-param", async () => {
    // FastAPI passes `?environment_id=` through as "" (present ≠ missing — the pinned "" rule);
    // the handler then 404s on the unknown id. Only an ABSENT param is a RequestValidationError.
    let caught: ProjectControlPlaneError | undefined;
    try {
      await get("/api/v1/workflows/workflow/bundle", { environment_id: "" });
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(404);
    expect(caught?.message).toBe("unknown project environment: ");
  });

  it("a resolution-gated route on the foreign python project fails closed with 501 UnsupportedRuntime", async () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      await get("/api/v1/projects/py-project/workflows/flow/bundle", { environment_id: "local" });
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(501);
    expect(caught?.errorName).toBe("UnsupportedRuntime");
    expect(caught?.message).toMatch(/supported: typescript/);
  });

  it("deployments list is empty and an unknown plan is 404", async () => {
    expect((await get("/api/v1/deployments")).body).toEqual([]);
    await expect(get("/api/v1/deployments/nope")).rejects.toThrow(/unknown deployment plan: nope/);
  });

  it("projects lists both entries with correct resolvable/runtime", async () => {
    const { body } = await get("/api/v1/projects") as { body: Array<{ id: string; resolvable: boolean }> };
    expect(body.map((p) => p.id).sort()).toEqual(["conformance", "py-project"]);
  });
});

describe("node:http server — live loopback round-trip", () => {
  it("serves meta over a real socket and closes cleanly", async () => {
    const started = await serve({ ...context(), port: 0, host: "127.0.0.1" });
    try {
      const response = await fetch(`http://127.0.0.1:${started.port}/api/v1/meta`);
      expect(response.status).toBe(200);
      const meta = (await response.json()) as { runtime: string; project: string };
      expect(meta.runtime).toBe("typescript");
      expect(meta.project).toBe("conformance-fixture");

      const missing = await fetch(`http://127.0.0.1:${started.port}/api/v1/workflows/workflow/bundle`);
      expect(missing.status).toBe(422);
      const envelope = (await missing.json()) as { error: string; message: string };
      expect(envelope.error).toBe("RequestValidationError");
      expect(envelope.message).toBe("1 validation error: query.environment_id: Field required");

      const notFound = await fetch(`http://127.0.0.1:${started.port}/api/v1/nope`);
      expect(notFound.status).toBe(404);
      expect(((await notFound.json()) as { error: string }).error).toBe("NotFound");

      // A malformed percent-encoded path segment must NOT crash the process (decodeURIComponent
      // throws URIError) — it falls through to a clean 404, and the server stays up for the next call.
      const malformed = await fetch(`http://127.0.0.1:${started.port}/api/v1/environments/%zz`);
      expect(malformed.status).toBe(404);
      const stillUp = await fetch(`http://127.0.0.1:${started.port}/api/v1/meta`);
      expect(stillUp.status).toBe(200);

      // HEAD matches the GET table with an empty body (Starlette auto-derives HEAD for GET routes).
      const head = await fetch(`http://127.0.0.1:${started.port}/api/v1/meta`, { method: "HEAD" });
      expect(head.status).toBe(200);
      expect(await head.text()).toBe("");

      // A known path under the wrong method is Starlette's 405, not a 404.
      const wrongMethod = await fetch(`http://127.0.0.1:${started.port}/api/v1/meta`, { method: "POST" });
      expect(wrongMethod.status).toBe(405);
      expect(((await wrongMethod.json()) as { message: string }).message).toBe("Method Not Allowed");
    } finally {
      await started.close();
    }
  });

  it("fails closed on WIRE-duplicated proxy headers over a real socket (#577)", async () => {
    // node's ClientRequest sends an array header value as separate wire lines — real duplicate
    // occurrences, which the adapter must surface distinctly (headersDistinct) instead of the
    // joined `req.headers` string. fetch/undici would join client-side, hence raw http here.
    const started = await serve({
      ...context(),
      authorizer: buildAuthorizer([], { trustProxy: true })!,
      port: 0,
      host: "127.0.0.1",
    });
    const rawGet = (headers: Record<string, string | string[]>) =>
      new Promise<{ status: number; body: { caller_identity?: string | null } }>((resolvePromise, reject) => {
        const req = httpRequest(
          { host: "127.0.0.1", port: started.port, path: "/api/v1/meta", method: "GET", headers },
          (res) => {
            const chunks: Buffer[] = [];
            res.on("data", (chunk: Buffer) => chunks.push(chunk));
            res.on("end", () =>
              resolvePromise({
                status: res.statusCode ?? 0,
                body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
              }),
            );
          },
        );
        req.on("error", reject);
        req.end();
      });
    try {
      // Smuggled duplicate actor header → identity null (never "mallory, alice" or first-wins).
      const dupActor = await rawGet({
        "X-Typeflux-Actor": ["mallory", "alice"],
        "X-Typeflux-Permissions": "inspect",
      });
      expect(dupActor.status).toBe(200);
      expect(dupActor.body.caller_identity).toBeNull();

      // Duplicate permissions header → absent → no grants: even the read 403s (no union escalation).
      const dupPerms = await rawGet({
        "X-Typeflux-Actor": "alice",
        "X-Typeflux-Permissions": ["inspect", "*"],
      });
      expect(dupPerms.status).toBe(403);

      // A clean single pair still authenticates and echoes the principal.
      const clean = await rawGet({ "X-Typeflux-Actor": "alice", "X-Typeflux-Permissions": "inspect" });
      expect(clean.status).toBe(200);
      expect(clean.body.caller_identity).toBe("alice");
    } finally {
      await started.close();
    }
  });

  it("fails closed on WIRE-duplicated Authorization over a real socket (#738 follow-up, token parity)", async () => {
    // node's ClientRequest sends an array header value as separate wire lines — genuine
    // duplicate Authorization occurrences, which the adapter surfaces distinctly
    // (headersDistinct). fetch/undici would join client-side, hence raw http here. A wire
    // duplicate can't be expressed in a conformance fixture (object-shaped headers), so this
    // socket test — mirrored by the Python `_MultiHeaders` test — is the cross-edition parity
    // pin: more than one Authorization header is rejected exactly like a bad token (403), not
    // joined and first-wins/order-honored.
    const started = await serve({
      ...context(),
      authorizer: buildAuthorizer(["reader:inspect:conformance-reader-token"])!,
      port: 0,
      host: "127.0.0.1",
    });
    const rawStatus = (headers: Record<string, string | string[]>) =>
      new Promise<number>((resolvePromise, reject) => {
        const req = httpRequest(
          { host: "127.0.0.1", port: started.port, path: "/api/v1/meta", method: "GET", headers },
          (res) => {
            res.on("data", () => {});
            res.on("end", () => resolvePromise(res.statusCode ?? 0));
          },
        );
        req.on("error", reject);
        req.end();
      });
    try {
      // Two valid tokens → still rejected: no occurrence is trusted (no "first wins" union).
      expect(
        await rawStatus({ Authorization: ["Bearer conformance-reader-token", "Bearer conformance-reader-token"] }),
      ).toBe(403);
      // Valid + garbage → rejected too (the smuggled-second-credential shape).
      expect(
        await rawStatus({ Authorization: ["Bearer conformance-reader-token", "Bearer nope"] }),
      ).toBe(403);
      // A single valid Authorization still authenticates — the guard only bites on >1.
      expect(await rawStatus({ Authorization: "Bearer conformance-reader-token" })).toBe(200);
    } finally {
      await started.close();
    }
  });
});

describe("validate route — 404 pre-flight on unknown selected ids (Python parity)", () => {
  it("an unknown environment_id query is 404, not an in-band validation issue", async () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      await get("/api/v1/validate", { environment_id: "ghost" });
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(404);
    expect(caught?.message).toBe("unknown project environment: ghost");
  });

  it("an EMPTY-string environment_id is 404 too (Python gates on `is not None`, '' included)", async () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      await get("/api/v1/validate", { environment_id: "" });
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(404);
    expect(caught?.message).toBe("unknown project environment: ");
  });

  it("an unknown workflow_id query is 404", async () => {
    await expect(get("/api/v1/validate", { workflow_id: "ghost" })).rejects.toThrow(/unknown project workflow: ghost/);
  });
});

describe("auth boundary — token + proxy profiles over the route table (#620 slice 3)", () => {
  // The canonical conformance grants (fixtures/_suite.json, mirrored by serve-typescript.sh).
  const tokenContext = () => ({
    ...context(),
    authorizer: buildAuthorizer([
      "reader:inspect:conformance-reader-token",
      "operator:*:conformance-operator-token",
      "starter:start:conformance-starter-token",
    ])!,
  });
  const proxyContext = () => ({ ...context(), authorizer: buildAuthorizer([], { trustProxy: true })! });

  it("a missing or unknown bearer token is 403 Forbidden — no 401 exists, validity undisclosed", async () => {
    for (const headers of [{}, { authorization: "Bearer not-a-configured-token" }]) {
      let caught: ProjectControlPlaneError | undefined;
      try {
        await request("GET", "/api/v1/meta", {}, headers, tokenContext());
      } catch (error) {
        caught = error as ProjectControlPlaneError;
      }
      expect(caught?.status).toBe(403);
      expect(caught?.errorName).toBe("Forbidden");
      expect(caught?.message).toBe("operation requires the 'inspect' permission");
    }
  });

  it("reader meta: grants intersect with server abilities (can_resolve true, operation flags false)", async () => {
    const { status, body } = await request(
      "GET",
      "/api/v1/meta",
      {},
      { authorization: "Bearer conformance-reader-token" },
      tokenContext(),
    );
    expect(status).toBe(200);
    expect((body as { capabilities: unknown }).capabilities).toEqual({
      can_start: false,
      can_review: false,
      can_cancel: false,
      can_refresh_project: false,
      can_resolve: true,
      // Resolution-bound, so it tracks can_resolve (true here) regardless of the actor's grants (#723).
      enforcement_events: true,
      github_provenance: false,
    });
  });

  it("a start grant implies inspect (meta readable) and can_start is now true (operate tier served)", async () => {
    const { status, body } = await request(
      "GET",
      "/api/v1/meta",
      {},
      { authorization: "Bearer conformance-starter-token" },
      tokenContext(),
    );
    expect(status).toBe(200);
    // start grant ∩ resolvable ∩ operations-served → can_start true; the other operate flags stay
    // false (the starter holds only `start`), can_resolve true.
    expect((body as { capabilities: unknown }).capabilities).toEqual({
      can_start: true,
      can_review: false,
      can_cancel: false,
      can_refresh_project: false,
      can_resolve: true,
      enforcement_events: true,
      github_provenance: false,
    });
  });

  it("a reader POSTing start gets Python's exact 403 before anything else runs", async () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      await request(
        "POST",
        "/api/v1/workflows/workflow/start",
        {},
        { authorization: "Bearer conformance-reader-token" },
        tokenContext(),
      );
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(403);
    expect(caught?.message).toBe("operation requires the 'start' permission");
  });

  it("a privileged caller on start passes the permission gate and reaches body validation (422, no stub)", async () => {
    // The operate tier is real now: an authorized operator with an EMPTY body gets the FastAPI-
    // parity body-validation 422 (missing required fields), NOT the old 501 stub — proving the
    // permission gate passed and the real handler ran.
    let caught: ProjectControlPlaneError | undefined;
    try {
      await request(
        "POST",
        "/api/v1/workflows/workflow/start",
        {},
        { authorization: "Bearer conformance-operator-token" },
        tokenContext(),
      );
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(422);
    expect(caught?.errorName).toBe("RequestValidationError");
    // The test helper sends no body → the body-root validation error (a present `{}` body would
    // instead list each missing field); either way it is the real handler's 422, not the old stub.
    expect(caught?.message).toMatch(/^\d+ validation error/);
  });

  it("a start-ONLY token passes the router inspect gate via implication and reaches the real handler", async () => {
    // Pins Actor.can()'s grant-implies-inspect through BOTH gates (dispatch inspect + route start):
    // if the implication ever broke, this caller would 403 at the outer gate instead of reaching
    // the handler's body validation (422 on the empty body).
    let caught: ProjectControlPlaneError | undefined;
    try {
      await request(
        "POST",
        "/api/v1/workflows/workflow/start",
        {},
        { authorization: "Bearer conformance-starter-token" },
        tokenContext(),
      );
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(422);
    expect(caught?.errorName).toBe("RequestValidationError");
  });

  it("migrate requires BOTH start AND cancel — a start-ONLY token 403s on the missing 'cancel' (#204)", async () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      await request(
        "POST",
        "/api/v1/workflows/workflow/migrate",
        {},
        { authorization: "Bearer conformance-starter-token" },
        tokenContext(),
      );
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(403);
    // start is granted (checked first, passes), so the refusal names the missing 'cancel'.
    expect(caught?.message).toBe("operation requires the 'cancel' permission");
  });

  it("migrate: an operator with both grants passes the gate and reaches body validation (422, #204)", async () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      await request(
        "POST",
        "/api/v1/workflows/workflow/migrate",
        {},
        { authorization: "Bearer conformance-operator-token" },
        tokenContext(),
      );
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(422);
    expect(caught?.errorName).toBe("RequestValidationError");
  });

  it("an operation on an UNKNOWN project is 404 after the permission gate (valid body), not the tier 501", async () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      await request(
        "POST",
        "/api/v1/projects/nope/workflows/workflow/start",
        {},
        { authorization: "Bearer conformance-operator-token" },
        tokenContext(),
        { environment_id: "local", execution_id: "e-1", input: {} },
      );
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(404);
    expect(caught?.message).toBe("unknown project: nope");
  });

  it("start validates the input against the workflow's schema — 422 with the coercion message (codex)", async () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      await request(
        "POST",
        "/api/v1/workflows/workflow/start",
        {},
        { authorization: "Bearer conformance-operator-token" },
        tokenContext(),
        // ClaimInput requires claims: ClaimItem[] — an empty object is a Python-side 422 too.
        { environment_id: "local", execution_id: "e-bad-input", input: {} },
      );
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(422);
    expect(caught?.message).toMatch(/^invalid workflow input for ClaimInput: /);
  });

  it("trace coerces like a FastAPI boolean: 1 gates exactly like true, garbage is a 422 (codex)", async () => {
    // trace=1 from an inspect-only caller must 403 (not silently read as false).
    await expect(
      request(
        "GET",
        "/api/v1/workflows/workflow/status",
        { environment_id: "local", execution_id: "e-1", trace: "1" },
        { authorization: "Bearer conformance-reader-token" },
        tokenContext(),
      ),
    ).rejects.toMatchObject({ status: 403 });
    await expect(
      request(
        "GET",
        "/api/v1/workflows/workflow/status",
        { environment_id: "local", execution_id: "e-1", trace: "banana" },
        { authorization: "Bearer conformance-reader-token" },
        tokenContext(),
      ),
    ).rejects.toMatchObject({ status: 422, errorName: "RequestValidationError" });
  });

  it("a MALFORMED body 422s before the project gates — FastAPI's pre-handler solve order", async () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      await request(
        "POST",
        "/api/v1/projects/nope/workflows/workflow/start",
        {},
        { authorization: "Bearer conformance-operator-token" },
        tokenContext(),
        // missing every required field — Python validates the body before _project() runs.
      );
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(422);
    expect(caught?.errorName).toBe("RequestValidationError");
  });

  it("project refresh is permission-gated the same way (single mount)", async () => {
    await expect(
      request(
        "POST",
        "/api/v1/projects/conformance/refresh",
        {},
        { authorization: "Bearer conformance-reader-token" },
        tokenContext(),
      ),
    ).rejects.toThrow(/operation requires the 'project.refresh' permission/);
  });

  it("proxy: no headers is 403; an unmodeled permission is tolerantly ignored; actor grants apply", async () => {
    await expect(request("GET", "/api/v1/meta", {}, {}, proxyContext())).rejects.toThrow(
      /operation requires the 'inspect' permission/,
    );
    const bob = await request(
      "GET",
      "/api/v1/meta",
      {},
      { "x-typeflux-actor": "bob", "x-typeflux-permissions": "inspect banana" },
      proxyContext(),
    );
    expect(bob.status).toBe(200);
    const alice = await request(
      "GET",
      "/api/v1/meta",
      {},
      { "x-typeflux-actor": "alice", "x-typeflux-permissions": "inspect,review" },
      proxyContext(),
    );
    // can_review intersects with the operate tier (now served) ∩ operability (TS project) → true;
    // alice holds `review` but not `start`/`cancel`, so those stay false.
    expect((alice.body as { capabilities: unknown }).capabilities).toEqual({
      can_start: false,
      can_review: true,
      can_cancel: false,
      can_refresh_project: false,
      can_resolve: true,
      enforcement_events: true,
      github_provenance: false,
    });
  });
});

describe("auth ordering over a real socket — 403 wins over body-shape errors", () => {
  it("an unauthenticated POST with a garbage body is 403, never a 422 JSON error (Python dependency order)", async () => {
    const started = await serve({
      ...context(),
      authorizer: buildAuthorizer(["reader:inspect:conformance-reader-token"])!,
      port: 0,
      host: "127.0.0.1",
    });
    try {
      const response = await fetch(`http://127.0.0.1:${started.port}/api/v1/workflows/workflow/start`, {
        method: "POST",
        body: "{not json",
        headers: { "content-type": "application/json" },
      });
      expect(response.status).toBe(403);
      expect(((await response.json()) as { message: string }).message).toBe(
        "operation requires the 'inspect' permission",
      );
    } finally {
      await started.close();
    }
  });
});

describe("operate tier routes — refresh/repin/204/body-validation (#563)", () => {
  /** Run an operate POST handler with a JSON body against a fresh route table. */
  const post = (
    path: string,
    body: unknown,
    headers: Record<string, string> = {},
    routeContext: ReturnType<typeof context> = context(),
  ): Promise<RouteResponse> => {
    const routes = buildRoutes(routeContext);
    const segments = path.split("/").filter((s) => s.length > 0);
    for (const route of routes) {
      if (route.method !== "POST" || route.segments.length !== segments.length) continue;
      const params: Record<string, string> = {};
      let matched = true;
      for (let i = 0; i < route.segments.length; i += 1) {
        const pattern = route.segments[i]!;
        if (pattern.startsWith("{") && pattern.endsWith("}")) params[pattern.slice(1, -1)] = segments[i]!;
        else if (pattern !== segments[i]) {
          matched = false;
          break;
        }
      }
      // Wrap in an async IIFE so a handler's SYNCHRONOUS throw (refresh/repin are sync) surfaces
      // as a rejected promise for `.rejects`, matching how the node adapter's dispatch awaits it.
      if (matched) return (async () => route.handler({ params, query: {}, body: () => body, headers }))();
    }
    throw new Error(`no route matched POST ${path}`);
  };

  it("refresh returns the honest local-checkout DTO (Python ProjectRefreshResult shape)", async () => {
    const { status, body } = await post("/api/v1/projects/conformance/refresh", {});
    expect(status).toBe(200);
    expect(body).toMatchObject({
      id: "conformance",
      source: "local",
      refreshed: false,
      ref: null,
      sha: null,
      detail: "local checkout — nothing to refresh",
    });
    expect(typeof (body as { refreshed_at: string }).refreshed_at).toBe("string");
  });

  it("refresh 404s an unknown project", async () => {
    await expect(post("/api/v1/projects/nope/refresh", {})).rejects.toMatchObject({
      status: 404,
      message: "unknown project: nope",
    });
  });

  it("repin returns the honest no-pin DTO (the TS operate tier pins nothing)", async () => {
    const { status, body } = await post("/api/v1/workflows/workflow/repin", { environment_id: "local" });
    expect(status).toBe(200);
    expect(body).toEqual({ repinned: false, dropped: 0 });
  });

  it("repin follows the guard order: workflow 404 then environment 404", async () => {
    await expect(post("/api/v1/workflows/nope/repin", { environment_id: "local" })).rejects.toMatchObject({
      status: 404,
      message: "unknown project workflow: nope",
    });
    await expect(
      post("/api/v1/workflows/workflow/repin", { environment_id: "nope" }),
    ).rejects.toMatchObject({ status: 404, message: "unknown project environment: nope" });
  });

  it("start body validation: missing required fields is a 422 RequestValidationError listing each", async () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      await post("/api/v1/workflows/workflow/start", {});
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(422);
    expect(caught?.errorName).toBe("RequestValidationError");
    expect(caught?.message).toMatch(/body\.environment_id: Field required/);
    expect(caught?.message).toMatch(/body\.execution_id: Field required/);
    expect(caught?.message).toMatch(/body\.input: Field required/);
  });

  it("start body validation: an extra key is rejected (extra=forbid parity)", async () => {
    await expect(
      post("/api/v1/workflows/workflow/start", {
        environment_id: "local",
        execution_id: "e",
        input: {},
        surprise: 1,
      }),
    ).rejects.toMatchObject({ status: 422, message: /body\.surprise: Extra inputs are not permitted/ });
  });

  it("start with an unknown policy id is a 422 ProjectPolicyError (composition over declared policies, #663)", async () => {
    await expect(
      post("/api/v1/workflows/workflow/start", {
        environment_id: "local",
        execution_id: "e",
        input: { claims: [] },
        policy_ids: ["nope"],
      }),
    ).rejects.toMatchObject({ status: 422, errorName: "ProjectPolicyError" });
  });

  it("start with a wrong expected_policy_hash is a 422 mismatch BEFORE the Temporal tier (#663)", async () => {
    // Deterministic without a cluster: the policy gate precedes the bounded connect, so this
    // answers instantly (not a 503) — the guard-order pin for the operate policy gate.
    await expect(
      post("/api/v1/workflows/workflow/start", {
        environment_id: "local",
        execution_id: "e",
        input: { claims: [] },
        policy_ids: ["base"],
        expected_policy_hash: "deadbeef",
      }),
    ).rejects.toMatchObject({
      status: 422,
      errorName: "ProjectPolicyEnforcementError",
      message: /does not match expected deployment policy hash \(expected=deadbeef/,
    });
  });

  it("a VALID policy selection is honored — the gate passes and start reaches the Temporal tier (#663)", async () => {
    // The read-side bundle exposes the composed closure hash the operate gate verifies; with the
    // matching hash the request clears composition + admission + hash-verify and fails only at the
    // (absent) cluster — 503, exactly where an unpoliced start fails.
    const bundleBody = (await get("/api/v1/workflows/workflow/bundle", { environment_id: "local" })).body as {
      policy: { policy_hash: string };
    };
    const prior = process.env["TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS"];
    process.env["TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS"] = "2";
    let caught: ProjectControlPlaneError | undefined;
    try {
      await post("/api/v1/workflows/workflow/start", {
        environment_id: "unreachable",
        execution_id: "e",
        input: { claims: [] },
        policy_ids: ["base"],
        expected_policy_hash: bundleBody.policy.policy_hash,
      });
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    } finally {
      if (prior === undefined) delete process.env["TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS"];
      else process.env["TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS"] = prior;
    }
    expect(caught?.status).toBe(503);
    expect(caught?.errorName).toBe("TemporalUnavailable");
  }, 10000);

  it("start against the unreachable environment is a 503 TemporalUnavailable (bounded, no server)", async () => {
    // No client injected → the real connector races the bound; the unreachable env refuses the
    // connection → 503, exactly like the executions-temporal-unreachable read case. Shorten the
    // bound so the test does not wait the 10s default when the connect hangs rather than refuses.
    const prior = process.env["TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS"];
    process.env["TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS"] = "2";
    let caught: ProjectControlPlaneError | undefined;
    try {
      await post("/api/v1/workflows/workflow/start", {
        environment_id: "unreachable",
        execution_id: "e",
        input: { claims: [] },
      });
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    } finally {
      if (prior === undefined) delete process.env["TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS"];
      else process.env["TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS"] = prior;
    }
    expect(caught?.status).toBe(503);
    expect(caught?.errorName).toBe("TemporalUnavailable");
  }, 10000);

  it("review answers 204 with an empty body (against the unreachable env it is 503 first)", async () => {
    // Review needs a live cluster to reach the signal; against unreachable it 503s at describe.
    const prior = process.env["TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS"];
    process.env["TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS"] = "2";
    let caught: ProjectControlPlaneError | undefined;
    try {
      await post("/api/v1/workflows/workflow/review", {
        environment_id: "unreachable",
        execution_id: "e",
        command: { user_decision: "approve" },
      });
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    } finally {
      if (prior === undefined) delete process.env["TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS"];
      else process.env["TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS"] = prior;
    }
    expect(caught?.status).toBe(503);
  }, 10000);

  it("review permission gate runs FIRST — a reader 403s before body validation", async () => {
    const tokenCtx = {
      registry: loadProjectRegistry(REGISTRY_PATH),
      schemasFor: () => CONFORMANCE_SCHEMAS,
      authorizer: buildAuthorizer(["reader:inspect:conformance-reader-token"])!,
    };
    await expect(
      post("/api/v1/workflows/workflow/review", { garbage: true }, { authorization: "Bearer conformance-reader-token" }, tokenCtx),
    ).rejects.toMatchObject({ status: 403, message: "operation requires the 'review' permission" });
  });

  it("cancel permission gate: a reader 403s with the 'cancel' message", async () => {
    const tokenCtx = {
      registry: loadProjectRegistry(REGISTRY_PATH),
      schemasFor: () => CONFORMANCE_SCHEMAS,
      authorizer: buildAuthorizer(["reader:inspect:conformance-reader-token"])!,
    };
    await expect(
      post("/api/v1/workflows/workflow/cancel", {}, { authorization: "Bearer conformance-reader-token" }, tokenCtx),
    ).rejects.toMatchObject({ status: 403, message: "operation requires the 'cancel' permission" });
  });

  it("status GET: trace=true from an inspect-only caller is a 403 (auditable op needs operate-class)", async () => {
    const tokenCtx = {
      registry: loadProjectRegistry(REGISTRY_PATH),
      schemasFor: () => CONFORMANCE_SCHEMAS,
      authorizer: buildAuthorizer(["reader:inspect:conformance-reader-token"])!,
    };
    await expect(
      request(
        "GET",
        "/api/v1/workflows/workflow/status",
        { environment_id: "local", execution_id: "e", trace: "true" },
        { authorization: "Bearer conformance-reader-token" },
        tokenCtx,
      ),
    ).rejects.toMatchObject({ status: 403, message: /trace=true status records an auditable/ });
  });

  it("status GET: missing required execution_id is a 422 RequestValidationError", async () => {
    await expect(
      request("GET", "/api/v1/workflows/workflow/status", { environment_id: "local" }),
    ).rejects.toMatchObject({ status: 422, message: "1 validation error: query.execution_id: Field required" });
  });
});

describe("load failure — a broken manifest is a 422 config error, not a 500", () => {
  it("a project whose manifest fails to load surfaces a 422 from a read route", async () => {
    const dir = mkdtempSync(join(tmpdir(), "tf-broken-"));
    tempDirs.push(dir);
    // A registry pointing at a nonexistent manifest: the entry is known (no 404), but resolveProject
    // throws a plain loader Error — the adapter must wrap it as a 422, not let it fall through to 500.
    const registryFile = join(dir, "typeflux.projects.yaml");
    writeFileSync(
      registryFile,
      "version: '1'\ndefault: broken\nprojects:\n  - id: broken\n    manifest: does-not-exist.yaml\n    runtime: typescript\n",
      "utf8",
    );
    const ctx = { registry: loadProjectRegistry(registryFile), schemasFor: () => undefined };
    const routes = buildRoutes(ctx);
    const metaRoute = routes.find((r) => r.segments.join("/") === "api/v1/meta")!;
    let caught: ProjectControlPlaneError | undefined;
    try {
      metaRoute.handler({ params: {}, query: {}, body: () => undefined, headers: {} });
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught).toBeInstanceOf(ProjectControlPlaneError);
    expect(caught?.status).toBe(422);
  });
});

describe("enforcement-events route — request surface + transport seam (#723 slice 2)", () => {
  // A full fake transport (all tiers stubbed empty); `searchEnforcementTraces` is what the feed reads.
  const fakeTransport = (overrides: Partial<LangfuseControlPlaneTransport> = {}): LangfuseControlPlaneTransport => ({
    ping: async () => undefined,
    promptLabelVersion: async () => undefined,
    lastRunPromptVersions: async () => ({}),
    traceSummary: async () => null,
    searchEnforcementTraces: async () => [],
    ...overrides,
  });

  it("requires environment_id — an unscoped call is a 422 InvalidRequest (never a silently-empty feed)", async () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      await get("/api/v1/enforcement-events");
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(422);
    expect(caught?.errorName).toBe("InvalidRequest");
    expect(caught?.message).toMatch(/enforcement-events requires environment_id/);
  });

  it("rejects an unknown verdict filter with a 422 naming the allowed set", async () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      await get("/api/v1/enforcement-events", { environment_id: "local", verdict: "nope" });
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(422);
    expect(caught?.message).toBe("unknown verdict filter(s): nope; allowed: blocked, rejected");
  });

  it("a scoped, clean project answers 200 with an empty feed + not_configured (observer 'none', no transport)", async () => {
    const { status, body } = await get("/api/v1/enforcement-events", {
      environment_id: "local",
      policy_id: "base",
      workflow_id: "workflow",
    });
    expect(status).toBe(200);
    expect(body).toMatchObject({ events: [], partial: { langfuse: "not_configured" } });
    expect((body as { since: string; until: string }).since).toEqual(expect.any(String));
    expect((body as Record<string, unknown>)["next_cursor"]).toBeUndefined();
  });

  it("404s an unknown environment and an unknown workflow filter", async () => {
    await expect(get("/api/v1/enforcement-events", { environment_id: "nope" })).rejects.toThrow(
      /unknown project environment: nope/,
    );
    await expect(
      get("/api/v1/enforcement-events", { environment_id: "local", workflow_id: "ghost" }),
    ).rejects.toThrow(/unknown project workflow: ghost/);
  });

  it("rejects a non-integer limit with the FastAPI-parity 422 message", async () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      await get("/api/v1/enforcement-events", { environment_id: "local", limit: "abc" });
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(422);
    expect(caught?.message).toMatch(/query\.limit: Input should be a valid integer/);
  });

  it("rejects a cursor minted under different filters (422), never a silent skip/dupe", async () => {
    // Mint a cursor by paginating a langfuse project down to one-per-page, then reuse it with a
    // changed verdict filter — the fingerprint mismatch is a 422.
    const registryPath = langfuseProject();
    const trace = (id: string, when: string): EnforcementTraceRecord => ({
      traceId: id,
      timestamp: when,
      workflowName: "WfFlow",
      workflowId: `${id}-exec`,
      environment: "local",
      appliedPolicyIds: [],
      observations: [{ startTime: when, metadata: { typeflux_moderation: { decision: "block", categories: ["hate"] } } }],
    });
    const ctx = {
      registry: loadProjectRegistry(registryPath),
      schemasFor: () => undefined,
      langfuseFor: () => fakeTransport({ searchEnforcementTraces: async () => [trace("a", "2026-07-05T00:00:00+00:00"), trace("b", "2026-07-04T00:00:00+00:00")] }),
    };
    const first = (await request("GET", "/api/v1/enforcement-events", { environment_id: "local", since: "2026-07-01T00:00:00Z", until: "2026-07-31T00:00:00Z", limit: "1" }, {}, ctx)).body as { next_cursor?: string };
    expect(first.next_cursor).toBeDefined();
    let caught: ProjectControlPlaneError | undefined;
    try {
      await request("GET", "/api/v1/enforcement-events", { environment_id: "local", since: "2026-07-01T00:00:00Z", until: "2026-07-31T00:00:00Z", limit: "1", verdict: "rejected", cursor: first.next_cursor! }, {}, ctx);
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(422);
    expect(caught?.message).toMatch(/pagination cursor does not match the current filters/);
  });

  it("reads runtime moderation-block verdicts through the injected transport (partial ok, name→id normalized)", async () => {
    const registryPath = langfuseProject();
    const moderationTrace: EnforcementTraceRecord = {
      traceId: "trace-block",
      timestamp: "2026-07-05T00:00:00+00:00",
      workflowName: "WfFlow", // the trace records the workflow TYPE name…
      workflowId: "exec-1",
      environment: "local",
      appliedPolicyIds: [],
      observations: [{ startTime: "2026-07-05T00:00:01+00:00", metadata: { typeflux_moderation: { decision: "block", categories: ["hate", "violence"] } } }],
    };
    const ctx = {
      registry: loadProjectRegistry(registryPath),
      schemasFor: () => undefined,
      langfuseFor: () => fakeTransport({ searchEnforcementTraces: async () => [moderationTrace] }),
    };
    const { status, body } = await request(
      "GET",
      "/api/v1/enforcement-events",
      { environment_id: "local", since: "2026-07-01T00:00:00Z", until: "2026-07-31T00:00:00Z" },
      {},
      ctx,
    );
    expect(status).toBe(200);
    const feed = body as { partial: { langfuse: string }; events: Array<Record<string, unknown>> };
    expect(feed.partial.langfuse).toBe("ok");
    expect(feed.events).toHaveLength(1);
    expect(feed.events[0]).toMatchObject({
      source: "runtime",
      verdict: "blocked",
      rule: "moderation.on_violation.block",
      workflow_id: "wf", // …normalized to the PROJECT id via the report's resolved workflows.
      execution_id: "exec-1",
      evidence: { trace_id: "trace-block" },
    });
  });

  it("degrades LOUDLY to unreachable (never a silent empty ok) when the transport rejects", async () => {
    const registryPath = langfuseProject();
    const ctx = {
      registry: loadProjectRegistry(registryPath),
      schemasFor: () => undefined,
      langfuseFor: () =>
        fakeTransport({
          searchEnforcementTraces: async () => {
            throw new Error("langfuse down");
          },
        }),
    };
    const { status, body } = await request("GET", "/api/v1/enforcement-events", { environment_id: "local" }, {}, ctx);
    expect(status).toBe(200);
    expect(body).toMatchObject({ events: [], partial: { langfuse: "unreachable" } });
  });

  it("reports not_configured WITHOUT a network call when the transport's credentials are unconfigured (#723 P1-3)", async () => {
    // Python `_langfuse_env_configured`: an empty-credentialed transport answers not_configured up
    // front, never 401ing into a misleading `unreachable`. The stub throws to prove it is not called.
    const registryPath = langfuseProject();
    let called = false;
    const ctx = {
      registry: loadProjectRegistry(registryPath),
      schemasFor: () => undefined,
      langfuseFor: () =>
        fakeTransport({
          enforcementCredentialsConfigured: false,
          searchEnforcementTraces: async () => {
            called = true;
            throw new Error("must not be called when credentials are unconfigured");
          },
        }),
    };
    const { status, body } = await request("GET", "/api/v1/enforcement-events", { environment_id: "local" }, {}, ctx);
    expect(status).toBe(200);
    expect(body).toMatchObject({ partial: { langfuse: "not_configured" } });
    expect(called).toBe(false);
  });

  it("scopes runtime events by environment via METADATA client-side, not a native Langfuse tag (#723 P0-1)", async () => {
    // Both traces are returned by the (environment-param-free) transport; the feed keeps only the one
    // whose trace METADATA environment matches the request scope — proving the client-side filter.
    const registryPath = langfuseProject();
    const trace = (id: string, environment: string): EnforcementTraceRecord => ({
      traceId: id,
      timestamp: "2026-07-05T00:00:00+00:00",
      workflowName: "WfFlow",
      workflowId: `${id}-exec`,
      environment,
      appliedPolicyIds: [],
      observations: [{ startTime: "2026-07-05T00:00:01+00:00", metadata: { typeflux_moderation: { decision: "block", categories: ["hate"] } } }],
    });
    const ctx = {
      registry: loadProjectRegistry(registryPath),
      schemasFor: () => undefined,
      langfuseFor: () => fakeTransport({ searchEnforcementTraces: async () => [trace("in", "local"), trace("out", "staging")] }),
    };
    const { status, body } = await request(
      "GET",
      "/api/v1/enforcement-events",
      { environment_id: "local", since: "2026-07-01T00:00:00Z", until: "2026-07-31T00:00:00Z" },
      {},
      ctx,
    );
    expect(status).toBe(200);
    const feed = body as { partial: { langfuse: string }; events: Array<Record<string, unknown>> };
    expect(feed.partial.langfuse).toBe("ok");
    expect(feed.events.map((e) => e["execution_id"])).toEqual(["in-exec"]); // the staging trace is metadata-filtered out
  });

  it("rejects a rolled-over calendar date (2026-02-30) with a datetime 422 (#723 P1-6)", async () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      await get("/api/v1/enforcement-events", { environment_id: "local", since: "2026-02-30" });
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(422);
    expect(caught?.message).toMatch(/query\.since: Input should be a valid datetime or date/);
  });

  it("is resolution-gated: the feed on a foreign python project fails closed with 501", async () => {
    await expect(get("/api/v1/projects/py-project/enforcement-events", { environment_id: "local" })).rejects.toThrow(
      /this server cannot resolve/,
    );
  });

  it("serves the project-scoped mount identically to the default mount", async () => {
    const { status, body } = await get("/api/v1/projects/conformance/enforcement-events", { environment_id: "local" });
    expect(status).toBe(200);
    expect(body).toMatchObject({ partial: { langfuse: "not_configured" } });
  });
});

/** A minimal on-disk TS project whose workflow declares langfuse observability and NO policy — so the
 * enforcement feed's runtime source is Langfuse (observer honest) and admission stays clean/empty. */
function langfuseProject(): string {
  const dir = mkdtempSync(join(tmpdir(), "tf-enforce-"));
  tempDirs.push(dir);
  mkdirSync(join(dir, "environments"), { recursive: true });
  writeFileSync(
    join(dir, "wf.yaml"),
    [
      "project: p",
      "name: WfFlow",
      "task_queue: base-queue",
      "runtime:",
      "  temporal: {}",
      "  registry: { type: inline, prompts: { p/x: hi } }",
      "  provider: { type: openai, model: gpt-4o-mini }",
      "  observability: { type: langfuse }",
      "activities:",
      "  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]",
      "workflow:",
      "  name: WfFlow",
      "  input: schemas:In",
      "  steps: [{ id: s, activity: a }]",
      "",
    ].join("\n"),
    "utf8",
  );
  writeFileSync(join(dir, "environments", "local.yaml"), "name: local\noverrides: {}\n", "utf8");
  writeFileSync(
    join(dir, "typeflux.project.yaml"),
    "version: '1'\nname: enforce\nworkflows:\n  - id: wf\n    path: wf.yaml\nenvironments:\n  local: environments/local.yaml\n",
    "utf8",
  );
  writeFileSync(
    join(dir, "typeflux.projects.yaml"),
    "version: '1'\nprojects:\n  - id: enforce\n    manifest: typeflux.project.yaml\n    runtime: typescript\n",
    "utf8",
  );
  return join(dir, "typeflux.projects.yaml");
}

describe("github-provenance route — served drift + plan→PR seam (#727 slice 2)", () => {
  /** A fake GitHub transport returning a fixed result and capturing the served side it was handed. */
  const githubTransport = (
    result: GithubReadResult,
  ): GithubProvenanceTransport & { captured: Record<string, unknown> } => {
    const captured: Record<string, unknown> = {};
    return {
      captured,
      read: async (options) => {
        Object.assign(captured, options);
        return result;
      },
    };
  };

  /**
   * Inject a recorded github source by overriding the two registry provenance accessors the route
   * reads (mirrors the Python monkeypatch; `resolveProject` still serves the local manifest, so no
   * clone is attempted). Returns the same context, mutated.
   */
  const withGithubSource = (
    ctx: RegistryContext,
    options: { url: string; ref?: string; sha?: string | null },
  ): RegistryContext => {
    ctx.registry.repoSource = () => ({ url: options.url, ref: options.ref ?? "main" });
    ctx.registry.repoHeadSha = () => options.sha ?? null;
    return ctx;
  };

  const capabilityOf = (body: unknown): boolean =>
    (body as { capabilities: { github_provenance: boolean } }).capabilities.github_provenance;

  it("a local mount reports not_configured — no head, no network call, plans []", async () => {
    const boom: GithubProvenanceTransport = {
      read: async () => {
        throw new Error("the github reader must not run without a recorded git source");
      },
    };
    const ctx: RegistryContext = { ...context(), githubFor: () => boom };
    const { status, body } = await request("GET", "/api/v1/github-provenance", {}, {}, ctx);
    expect(status).toBe(200);
    expect(body).toEqual({ partial: { github: "not_configured" }, plans: [] });
  });

  it("the meta capability reflects a recorded github source (false → true → false for GHE)", async () => {
    expect(capabilityOf((await get("/api/v1/meta")).body)).toBe(false);
    const gh = withGithubSource(context(), { url: "https://github.com/acme/flows" });
    expect(capabilityOf((await request("GET", "/api/v1/meta", {}, {}, gh)).body)).toBe(true);
    const ghe = withGithubSource(context(), { url: "https://ghe.example/acme/flows" });
    expect(capabilityOf((await request("GET", "/api/v1/meta", {}, {}, ghe)).body)).toBe(false);
  });

  it("surfaces head drift through the injected reader, handing it the derived served side", async () => {
    const transport = githubTransport({ status: "ok", headSha: "remote-sha", commitsBehind: 2, planPrs: {} });
    const ctx: RegistryContext = {
      ...withGithubSource(context(), { url: "https://github.com/acme/flows", ref: "main", sha: "served-sha" }),
      githubFor: () => transport,
    };
    const { status, body } = await request("GET", "/api/v1/github-provenance", {}, {}, ctx);
    expect(status).toBe(200);
    expect(body).toMatchObject({
      partial: { github: "ok" },
      head: { branch: "main", sha: "remote-sha", ahead_of_served: true, commits_behind: 2 },
    });
    expect(transport.captured["branch"]).toBe("main");
    expect(transport.captured["servedSha"]).toBe("served-sha");
    expect((transport.captured["repo"] as { owner: string }).owner).toBe("acme");
  });

  it("a reader degradation without a head keeps head null", async () => {
    const transport = githubTransport({ status: "rate_limited" });
    const ctx: RegistryContext = {
      ...withGithubSource(context(), { url: "https://github.com/acme/flows", sha: "served-sha" }),
      githubFor: () => transport,
    };
    const { status, body } = await request("GET", "/api/v1/github-provenance", {}, {}, ctx);
    expect(status).toBe(200);
    expect(body).toEqual({ partial: { github: "rate_limited" }, plans: [] });
  });

  it("501s for a foreign (python) runtime project — the surface is resolution-bound", async () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      await request("GET", "/api/v1/projects/py-project/github-provenance");
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(501);
    expect(caught?.errorName).toBe("UnsupportedRuntime");
  });
});
