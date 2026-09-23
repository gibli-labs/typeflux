// Unit-test network guard shared by the TypeScript packages.
//
// Mirrors packages/python/tests/conftest.py: non-loopback network access is
// blocked with a descriptive error, loopback/localhost and unix sockets stay
// usable, and provider credentials are scrubbed from the environment so unit
// tests cannot silently reach real services. When the live gate
// (TYPEFLUX_LIVE_TEMPORAL=1) is set the guard does not install, so gated live
// integration runs keep full network access and their configured credentials.
//
// Scope: the guard intercepts JS-level transports (net.Socket.connect — which
// http/https/undici route through — and globalThis.fetch). It does NOT cover
// @temporalio/worker's Rust-core NativeConnection; for that path the
// credential/address scrubbing below is the effective isolation.
import net from "node:net";

// Either gate disables the guard: TYPEFLUX_LIVE_TEMPORAL=1 (the Temporal e2e lanes) or
// TYPEFLUX_RUN_LIVE=1 (the langfuse observability seam, #573) — a gated live run needs full
// network + its configured credentials.
const LIVE_GATE_ENV = "TYPEFLUX_LIVE_TEMPORAL";
const LIVE_GATE_ENVS = [LIVE_GATE_ENV, "TYPEFLUX_RUN_LIVE"] as const;

const ISOLATED_ENV_NAMES = new Set([
  "ANTHROPIC_API_KEY",
  "GEMINI_API_KEY",
  "GOOGLE_API_KEY",
  "LANGFUSE_BASE_URL",
  "LANGFUSE_HOST",
  "LANGFUSE_PROMPT_LABEL",
  "LANGFUSE_PUBLIC_KEY",
  "LANGFUSE_SECRET_KEY",
  "LANGSMITH_API_KEY",
  "LANGSMITH_ENDPOINT",
  "LANGSMITH_PROJECT",
  "OPENAI_API_KEY",
  "TEMPORAL_ADDRESS",
  "TEMPORAL_API_KEY",
  "TEMPORAL_NAMESPACE",
  "TEMPORAL_TASK_QUEUE",
  "TEMPORAL_TLS",
  "TEMPORAL_WORKFLOW_ID",
]);
const ISOLATED_ENV_PREFIXES = ["ANTHROPIC_", "GEMINI_", "LANGCHAIN_", "LANGFUSE_", "LANGSMITH_", "OPENAI_", "TEMPORAL_"];

// Parity with the Python guard (ipaddress.is_loopback): the unspecified
// addresses 0.0.0.0 and :: are NOT loopback and stay blocked.
const LOOPBACK_HOSTS = new Set(["", "localhost", "::1", "0:0:0:0:0:0:0:1"]);

function isLoopbackHost(rawHost: string): boolean {
  const host = rawHost.replace(/^\[/, "").replace(/\]$/, "").toLowerCase();
  if (LOOPBACK_HOSTS.has(host)) {
    return true;
  }
  // 127.0.0.0/8 (and its IPv6-mapped form) only counts for IP LITERALS —
  // a hostname like "127.evil.example" must not pass before DNS resolution.
  if (net.isIPv4(host)) {
    return host.startsWith("127.");
  }
  return net.isIPv6(host) && host.startsWith("::ffff:127.");
}

function blockedNetworkError(destination: string): Error {
  return new Error(
    "External network access blocked in unit tests — mark the test live " +
      `(${LIVE_GATE_ENVS.map((name) => `${name}=1`).join(" or ")}) or mock the transport ` +
      `(destination: ${destination})`,
  );
}

function assertLoopbackDestination(args: readonly unknown[]): void {
  const [first, second] = args;
  if (Array.isArray(first)) {
    // net.createConnection / http agents call Socket.connect with a single
    // normalized-arguments ARRAY ([options, callback]) — unwrap it, or the
    // options object below would be the array itself (no .host → "localhost").
    assertLoopbackDestination(first);
    return;
  }
  if (typeof first === "object" && first !== null) {
    const options = first as { path?: unknown; host?: unknown };
    if (typeof options.path === "string") {
      return; // unix domain socket
    }
    const host = typeof options.host === "string" ? options.host : "localhost";
    if (!isLoopbackHost(host)) {
      throw blockedNetworkError(host);
    }
    return;
  }
  if (typeof first === "string" && Number.isNaN(Number(first))) {
    return; // connect(path) — unix domain socket
  }
  // connect(port[, host][, listener])
  const host = typeof second === "string" ? second : "localhost";
  if (!isLoopbackHost(host)) {
    throw blockedNetworkError(host);
  }
}

function installNetworkGuard(): void {
  const originalConnect = net.Socket.prototype.connect;
  net.Socket.prototype.connect = function guardedConnect(
    this: net.Socket,
    ...args: unknown[]
  ): net.Socket {
    assertLoopbackDestination(args);
    return (originalConnect as (...inner: unknown[]) => net.Socket).apply(this, args);
  } as typeof net.Socket.prototype.connect;

  const originalFetch = globalThis.fetch;
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    // fetch never throws synchronously — blocked hosts must reject, so code
    // under test exercises its real .catch()/rejection path.
    let hostname: string;
    try {
      hostname = new URL(input instanceof Request ? input.url : String(input)).hostname;
    } catch {
      return originalFetch(input, init); // let fetch produce its native error
    }
    if (!isLoopbackHost(hostname)) {
      return Promise.reject(blockedNetworkError(hostname));
    }
    return originalFetch(input, init);
  }) as typeof fetch;
}

function clearRuntimeEnvironment(): void {
  for (const name of Object.keys(process.env)) {
    if (ISOLATED_ENV_NAMES.has(name) || ISOLATED_ENV_PREFIXES.some((p) => name.startsWith(p))) {
      delete process.env[name];
    }
  }
}

if (!LIVE_GATE_ENVS.some((name) => process.env[name] === "1")) {
  clearRuntimeEnvironment();
  installNetworkGuard();
}
