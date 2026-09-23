/**
 * Route tree (#578): code-based TanStack Router over hash history, so every
 * pre-existing `#/…` URL keeps resolving. Routes own params/search parsing and
 * wire shell data into the pages — pages stay presentational and prop-driven.
 */

import {
  createHashHistory,
  createRootRoute,
  createRoute,
  createRouter,
  useParams,
  useSearch,
} from "@tanstack/react-router";

import { Shell, switchProject } from "./shell/Shell";
import { useEnv } from "./shell/env";
import { useShellData } from "./queries";
import { DefinitionPage } from "./pages/DefinitionPage";
import { DeploymentsPage } from "./pages/DeploymentsPage";
import { DiffPage } from "./pages/DiffPage";
import { activeProject } from "./api";
import { DriftPage } from "./pages/DriftPage";
import { EnvironmentPage } from "./pages/EnvironmentPage";
import { GovernancePage } from "./pages/GovernancePage";
import { OverviewPage } from "./pages/OverviewPage";
import {
  PersonaExecutiveView,
  PersonaGovernanceView,
  PersonaOperationsView,
  PersonaSecurityView,
} from "./pages/personas";
import { RunDiffPage } from "./pages/RunDiffPage";
import { RunPage } from "./pages/RunPage";
import { RunsPage } from "./pages/RunsPage";
import { VersionsPage } from "./pages/VersionsPage";
import { WorkflowPage } from "./pages/WorkflowPage";

function optional(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

const rootRoute = createRootRoute({
  component: Shell,
  // Unknown routes render the Overview, matching the old dispatch fall-through.
  notFoundComponent: OverviewRoute,
  validateSearch: (search: Record<string, unknown>): { env?: string } => ({
    env: optional(search.env),
  }),
});

function OverviewRoute() {
  const shell = useShellData();
  const env = useEnv();
  if (!shell.data) return null; // the Shell gates loading/error before the outlet
  return (
    <OverviewPage
      env={env}
      workflows={shell.data.workflows}
      environments={shell.data.environments}
      projects={shell.data.projects}
      onSwitchProject={(projectId) => switchProject(shell.data.projects, projectId)}
      canRefreshProject={shell.data.meta.capabilities.can_refresh_project}
      project={activeProject(shell.data.projects)}
      capabilities={shell.data.meta.capabilities}
    />
  );
}

const overviewRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: OverviewRoute,
});

function DriftRoute() {
  const shell = useShellData();
  const env = useEnv();
  if (!shell.data) return null;
  return (
    <DriftPage
      env={env}
      workflows={shell.data.workflows}
      environments={shell.data.environments}
      project={activeProject(shell.data.projects)}
      capabilities={shell.data.meta.capabilities}
    />
  );
}

const driftRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/drift",
  component: DriftRoute,
});

function RunsRoute() {
  const shell = useShellData();
  const env = useEnv();
  if (!shell.data) return null;
  return <RunsPage env={env} workflows={shell.data.workflows} />;
}

const runsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/runs",
  component: RunsRoute,
});

function GovernanceRoute() {
  const shell = useShellData();
  const env = useEnv();
  if (!shell.data) return null;
  return (
    <GovernancePage
      env={env}
      workflows={shell.data.workflows}
      environments={shell.data.environments}
      policies={shell.data.policies}
      capabilities={shell.data.meta.capabilities}
    />
  );
}

const governanceRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/governance",
  component: GovernanceRoute,
});

// Persona landing views (#721): four read-only role surfaces under one nav group, each a
// shareable hash URL, composed entirely from the cached read tier.
function PersonaGovernanceRoute() {
  const shell = useShellData();
  const env = useEnv();
  if (!shell.data) return null;
  return (
    <PersonaGovernanceView
      env={env}
      workflows={shell.data.workflows}
      environments={shell.data.environments}
      policies={shell.data.policies}
      project={activeProject(shell.data.projects)}
      capabilities={shell.data.meta.capabilities}
    />
  );
}

const personaGovernanceRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/personas/governance",
  component: PersonaGovernanceRoute,
});

function PersonaSecurityRoute() {
  const shell = useShellData();
  const env = useEnv();
  if (!shell.data) return null;
  return (
    <PersonaSecurityView
      env={env}
      workflows={shell.data.workflows}
      environments={shell.data.environments}
      policies={shell.data.policies}
      project={activeProject(shell.data.projects)}
    />
  );
}

const personaSecurityRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/personas/security",
  component: PersonaSecurityRoute,
});

function PersonaOperationsRoute() {
  const shell = useShellData();
  const env = useEnv();
  if (!shell.data) return null;
  return <PersonaOperationsView env={env} workflows={shell.data.workflows} />;
}

const personaOperationsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/personas/operations",
  component: PersonaOperationsRoute,
});

function PersonaExecutiveRoute() {
  const shell = useShellData();
  const env = useEnv();
  if (!shell.data) return null;
  return (
    <PersonaExecutiveView
      env={env}
      workflows={shell.data.workflows}
      environments={shell.data.environments}
      policies={shell.data.policies}
    />
  );
}

const personaExecutiveRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/personas/executive",
  component: PersonaExecutiveRoute,
});

function DeploymentsRoute() {
  const shell = useShellData();
  if (!shell.data) return null;
  return (
    <DeploymentsPage
      workflowIds={shell.data.workflows.map((w) => w.id)}
      environmentIds={shell.data.environments.map((e) => e.id)}
      policyIds={shell.data.policies.map((p) => p.id)}
      project={activeProject(shell.data.projects)}
      manifestPath={shell.data.meta.manifest_path ?? "typeflux.project.yaml"}
      capabilities={shell.data.meta.capabilities}
    />
  );
}

const deploymentsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/deployments",
  component: DeploymentsRoute,
});

function WorkflowRoute() {
  const { workflowId } = useParams({ from: workflowRoute.id });
  const { section } = useSearch({ from: workflowRoute.id });
  const env = useEnv();
  return <WorkflowPage workflowId={workflowId} env={env} section={section ?? null} />;
}

const workflowRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/workflows/$workflowId",
  component: WorkflowRoute,
  validateSearch: (search: Record<string, unknown>): { section?: string } => ({
    section: optional(search.section),
  }),
});

function WorkflowDiffRoute() {
  const { workflowId } = useParams({ from: workflowDiffRoute.id });
  const search = useSearch({ from: workflowDiffRoute.id });
  const shell = useShellData();
  const env = useEnv();
  if (!shell.data) return null;
  const environments = shell.data.environments;
  const left = search.left ?? env;
  const right = search.right ?? environments.find((e) => e.id !== left)?.id ?? left;
  return <DiffPage workflowId={workflowId} environments={environments} left={left} right={right} />;
}

const workflowDiffRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/workflows/$workflowId/diff",
  component: WorkflowDiffRoute,
  validateSearch: (search: Record<string, unknown>): { left?: string; right?: string } => ({
    left: optional(search.left),
    right: optional(search.right),
  }),
});

function WorkflowVersionsRoute() {
  const { workflowId } = useParams({ from: workflowVersionsRoute.id });
  const env = useEnv();
  return <VersionsPage workflowId={workflowId} env={env} />;
}

const workflowVersionsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/workflows/$workflowId/versions",
  component: WorkflowVersionsRoute,
});

function WorkflowRunDiffRoute() {
  const { workflowId } = useParams({ from: workflowRunDiffRoute.id });
  const search = useSearch({ from: workflowRunDiffRoute.id });
  const env = useEnv();
  return (
    <RunDiffPage
      workflowId={workflowId}
      env={env}
      left={search.left ?? null}
      right={search.right ?? null}
    />
  );
}

const workflowRunDiffRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/workflows/$workflowId/run-diff",
  component: WorkflowRunDiffRoute,
  validateSearch: (search: Record<string, unknown>): { left?: string; right?: string } => ({
    left: optional(search.left),
    right: optional(search.right),
  }),
});

function WorkflowRunsRoute() {
  const { workflowId } = useParams({ from: workflowRunsRoute.id });
  const { run } = useSearch({ from: workflowRunsRoute.id });
  const shell = useShellData();
  const env = useEnv();
  if (!shell.data) return null;
  return (
    <RunPage
      workflowId={workflowId}
      env={env}
      run={run ?? null}
      capabilities={shell.data.meta.capabilities}
      callerIdentity={shell.data.meta.caller_identity ?? null}
    />
  );
}

const workflowRunsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/workflows/$workflowId/runs",
  component: WorkflowRunsRoute,
  // The inspected run is URL state (#580): shareable, restored on cold load,
  // and back/forward walks the inspection history.
  validateSearch: (search: Record<string, unknown>): { run?: string } => ({
    run: optional(search.run),
  }),
});

function EnvironmentRoute() {
  const { environmentId } = useParams({ from: environmentRoute.id });
  const shell = useShellData();
  if (!shell.data) return null;
  return (
    <EnvironmentPage
      environmentId={environmentId}
      workflows={shell.data.workflows}
      project={activeProject(shell.data.projects)}
      sourcePath={shell.data.environments.find((environment) => environment.id === environmentId)?.path}
    />
  );
}

const environmentRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/environments/$environmentId",
  component: EnvironmentRoute,
});

function PolicyRoute() {
  const { policyId } = useParams({ from: policyRoute.id });
  const shell = useShellData();
  const env = useEnv();
  return (
    <DefinitionPage
      kind="policy"
      id={policyId}
      env={env}
      project={activeProject(shell.data?.projects ?? [])}
      sourcePath={shell.data?.policies.find((policy) => policy.id === policyId)?.path}
    />
  );
}

const policyRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/policies/$policyId",
  component: PolicyRoute,
});

function ProfileRoute() {
  const { kind, profileId } = useParams({ from: profileRoute.id });
  const shell = useShellData();
  const env = useEnv();
  return <DefinitionPage kind="profile" profileKind={kind} id={profileId} env={env} project={activeProject(shell.data?.projects ?? [])} />;
}

const profileRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/profiles/$kind/$profileId",
  component: ProfileRoute,
});

const routeTree = rootRoute.addChildren([
  overviewRoute,
  driftRoute,
  runsRoute,
  governanceRoute,
  personaGovernanceRoute,
  personaSecurityRoute,
  personaOperationsRoute,
  personaExecutiveRoute,
  deploymentsRoute,
  workflowRoute,
  workflowDiffRoute,
  workflowVersionsRoute,
  workflowRunDiffRoute,
  workflowRunsRoute,
  environmentRoute,
  policyRoute,
  profileRoute,
]);

export const router = createRouter({
  routeTree,
  history: createHashHistory(),
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
