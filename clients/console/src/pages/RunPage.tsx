import { useState } from "react";
import { useNavigate } from "@tanstack/react-router";

import type { Capabilities } from "../api";
import {
  ExecutionsPanel,
  RunInspector,
  RunLookupPanel,
  StartExecutionPanel,
} from "../panels/runs";
import { useBundle } from "../queries";

/**
 * Runs & operations (#243, #580): start a resolved workflow version, inspect
 * a run, submit a version-valid review, request cancellation. The inspected
 * execution is URL state (`?run=…`) so a run view is shareable and
 * back/forward walks the inspection history; the panels own their own data
 * and operation state.
 */
export function RunPage({
  workflowId,
  env,
  run,
  capabilities,
  callerIdentity = null,
}: {
  workflowId: string;
  env: string;
  run: string | null;
  capabilities: Capabilities;
  /** The trusted proxy-auth principal from `/meta` (#577 §5) — attributes review decisions. */
  callerIdentity?: string | null;
}) {
  const navigate = useNavigate();
  const bundleState = useBundle(workflowId, env);
  // Re-inspecting the already-addressed run can't navigate (same URL), so a
  // signal tells the inspector to refetch — a manual refresh that keeps
  // in-progress review/cancel form state, unlike a remount.
  const [refreshSignal, setRefreshSignal] = useState(0);

  const inspect = (executionId: string) => {
    if (executionId === run) {
      setRefreshSignal((previous) => previous + 1);
    } else {
      void navigate({
        to: ".",
        search: (previous) => ({ ...previous, run: executionId }),
      });
    }
  };

  return (
    <>
      <div className="page-actions">
        <button
          type="button"
          onClick={() =>
            void navigate({
              to: "/workflows/$workflowId/versions",
              params: { workflowId },
              search: { env },
            })
          }
        >
          Versions &amp; drain
        </button>
      </div>

      <StartExecutionPanel
        workflowId={workflowId}
        env={env}
        bundle={bundleState.data}
        capabilities={capabilities}
        onInspect={inspect}
      />
      <ExecutionsPanel
        workflowId={workflowId}
        env={env}
        bundle={bundleState.data}
        onInspect={inspect}
      />
      <RunLookupPanel addressedRun={run} syncSignal={refreshSignal} onInspect={inspect} />
      {run ? (
        <RunInspector
          // Operator state (reviewer, notes, polling) belongs to one addressed
          // execution: reset when the identity changes, survive refreshes.
          key={`${workflowId}:${env}:${run}`}
          workflowId={workflowId}
          env={env}
          executionId={run}
          refreshSignal={refreshSignal}
          bundle={bundleState.data}
          bundlePending={bundleState.isPending}
          capabilities={capabilities}
          callerIdentity={callerIdentity}
        />
      ) : null}
    </>
  );
}
