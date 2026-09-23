/**
 * Control-plane authentication & operation authorization (#620 slice 3; Python
 * `controlplane/auth.py`, #292). The boundary is pluggable and OPEN BY DEFAULT — the
 * local server binds 127.0.0.1 and trusts its caller. Two built-in modes cover the rest:
 *
 * - {@link TokenAuthorizer} — bearer tokens, each mapped to a named grant set (the
 *   internal-operator-console pattern).
 * - {@link ProxyHeaderAuthorizer} — trusts an authenticating reverse proxy that passes the
 *   actor id and granted permissions in headers (the enterprise-SSO integration point).
 *   Enabled explicitly so the headers are never honored by accident on an exposed server.
 *
 * The resolved {@link Actor} is used ONLY for authorization and the capability report —
 * never written to typeflux.* execution metadata. There is NO 401: unauthenticated and
 * unauthorized callers both receive 403 `Forbidden`, and token validity is never disclosed.
 */

import { ProjectControlPlaneError } from "../errors.js";

/** An operation category the API authorizes against (Python `Permission`). */
export const PERMISSIONS = ["inspect", "start", "review", "cancel", "project.refresh"] as const;
export type Permission = (typeof PERMISSIONS)[number];

export const ALL_PERMISSIONS: ReadonlySet<Permission> = new Set(PERMISSIONS);

/** State-changing grants — holding any of these is "operate-class"; bare inspect is not. */
export const OPERATE_PERMISSIONS: ReadonlySet<Permission> = new Set([
  "start",
  "review",
  "cancel",
  "project.refresh",
]);

/** The authenticated caller's identity and granted permissions (Python `Actor`). */
export class Actor {
  constructor(
    /** Opaque, capability-report-only — never persisted into execution metadata. */
    readonly id: string | null,
    readonly permissions: ReadonlySet<Permission>,
  ) {}

  can(permission: Permission): boolean {
    if (this.permissions.has(permission)) return true;
    // Any operate grant implies the ability to inspect — you cannot operate on what you
    // cannot see, and reads are the weaker capability.
    return permission === "inspect" && this.permissions.size > 0;
  }

  /** True when the actor holds any state-changing (operate-class) grant. */
  canOperate(): boolean {
    for (const permission of this.permissions) {
      if (OPERATE_PERMISSIONS.has(permission)) return true;
    }
    return false;
  }
}

/**
 * Lowercased request headers, as the node adapter delivers them. A `string[]` value means the
 * header occurred MORE THAN ONCE on the wire (the adapter reads `req.headersDistinct`, which
 * preserves occurrences node's joined `req.headers` would fold into one `", "`-separated
 * string) — the proxy AND token trust seams need that distinction to fail closed on duplicates
 * (#577, #738).
 */
export type RequestHeaders = Readonly<Record<string, string | readonly string[] | undefined>>;

/**
 * Resolves the calling {@link Actor} from the request headers. Must not throw for an
 * unauthenticated caller — return an actor with no permissions so the route dependency
 * fails closed with a 403.
 */
export interface Authorizer {
  authorize(headers: RequestHeaders): Actor;
}

/** Default: trust the caller (local / operator network) and grant everything. */
export class OpenAuthorizer implements Authorizer {
  authorize(): Actor {
    return new Actor(null, ALL_PERMISSIONS);
  }
}

/** The bearer token from an Authorization header, or null (Python `_bearer_token`). */
function bearerToken(headers: RequestHeaders): string | null {
  // Read Authorization through the same single-occurrence guard as the proxy trust seam
  // (#738): an array value means the header occurred MORE THAN ONCE on the wire — a client
  // smuggling a second Authorization header alongside the first — so it is rejected exactly
  // like a bad token (no-permission actor → 403), never joined with ", " and matched as one
  // string. Zero occurrences → no token (unauthenticated, unchanged). The single value is
  // trimmed; a whitespace-only header reads as no token (Python `if not header`).
  const header = singleTrustedHeader(headers, "authorization");
  if (header === null) return null;
  const space = header.indexOf(" ");
  const scheme = space === -1 ? header : header.slice(0, space);
  const value = space === -1 ? "" : header.slice(space + 1).trim();
  if (scheme.toLowerCase() !== "bearer" || value.length === 0) return null;
  return value;
}

export interface TokenGrant {
  name: string;
  permissions: ReadonlySet<Permission>;
}

/**
 * Bearer-token auth: each token maps to a named grant set. Tokens match by exact value;
 * an unknown or missing token yields an actor with no permissions (fail closed). Token
 * VALUES are never logged — only the configured names are identifying.
 */
export class TokenAuthorizer implements Authorizer {
  private readonly tokens: ReadonlyMap<string, TokenGrant>;

  constructor(tokens: ReadonlyMap<string, TokenGrant>) {
    this.tokens = new Map(tokens);
  }

  authorize(headers: RequestHeaders): Actor {
    const token = bearerToken(headers);
    const grant = token !== null ? this.tokens.get(token) : undefined;
    if (grant === undefined) return new Actor(null, new Set());
    return new Actor(grant.name, grant.permissions);
  }
}

/**
 * The single trusted occurrence of header `name`, or null (Python `_single_trusted_header`).
 * Guards every header that is only ever meaningful once at this trust boundary — the proxy
 * actor/permissions headers AND `Authorization` (via {@link bearerToken}).
 * Fail-closed hardening of the proxy trust seam (#577): behind a mis-configured proxy that
 * APPENDS its header instead of replacing the client's, a client-smuggled copy coexists with
 * the proxy's — every occurrence is then attacker-influenced, so DUPLICATE occurrences (a
 * `string[]` from the adapter's `headersDistinct` read) read as absent, never "first wins" or
 * a joined union. The value is trimmed; a whitespace-only value reads as absent too (a
 * bare-space header must not become a literal identity). Occurrences are counted structurally
 * — never by sniffing `", "` in a joined value, which a legitimate single value may contain.
 */
function singleTrustedHeader(headers: RequestHeaders, name: string): string | null {
  const raw = headers[name];
  if (raw === undefined || Array.isArray(raw)) return null;
  const value = (raw as string).trim();
  return value.length > 0 ? value : null;
}

/**
 * Trust an authenticating reverse proxy that injects the actor + grants. Only use behind
 * a proxy that strips these headers from client requests and sets them itself — enabled
 * explicitly (`--trust-proxy-auth`), never by default. The single-occurrence rule is
 * ENFORCED, not assumed (#577, Python parity): a duplicated actor or permissions header —
 * the signature of a proxy that appends rather than replaces, letting a client-smuggled
 * copy through — reads as absent (identity null / no grants), and values are trimmed with
 * whitespace-only reading as absent.
 */
export class ProxyHeaderAuthorizer implements Authorizer {
  constructor(
    private readonly actorHeader = "x-typeflux-actor",
    private readonly permissionsHeader = "x-typeflux-permissions",
  ) {}

  authorize(headers: RequestHeaders): Actor {
    const actorId = singleTrustedHeader(headers, this.actorHeader);
    const raw = singleTrustedHeader(headers, this.permissionsHeader) ?? "";
    return new Actor(actorId, parsePermissions(raw));
  }
}

/**
 * Parse permissions from a comma/space-separated string (Python `parse_permissions`).
 * `*` (or `all`) expands to every permission. Unknown names throw when `strict` (token
 * config is fail-closed), otherwise they are ignored (a tolerant proxy may forward roles
 * Typeflux does not model).
 */
export function parsePermissions(value: string, options: { strict?: boolean } = {}): Set<Permission> {
  const tokens = value.replaceAll(",", " ").split(/\s+/).filter((item) => item.length > 0);
  const permissions = new Set<Permission>();
  for (const token of tokens) {
    const normalized = token.trim().toLowerCase();
    if (normalized === "*" || normalized === "all") return new Set(ALL_PERMISSIONS);
    if ((PERMISSIONS as readonly string[]).includes(normalized)) {
      permissions.add(normalized as Permission);
    } else if (options.strict === true) {
      throw new Error(`unknown permission: '${token}'`);
    }
  }
  return permissions;
}

/**
 * Parse a `NAME:PERMS:TOKEN` token spec (Python `parse_token_spec`). `PERMS` is a
 * comma-separated permission list (or `*`); `TOKEN` is the remainder — the only field
 * allowed to contain `:`. Unknown permissions throw (config is fail-closed).
 */
export function parseTokenSpec(spec: string): { token: string; grant: TokenGrant } {
  const first = spec.indexOf(":");
  const second = first === -1 ? -1 : spec.indexOf(":", first + 1);
  if (first === -1 || second === -1) {
    throw new Error(`token spec must be NAME:PERMS:TOKEN, got '${spec}'`);
  }
  const name = spec.slice(0, first).trim();
  const perms = spec.slice(first + 1, second).trim();
  const token = spec.slice(second + 1).trim();
  if (name.length === 0 || token.length === 0) {
    throw new Error(`token spec must be NAME:PERMS:TOKEN, got '${spec}'`);
  }
  const permissions = parsePermissions(perms, { strict: true });
  if (permissions.size === 0) {
    throw new Error(`token spec '${name}' grants no permissions`);
  }
  return { token, grant: { name, permissions } };
}

/**
 * Build the configured authorizer, or null for the open default (Python
 * `build_authorizer`). Proxy and token modes are mutually exclusive — a deployment
 * authenticates either at the proxy or with built-in tokens, not both.
 */
export function buildAuthorizer(
  tokenSpecs: readonly string[] = [],
  options: { trustProxy?: boolean } = {},
): Authorizer | null {
  const specs = tokenSpecs.filter((spec) => spec.trim().length > 0);
  if (options.trustProxy === true && specs.length > 0) {
    throw new Error("--trust-proxy-auth cannot be combined with --auth-token");
  }
  if (options.trustProxy === true) return new ProxyHeaderAuthorizer();
  if (specs.length === 0) return null;
  const grants = new Map<string, TokenGrant>();
  for (const spec of specs) {
    const { token, grant } = parseTokenSpec(spec);
    const existing = grants.get(token);
    if (existing !== undefined) {
      // A reused token value would silently inherit the last spec's grants — a
      // privilege-escalation footgun. Fail at config time instead.
      throw new Error(
        `token for '${grant.name}' is already configured for '${existing.name}'; ` +
          "each token value must be unique",
      );
    }
    grants.set(token, grant);
  }
  return new TokenAuthorizer(grants);
}

/**
 * The route-level permission gate (Python `require(permission)`): a 403 `Forbidden` with
 * Python's exact detail. Unauthenticated and unauthorized read the same — no 401 exists,
 * and token validity is never disclosed.
 */
export function requirePermission(actor: Actor, permission: Permission): Actor {
  if (!actor.can(permission)) {
    throw new ProjectControlPlaneError(`operation requires the '${permission}' permission`, 403);
  }
  return actor;
}
