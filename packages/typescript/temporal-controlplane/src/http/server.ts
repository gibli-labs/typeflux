/**
 * node:http adapter for the TS control-plane read tier (#620). Wraps the framework-agnostic route
 * table (`handlers.ts`) in a real HTTP server: parse the URL, match a route by method + path
 * segments, decode the query (repeated keys → arrays), read a JSON body, and render the handler's
 * `{status, body}` as JSON. A thrown `ProjectControlPlaneError` becomes the `{error, message}`
 * envelope (#617 `ApiError`); an UNMATCHED path is 404 `NotFound`, an unexpected throw is 500.
 *
 * Auth lives in the route table (#620 slice 3): the dispatch wrapper in handlers.ts resolves the
 * actor from the headers this adapter passes through and gates every route. The server itself is
 * transport-only: all policy lives in the handlers + the core.
 */

import { createServer as createHttpServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";

import { defaultErrorName, ProjectControlPlaneError } from "../errors.js";
import { buildRoutes, type RegistryContext, type Route, type RouteRequest, splitPath } from "./handlers.js";

export interface ServeOptions extends RegistryContext {
  /** TCP port to listen on (0 = an ephemeral port; read the actual one from {@link ControlPlaneServer.port}). */
  port?: number;
  /** Interface to bind (default `127.0.0.1` — a local control plane, not a public listener). */
  host?: string;
}

/** A started control-plane server: the underlying node server, its bound port, and a close handle. */
export interface ControlPlaneServer {
  server: Server;
  port: number;
  close(): Promise<void>;
}

/** Match `segments` against a route's pattern, capturing `{name}` params; undefined when no match. */
/** A percent-decoded path segment, or undefined when the encoding is malformed (a URIError). */
function safeDecode(segment: string): string | undefined {
  try {
    return decodeURIComponent(segment);
  } catch {
    return undefined;
  }
}

function matchRoute(route: Route, method: string, segments: readonly string[]): Record<string, string> | undefined {
  if (route.method !== method) return undefined;
  return matchSegments(route.segments, segments);
}

/** Match path segments against a pattern regardless of method (405-vs-404 discrimination). */
function matchSegments(
  patterns: readonly string[],
  segments: readonly string[],
): Record<string, string> | undefined {
  if (patterns.length !== segments.length) return undefined;
  const params: Record<string, string> = {};
  for (let index = 0; index < patterns.length; index += 1) {
    const pattern = patterns[index]!;
    const actual = segments[index]!;
    if (pattern.startsWith("{") && pattern.endsWith("}")) {
      // A malformed percent-encoded segment (e.g. `%zz`) can't decode — treat the route as
      // non-matching rather than throwing a URIError out of the (otherwise uncaught) match loop,
      // which would crash the process on an unauthenticated request. It falls through to 404.
      const decoded = safeDecode(actual);
      if (decoded === undefined) return undefined;
      params[pattern.slice(1, -1)] = decoded;
    } else if (pattern !== actual) {
      return undefined;
    }
  }
  return params;
}

/** Decode a URL's query string into scalars/arrays (a key appearing more than once collects into an array). */
function decodeQuery(searchParams: URLSearchParams): RouteRequest["query"] {
  const query: Record<string, string | string[]> = {};
  for (const [key, value] of searchParams.entries()) {
    const existing = query[key];
    if (existing === undefined) {
      query[key] = value;
    } else if (Array.isArray(existing)) {
      existing.push(value);
    } else {
      query[key] = [existing, value];
    }
  }
  return query;
}

/** The `{error, message}` envelope for a caller-facing error (#617 `ApiError`). */
function errorEnvelope(error: ProjectControlPlaneError): { status: number; body: { error: string; message: string } } {
  return { status: error.status, body: { error: error.errorName, message: error.message } };
}

/** Build the node:http request listener over a route table — exported for in-process testing. */
export function createRequestListener(context: RegistryContext): Server {
  const routes = buildRoutes(context);
  return createHttpServer((req, res) => {
    const isHead = req.method === "HEAD";
    const send = (status: number, body: unknown): void => {
      // A client that aborted while an ASYNC handler was in flight leaves a destroyed/answered
      // response — writing would throw, and a throw INSIDE the outer catch's own send() would
      // escape the guard as an unhandled rejection. Nothing to answer; drop it.
      if (res.destroyed || res.headersSent) return;
      try {
        // A 204 No Content (review/cancel) carries an EMPTY body — no content-type, no body
        // (Starlette `Response(status_code=204)`). `undefined` body ⇒ empty response, so a write
        // route's success answer is byte-empty like Python's, never the string "undefined".
        // 204/empty bodies: correct for HEAD too by construction (res.end() with no payload),
      // though today only POST routes return body-undefined and HEAD dispatches as GET.
      if (body === undefined) {
          res.writeHead(status, { "content-length": 0 });
          res.end();
          return;
        }
        const payload = JSON.stringify(body);
        res.writeHead(status, {
          "content-type": "application/json",
          "content-length": Buffer.byteLength(payload),
        });
        // HEAD gets the GET response's status + headers with no body (Starlette parity — FastAPI
        // auto-derives HEAD for every GET route).
        res.end(isHead ? undefined : payload);
      } catch {
        res.destroy();
      }
    };

    // A client that aborts mid-request emits 'error' on the request stream; with no listener,
    // the EventEmitter throw would crash the whole (open, unauthenticated) process. Nothing to
    // answer — the peer is gone — so just tear the socket down.
    req.on("error", () => res.destroy());

    // Only body-bearing methods buffer the body (write routes, later slices) — a GET/HEAD with
    // stray bytes attached must not force the server to hold them in memory.
    const chunks: Buffer[] = [];
    if (req.method !== "GET" && req.method !== "HEAD") {
      req.on("data", (chunk: Buffer) => chunks.push(chunk));
    } else {
      req.resume(); // drain without buffering so 'end' still fires
    }
    req.on("end", () => {
      // A single outer guard, ASYNC-aware: any throw OR rejection in matching/decoding/dispatch
      // becomes a JSON response, never an uncaught exception/rejection that would crash the
      // (open, unauthenticated) process. The IIFE's own promise can never reject — the catch
      // below is terminal.
      void (async () => {
      try {
        let url: URL;
        try {
          url = new URL(req.url ?? "/", "http://localhost");
        } catch {
          // A malformed request surface is 422 InvalidRequest per the #617 taxonomy.
          send(422, { error: defaultErrorName(422), message: "malformed request URL" });
          return;
        }
        const segments = splitPath(url.pathname);
        // HEAD matches the GET route table (Starlette parity); `send` withholds the body above.
        const method = isHead ? "GET" : (req.method ?? "GET");

        let matchedParams: Record<string, string> | undefined;
        let matched: Route | undefined;
        let pathExists = false;
        for (const route of routes) {
          const params = matchRoute(route, method, segments);
          if (params !== undefined) {
            matched = route;
            matchedParams = params;
            break;
          }
          // Track a path that exists under another method — Starlette answers 405 there, not 404.
          if (!pathExists && matchSegments(route.segments, segments) !== undefined) pathExists = true;
        }
        if (matched === undefined || matchedParams === undefined) {
          if (pathExists) {
            // Starlette's auto 405 (no 405 entry in the Python map → the generic HTTPError label).
            send(405, { error: defaultErrorName(405), message: "Method Not Allowed" });
            return;
          }
          // An unmatched path is a 404 NotFound (Python's Starlette 404 → the same discriminant).
          send(404, { error: defaultErrorName(404), message: `Not Found: ${url.pathname}` });
          return;
        }

        // The body parses LAZILY on first handler access — a write route's permission gate runs
        // before body validation (Python dependency order: 403 wins over a garbage body), and
        // read routes never parse stray bytes at all.
        const body = (): unknown => {
          if (chunks.length === 0) return undefined;
          try {
            return JSON.parse(Buffer.concat(chunks).toString("utf8"));
          } catch {
            throw new ProjectControlPlaneError("request body is not valid JSON", 422);
          }
        };

        // Copy into a `RequestHeaders` record from `req.headersDistinct` (never the joined
        // `req.headers`, which folds repeated occurrences into one ", "-separated string and
        // erases the duplication): a header that occurred ONCE becomes its string, one that
        // occurred MORE THAN ONCE stays a string[] so the proxy trust seam can fail closed on
        // client-smuggled duplicates instead of trusting a joined/first value (#577). Node
        // already lowercases the keys.
        const headers: Record<string, string | string[] | undefined> = {};
        for (const [name, values] of Object.entries(req.headersDistinct)) {
          if (values === undefined) continue;
          headers[name] = values.length === 1 ? values[0] : values;
        }
        const result = await matched.handler({
          params: matchedParams,
          query: decodeQuery(url.searchParams),
          body,
          headers,
        });
        send(result.status, result.body);
      } catch (error) {
        if (error instanceof ProjectControlPlaneError) {
          const { status, body: envelope } = errorEnvelope(error);
          send(status, envelope);
          return;
        }
        // An unexpected throw is a 500 — never leak a stack trace to the client (contract: no
        // server-implementation source locations in `message`).
        send(500, { error: defaultErrorName(500), message: "internal server error" });
      }
      })();
    });
  });
}

/** Start a control-plane HTTP server on `options.port`. Resolves once it is listening. */
export function serve(options: ServeOptions): Promise<ControlPlaneServer> {
  const server = createRequestListener(options);
  const host = options.host ?? "127.0.0.1";
  return new Promise((resolvePromise, reject) => {
    server.on("error", reject);
    server.listen(options.port ?? 0, host, () => {
      const address = server.address() as AddressInfo | null;
      const port = address?.port ?? options.port ?? 0;
      resolvePromise({
        server,
        port,
        close: () =>
          new Promise<void>((done, fail) => server.close((error) => (error ? fail(error) : done()))),
      });
    });
  });
}

/** Alias mirroring node's `createServer` naming for callers that want the raw server (no listen). */
export const createServer = createRequestListener;
