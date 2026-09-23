import { useEffect } from "react";
import { useNavigate } from "@tanstack/react-router";
import { isUnsupportedRuntime } from "../api";

import { ErrorPanel, InsightList, Loading, ResolutionUnavailable, Section } from "../components";
import { deriveBundleInsights } from "../insights";
import {
  ActivitiesPanel,
  SubWorkflowsPanel,
  CodeProvenanceBanner,
  ComponentsPanel,
  ConnectionsPanel,
  DeploymentPanel,
  IdentityPanel,
  LifecyclePanel,
  PolicyPanel,
  PromptRegistryPanel,
  RuntimePanel,
  SecretsPanel,
  TopologyPanel,
  ValidationPanel,
} from "../panels/workflow";
import { errorMessage, useBundle, useCatalog } from "../queries";

export function WorkflowPage({
  workflowId,
  env,
  section,
}: {
  workflowId: string;
  env: string;
  section: string | null;
}) {
  const navigate = useNavigate();
  const bundleState = useBundle(workflowId, env);
  // A missing catalog degrades to "catalog unavailable" per activity, exactly
  // as before — the page gates on both so the table renders once, complete.
  const catalogState = useCatalog(workflowId, env);
  const loading = bundleState.isPending || catalogState.isPending;

  useEffect(() => {
    if (section && !loading) {
      document.getElementById(section)?.scrollIntoView({ behavior: "smooth" });
    }
  }, [section, loading]);

  if (loading) return <Loading what={`bundle for ${workflowId}`} />;
  if (bundleState.isError || !bundleState.data) {
    if (isUnsupportedRuntime(bundleState.error)) {
      return <ResolutionUnavailable what={`The resolved bundle for ${workflowId}`} />;
    }
    return <ErrorPanel message={errorMessage(bundleState.error) ?? "no data"} />;
  }
  const bundle = bundleState.data;
  const catalog = catalogState.data;
  const insights = deriveBundleInsights(workflowId, env, bundle);

  return (
    <>
      <div className="page-actions">
        <button
          type="button"
          onClick={() =>
            void navigate({
              to: "/workflows/$workflowId/diff",
              params: { workflowId },
              search: { left: env },
            })
          }
        >
          Compare environments
        </button>
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
        <button
          type="button"
          onClick={() =>
            void navigate({
              to: "/workflows/$workflowId/runs",
              params: { workflowId },
              search: { env },
            })
          }
        >
          Runs &amp; operations
        </button>
      </div>

      <CodeProvenanceBanner bundle={bundle} />

      <Section id="insights" title="Insights">
        <div className="panel">
          <InsightList insights={insights} />
        </div>
      </Section>

      <IdentityPanel bundle={bundle} />
      <TopologyPanel bundle={bundle} env={env} />
      <ActivitiesPanel bundle={bundle} catalog={catalog} />
      <SubWorkflowsPanel bundle={bundle} env={env} />
      <LifecyclePanel bundle={bundle} />
      <PolicyPanel bundle={bundle} />
      <ComponentsPanel bundle={bundle} />
      <SecretsPanel bundle={bundle} />
      <ConnectionsPanel workflowId={workflowId} env={env} bundle={bundle} />
      <PromptRegistryPanel workflowId={workflowId} env={env} bundle={bundle} />
      <RuntimePanel bundle={bundle} />
      <ValidationPanel bundle={bundle} />
      <DeploymentPanel bundle={bundle} />
    </>
  );
}
