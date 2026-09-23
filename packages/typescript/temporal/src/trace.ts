/**
 * Trace contract (#390): the trace record + observations read back from an
 * observability backend, mirroring the Python `TraceRecord` / `ObservationRecord`
 * / `TraceRetrievalInfo`.
 *
 * `serializeTraceRecord` reproduces Python's `model_dump(exclude_none=True)`
 * for already-JSON inputs. `exclude_none` is **field-level**: a `null` model
 * field is dropped, but free-form `input`/`output`/`metadata` values are emitted
 * verbatim (a nested `null` inside them is kept). Those free-form values are
 * assumed JSON-ready — unlike Python's `mode="json"`, this does NOT recurse to
 * coerce rich types (a TS consumer builds the record from a backend's JSON, so
 * timestamps are already ISO-8601 strings). Empty collections survive
 * (`warnings: []`); the backend-specific `raw` payload is excluded.
 */

export interface ObservationRecord {
  observation_id?: string;
  name?: string;
  type?: string;
  level?: string;
  input?: unknown;
  output?: unknown;
  metadata?: Record<string, unknown>;
  start_time?: string;
  end_time?: string;
}

export interface TraceRetrievalInfo {
  backend: string;
  complete: boolean;
  pages_read: number;
  observations_read: number;
  page_size: number;
  max_pages: number;
  next_cursor?: string;
  warnings?: string[];
}

export interface TraceRecord {
  trace_id: string;
  name?: string;
  timestamp?: string;
  input?: unknown;
  output?: unknown;
  metadata?: Record<string, unknown>;
  observations?: ObservationRecord[];
  retrieval?: TraceRetrievalInfo;
}

/** Drop entries whose value is `null`/`undefined` (the field itself); values
 * that are present are emitted verbatim, never recursed into. */
function dropNullFields(fields: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(fields)) {
    if (value !== null && value !== undefined) {
      out[key] = value;
    }
  }
  return out;
}

function serializeObservation(obs: ObservationRecord): Record<string, unknown> {
  return dropNullFields({
    observation_id: obs.observation_id,
    name: obs.name,
    type: obs.type,
    level: obs.level,
    input: obs.input,
    output: obs.output,
    metadata: obs.metadata ?? {},
    start_time: obs.start_time,
    end_time: obs.end_time,
  });
}

function serializeRetrieval(info: TraceRetrievalInfo): Record<string, unknown> {
  return dropNullFields({
    backend: info.backend,
    complete: info.complete,
    pages_read: info.pages_read,
    observations_read: info.observations_read,
    page_size: info.page_size,
    max_pages: info.max_pages,
    next_cursor: info.next_cursor,
    warnings: info.warnings ?? [],
  });
}

/** Serialize a `TraceRecord` to its wire contract (`model_dump` exclude_none). */
export function serializeTraceRecord(record: TraceRecord): Record<string, unknown> {
  return dropNullFields({
    trace_id: record.trace_id,
    name: record.name,
    timestamp: record.timestamp,
    input: record.input,
    output: record.output,
    metadata: record.metadata ?? {},
    observations: (record.observations ?? []).map(serializeObservation),
    retrieval: record.retrieval ? serializeRetrieval(record.retrieval) : undefined,
  });
}
