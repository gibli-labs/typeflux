/**
 * The shared deadline-bound JSON fetch wrapper (#727 F7): the common core the langfuse (#573) and
 * GitHub (#727) reader transports both need — a single GET carrying a per-request deadline
 * (`AbortSignal.timeout`, so a stalled host degrades instead of hanging the route), a non-2xx gate,
 * and JSON parsing. The two transports classify a failed response DIFFERENTLY (langfuse throws one
 * named error; GitHub separates a rate-limit signal from a routine 404/422), so the status
 * classification is delegated to a caller-supplied {@link DeadlineFetchOptions.onErrorResponse}
 * callback that throws the transport's own typed error — the wrapper owns the timeout + the non-ok
 * gate + `.json()`, never the vendor-specific error taxonomy.
 */

/** One deadline-bound JSON GET (see {@link fetchJson}). */
export interface DeadlineFetchOptions {
  /** The `fetch` implementation (the global, or a unit-test injected stub). */
  fetch: typeof fetch;
  /** The fully-qualified request URL. */
  url: string;
  /** Request headers (auth, accept, etc.) — transport-specific. */
  headers: Record<string, string>;
  /** Per-request deadline in ms — a stalled host aborts and rejects so the caller degrades. */
  timeoutMs: number;
  /**
   * Called on a NON-2xx response to throw the transport's typed error (a rate-limit class, an
   * HTTP-status carrier, a named request error). MUST throw — its `never` return lets the wrapper's
   * `return response.json()` narrow to the ok path.
   */
  onErrorResponse(response: Response): never;
}

/**
 * Perform one deadline-bound GET and parse its JSON body. On a non-2xx response the transport's
 * {@link DeadlineFetchOptions.onErrorResponse} classifier throws; otherwise the parsed JSON is
 * returned as `unknown` (each transport defensively narrows the payload it recognizes).
 */
export async function fetchJson(options: DeadlineFetchOptions): Promise<unknown> {
  const response = await options.fetch(options.url, {
    headers: options.headers,
    signal: AbortSignal.timeout(options.timeoutMs),
  });
  if (!response.ok) options.onErrorResponse(response);
  return response.json();
}
