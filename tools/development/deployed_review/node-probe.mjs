import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { Client, Connection } from "@temporalio/client";
import { Worker } from "@temporalio/worker";
import proto from "@temporalio/proto";

const [action, executionId, runId] = process.argv.slice(2);
const connection = await Connection.connect({address: "temporal:7233"});
try {
  const client = new Client({connection, namespace: "default"});
  const handle = client.workflow.getHandle(executionId, runId === "current" ? undefined : runId);
  const description = await handle.describe();
  const history = await handle.fetchHistory();
  const eventCounts = {};
  for (const event of history.events ?? []) {
    const key = proto.temporal.api.enums.v1.EventType[event.eventType];
    eventCounts[key] = (eventCounts[key] ?? 0) + 1;
  }
  const record = {runId: description.runId, status: description.status.name,
    eventCounts, historyEvents: history.events?.length ?? 0};
  if (action === "replay") {
    const require = createRequire(import.meta.url);
    const workflowsPath = join(dirname(require.resolve("@typeflux/temporal-yaml")), "workflows.js");
    await Worker.runReplayHistory({workflowsPath}, history, executionId);
    record.replayPassed = true;
  }
  if (description.status.name === "COMPLETED") {
    const result = await handle.result();
    if (JSON.stringify(result) !== JSON.stringify({value: "1"})) throw new Error("unexpected validated candidate result");
    record.validatedResult = result;
  }
  console.log(JSON.stringify(record));
} finally { await connection.close(); }
