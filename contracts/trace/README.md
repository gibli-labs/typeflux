# Trace contract (#390)

The trace record + observation shape read back from an observability backend
(e.g. Langfuse). Frozen as part of the cross-SDK contract bundle (see
`../CONTRACT_VERSION`).

Golden: [`golden/trace_record.json`](golden/trace_record.json) — a `TraceRecord`
with one `GENERATION` `ObservationRecord` (generated from the Python baseline,
`model_dump(mode="json", exclude_none=True)`).

- **TraceRecord**: `trace_id` (required), `name`, `timestamp`, `input`,
  `output`, `metadata`, `observations[]`, `retrieval`.
- **ObservationRecord**: `observation_id`, `name`, `type`, `level`, `input`,
  `output`, `metadata`, `start_time`, `end_time`.
- **TraceRetrievalInfo** (`retrieval`): backend pagination/coverage info.

Timestamps are ISO-8601 UTC. The provider-native `raw` payload is excluded from
the contract (it is backend-specific, not part of the frozen shape).
