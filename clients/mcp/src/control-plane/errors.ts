/**
 * Error taxonomy for the Typeflux control-plane wrapping layer (#326 Phase 0; design §9).
 *
 * Ported from clients/console/src/api.ts (`ApiFailure`/`describeError`/`ApiRequestError`/
 * `isUnsupportedRuntime`) and extended into the design's structured MCP tool-error contract: a
 * failed control-plane read must reach the agent as a STRUCTURED error carrying the HTTP status
 * and the contract's `ApiError.error` discriminant — never flattened into prose the agent has to
 * parse. `toToolError` renders that structure as an MCP `CallToolResult` with `isError: true`.
 *
 * The taxonomy the control plane speaks (design §9):
 *   403 Unauthorized       — unauthenticated AND unauthorized collapse to 403 (there is no 401)
 *   404 NotFound           — unknown project / workflow / environment / policy / plan
 *   409 LifecycleBindingError — a lifecycle/binding conflict
 *   422 InvalidRequest     — a malformed or invalid request (missing query param, bad body, ...)
 *   501 UnsupportedRuntime — a resolution-dependent read on a runtime this server cannot resolve;
 *                            PURE-YAML reads stay up, only resolution-gated ones degrade
 *   503 TemporalUnavailable — Temporal is unreachable (a 10s-bounded probe)
 */

/** A canonical, agent-stable code for each status in the taxonomy (design §9). */
export const STATUS_CODES: Readonly<Record<number, string>> = {
  403: "Unauthorized",
  404: "NotFound",
  409: "LifecycleBindingError",
  422: "InvalidRequest",
  501: "UnsupportedRuntime",
  503: "TemporalUnavailable",
};

/** A structured control-plane failure: the HTTP status, a human message, and the wire code. */
export interface ApiFailure {
  status: number;
  message: string;
  /** The contract's `ApiError.error` discriminant (#617), e.g. `UnsupportedRuntime`. */
  code?: string;
}

/**
 * Normalize a control-plane error body into a structured {@link ApiFailure}. Prefers the wire
 * `error`/`message` fields (the `{error, message}` envelope the CP emits); falls back to the
 * canonical code for the status, then to a generic message. Never leaks a raw body blob.
 */
export function describeError(status: number, body: unknown): ApiFailure {
  if (body && typeof body === "object") {
    const candidate = body as { error?: string; message?: string; detail?: string };
    const message = candidate.message ?? candidate.detail;
    const code = candidate.error ?? STATUS_CODES[status];
    if (message) {
      return {
        status,
        message: code ? `${code}: ${message}` : message,
        ...(code ? { code } : {}),
      };
    }
  }
  const code = STATUS_CODES[status];
  return {
    status,
    message: code ? `${code}: request failed with status ${status}` : `request failed with status ${status}`,
    ...(code ? { code } : {}),
  };
}

/**
 * A failed control-plane call with the wire taxonomy preserved (console `ApiRequestError`, #621):
 * `status` is the HTTP status and `code` the contract's `ApiError.error` discriminant. Surfaces
 * that must distinguish a by-construction limitation (501 `UnsupportedRuntime`) from a genuine
 * failure branch on these, never by parsing the flattened message.
 */
export class ApiRequestError extends Error {
  readonly status: number;
  readonly code: string | undefined;
  constructor(failure: ApiFailure) {
    super(failure.message);
    this.name = "ApiRequestError";
    this.status = failure.status;
    this.code = failure.code;
  }
}

/**
 * True when the failure is the contract's 501 `UnsupportedRuntime` (console `isUnsupportedRuntime`,
 * #619): the routed project's runtime has no resolver on the serving control plane. Phase-0
 * resolution-dependent tools surface this honestly instead of as a crash; pure-YAML reads never
 * hit it.
 */
export function isUnsupportedRuntime(error: unknown): boolean {
  return (
    error instanceof ApiRequestError && (error.code === "UnsupportedRuntime" || error.status === 501)
  );
}

/** The structured payload an MCP tool returns for a control-plane failure (design §9). */
export interface StructuredToolError {
  error: {
    /** The canonical/wire discriminant (e.g. `UnsupportedRuntime`, `NotFound`). */
    code: string;
    /** The HTTP status the control plane returned. */
    status: number;
    /** The human-readable message (already prefixed with the code by `describeError`). */
    message: string;
  };
}

/** Normalize any thrown error into the structured `{code, status, message}` payload (design §9). */
export function toStructuredError(error: unknown): StructuredToolError {
  if (error instanceof ApiRequestError) {
    return {
      error: {
        code: error.code ?? STATUS_CODES[error.status] ?? "RequestFailed",
        status: error.status,
        message: error.message,
      },
    };
  }
  return {
    error: {
      code: "BackendUnavailable",
      status: 0,
      message: error instanceof Error ? error.message : String(error),
    },
  };
}

/**
 * Render any thrown error as an MCP tool result with `isError: true`, preserving the structured
 * `{status, code, message}` (design §9: never flatten to prose).
 *
 * The structured payload rides in `content` as JSON text and NO `structuredContent` is set: a
 * tool's `outputSchema` describes the SUCCESS shape, so returning a `{error}` structuredContent
 * would violate that schema (MCP clients reject it with -32602). The SDK skips output validation
 * when `isError` is true, and the JSON text is the agent-readable structured error.
 */
export function toToolError(error: unknown): {
  content: { type: "text"; text: string }[];
  isError: true;
} {
  const structured = toStructuredError(error);
  return {
    content: [{ type: "text", text: JSON.stringify(structured.error, null, 2) }],
    isError: true,
  };
}
