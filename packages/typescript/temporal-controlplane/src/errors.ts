/**
 * Control-plane error carrying an HTTP status (governance parity, #563; Python
 * `controlplane/api.py` raises `HTTPException(status_code=…)`). The operations core throws these
 * for caller-facing failures (unknown workflow/environment, unresolvable bundle); the HTTP server
 * slice maps `status` to the response code + the `{error, message}` body.
 *
 * `errorName` is the wire DISCRIMINANT (#617 `ApiError.error`). Python derives it from the raising
 * exception's class name (a config `ProjectProfileError` → `"ProjectProfileError"`), or names the
 * HTTP failure directly (`"NotFound"` for a 404, `"UnsupportedRuntime"` for a 501). The TS core has
 * one error class, so it carries the discriminant explicitly instead of relying on `constructor.name`.
 * Defaults track the Python `StarletteHTTPException` map so a bare 404/422 reads the same as Python's.
 */
export class ProjectControlPlaneError extends Error {
  readonly status: number;
  /** The `ApiError.error` discriminant for the wire envelope (Python's exception class name / HTTP label). */
  readonly errorName: string;
  constructor(message: string, status: number, errorName?: string) {
    super(message);
    this.name = "ProjectControlPlaneError";
    this.status = status;
    this.errorName = errorName ?? defaultErrorName(status);
  }
}

/**
 * The `ApiError.error` label for an HTTP status with no explicit discriminant — the Python
 * `StarletteHTTPException` handler's map (`controlplane/api.py`): 403 Forbidden, 404 NotFound,
 * 422 InvalidRequest, 503 TemporalUnavailable; anything else is a generic `HTTPError`. A config
 * error (422 from a `ProjectProfileError`) overrides this by passing `errorName` explicitly.
 */
export function defaultErrorName(status: number): string {
  switch (status) {
    case 403:
      return "Forbidden";
    case 404:
      return "NotFound";
    case 422:
      return "InvalidRequest";
    case 501:
      // Python names its one 501 path via a dedicated handler, not the Starlette map; the TS
      // default bakes it in so a future 501 site can't silently fall through to `HTTPError`.
      return "UnsupportedRuntime";
    case 503:
      return "TemporalUnavailable";
    default:
      return "HTTPError";
  }
}
