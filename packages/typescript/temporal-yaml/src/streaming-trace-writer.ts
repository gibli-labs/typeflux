/**
 * A `TraceWriter` that STREAMS — shared by the out-of-the-box langfuse and
 * langsmith observers (`buildRuntime` wires one when the spec opts in).
 */

import {
  type ActivityObservation,
  type ObserveActivityParams,
  redactMetadata,
  type RedactionConfig,
  type TraceTransport,
  TraceWriter,
} from "@typeflux/temporal";

/** Workflow-level facts for the grouped PARENT trace/run (Python parity: the
 * workflow span carries the run's input/output and identity metadata). */
export interface WorkflowRunRecord {
  runId: string;
  workflowId: string;
  input?: unknown;
  output?: unknown;
  error?: string;
  metadata?: Record<string, unknown>;
  /**
   * Subject id(s) this run processes (#715 slice 1). The transport sets the
   * trace's native `userId` to the PRIMARY (first) subject and adds a
   * `typeflux.subject:{id}` tag per id — native field and tags always agree.
   * Not PII-redacted: subject ids are stable pseudonymous handles (the erasure
   * key), never the underlying content.
   */
  subjectIds?: readonly string[];
}

/** Implemented by transports whose backend has a run-scoped parent to enrich. */
export interface WorkflowRunRecorder {
  recordWorkflowRun(record: WorkflowRunRecord): Promise<void>;
}

/**
 * A `TraceWriter` that STREAMS: each completed activity schedules a serialized
 * flush, so traces ship as the workflow runs — no drain call at the end of a
 * caller's lifecycle (Python's OTel writer exports continuously too). Flush
 * failures are logged, never thrown into the activity path; `drain()` awaits
 * everything in flight (`buildRuntime` exposes it for graceful exits).
 */
export class StreamingTraceWriter extends TraceWriter {
  private chain: Promise<void> = Promise.resolve();

  constructor(
    private readonly streamTransport: TraceTransport & Partial<WorkflowRunRecorder>,
    private readonly streamRedaction?: RedactionConfig,
  ) {
    super(streamTransport, streamRedaction);
  }

  /**
   * Enrich the grouped parent with workflow-level facts (input at start,
   * output/error at completion) — REDACTED like every other egress
   * (including the failure message, codex P1), and serialized on the same
   * chain as the activity flushes. Fire-and-forget like the streamed
   * flushes: the caller's start/result/error path never blocks on the
   * process-wide flush backlog (finder) — `drain()` awaits the tail.
   */
  recordWorkflowRun(record: WorkflowRunRecord): Promise<void> {
    const recorder = this.streamTransport.recordWorkflowRun?.bind(this.streamTransport);
    if (recorder === undefined) {
      return Promise.resolve();
    }
    const redacted: WorkflowRunRecord =
      this.streamRedaction !== undefined
        ? {
            ...record,
            ...(record.input !== undefined
              ? { input: redactMetadata(record.input, this.streamRedaction) }
              : {}),
            ...(record.output !== undefined
              ? { output: redactMetadata(record.output, this.streamRedaction) }
              : {}),
            ...(record.error !== undefined
              ? { error: String(redactMetadata(record.error, this.streamRedaction)) }
              : {}),
            ...(record.metadata !== undefined
              ? {
                  metadata: redactMetadata(record.metadata, this.streamRedaction) as Record<
                    string,
                    unknown
                  >,
                }
              : {}),
          }
        : record;
    this.chain = this.chain.then(async () => {
      try {
        await recorder(redacted);
      } catch (error) {
        console.error("workflow run trace update failed:", error);
      }
    });
    return Promise.resolve();
  }

  override observeActivity(params: ObserveActivityParams): ActivityObservation {
    const observation = super.observeActivity(params);
    const originalEnd = observation.end.bind(observation);
    observation.end = () => {
      originalEnd();
      this.chain = this.chain.then(async () => {
        try {
          await this.flush();
        } catch (error) {
          // Tracing is observability, not control flow: an unexpected THROW
          // from the transport/SDK must not fail activities — the trace stays
          // re-buffered (TraceWriter re-buffers on throw) for the next
          // flush/drain. (A plain Langfuse outage never reaches here: the SDK
          // retries then logs-and-drops, see LangfuseSdkTransport.)
          console.error("langfuse trace flush failed:", error);
        }
      });
    };
    return observation;
  }

  /** Await every scheduled flush, then flush once more (re-buffered stragglers). */
  async drain(): Promise<void> {
    await this.chain;
    await this.flush();
  }
}

