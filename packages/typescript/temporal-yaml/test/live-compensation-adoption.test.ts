import {execFile} from "node:child_process";
import {promisify} from "node:util";
import {fileURLToPath} from "node:url";
import {describe, expect, it} from "vitest";

describe.skipIf(process.env["TYPEFLUX_LIVE_TEMPORAL"] !== "1")("documented compensation adoption", () => {
  it("observes complete and partial compensation and same-run worker replacement", async () => {
    const environment = {...process.env, TEMPORAL_ADDRESS: "localhost:7233", TEMPORAL_NAMESPACE: "default"};
    const script = fileURLToPath(new URL("../examples/compensation-saga/adoption-demo.mjs", import.meta.url));
    const {stdout} = await promisify(execFile)(process.execPath, [script], {env: environment, timeout: 120000, killSignal: "SIGKILL"});
    const observations = JSON.parse(stdout);
    expect(observations).toHaveLength(3);
    for (const [index, scenario] of ["complete", "partial", "replacement"].entries()) {
      const actions = [["book_room", 1], ["charge_card", 1], ["fulfill_order", 1], ["refund_card", 1],
        ...(scenario === "replacement" ? [["refund_card", 2]] : []), ["cancel_room", 1]];
      expect(observations[index]).toEqual({scenario, sameRun: true, originalFailure: true,
        compensationStatus: scenario === "partial" ? "partial" : "complete", actions,
        forwardFakeWrites: {book_room: 1, charge_card: 1}, retryRecordedBeforeStop: scenario === "replacement"});
    }
  }, 125000);
});
