/**
 * Application shell (#578): sidebar and topbar — the layout that used to live
 * in `App.tsx`, rendered by the root route. Owns the cross-cutting
 * scope (active project, selected environment, theme, API token); pages render
 * inside the outlet and stay presentational.
 */

import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Link, Outlet, useLocation, useMatches, useNavigate } from "@tanstack/react-router";

import type { ProjectSummary } from "../api";
import { apiBase, currentApiToken, currentProject, setApiToken, setCurrentProject } from "../api";
import { ErrorPanel, Loading } from "../components";
import { PERSONA_LINKS } from "../pages/personas";
import { errorMessage, useShellData } from "../queries";
import { useEnv } from "./env";

type Theme = "dark" | "light";

function initialTheme(): Theme {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function BrandIcon() {
  return (
    <svg className="brand-icon" viewBox="0 0 22 22" aria-hidden="true">
      <rect width="22" height="22" rx="6" fill="currentColor" />
      <g fill="#fff">
        <path
          transform="translate(8.83545 15.5) matrix(.006347656 0 0 -.006347656 0 0)"
          d="M420-18q-124 0-191 67.5T162 254v638H25v190h151l88 254h176v-254h205V892H440V330q0-79 30-116.5t93-37.5q26 0 47 4t47 10V16Q605-1 547-9.5T420-18Z"
        />
        <path
          transform="translate(12.32617 17.5) matrix(.005859375 0 0 -.005859375 0 0)"
          d="M993 1247q-9 3-22.5 6t-27.5 5.5t-27.5 4t-24.5 1.5q-65 0-93.5-33T757 1137l-20-107h212l-36-190H700L514-100q-38-192-122-258.5T177-425q-68 0-127 22l39 196q31-12 74-12q35 0 74 119l182 940H261l37 190h158l27 135q11 62 35.5 114.5T586 1371t108 61t157 22q23 0 48.5-2.5t49.5-6.5t46.5-9.5t37.5-11.5Z"
        />
      </g>
    </svg>
  );
}

type PrimaryNavIcon = "overview" | "drift" | "runs" | "governance" | "deployments";

function NavIcon({ icon }: { icon: PrimaryNavIcon }) {
  const paths: Record<PrimaryNavIcon, ReactNode> = {
    overview: (
      <>
        <rect x="3" y="3" width="6" height="6" rx="1.5" />
        <rect x="13" y="3" width="6" height="6" rx="1.5" />
        <rect x="3" y="13" width="6" height="6" rx="1.5" />
        <rect x="13" y="13" width="6" height="6" rx="1.5" />
      </>
    ),
    drift: <path d="M4 6h5l2 3 3-5 4 2M4 16h5l2-3 3 5 4-2" />,
    runs: (
      <>
        <path d="M5 4h9a4 4 0 0 1 4 4v6a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4V7" />
        <path d="m9 8 5 3-5 3Z" />
      </>
    ),
    governance: <path d="M11 3 18 6v5c0 4.4-2.7 7-7 8-4.3-1-7-3.6-7-8V6l7-3Zm-3 8 2 2 4-4" />,
    deployments: (
      <>
        <path d="m4 7 7-4 7 4-7 4-7-4Z" />
        <path d="m4 11 7 4 7-4M4 15l7 4 7-4" />
      </>
    ),
  };
  return (
    <svg className="nav-icon" viewBox="0 0 22 22" aria-hidden="true">
      <g fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        {paths[icon]}
      </g>
    </svg>
  );
}

function ThemeIcon({ theme }: { theme: Theme }) {
  return theme === "dark" ? (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <circle cx="10" cy="10" r="3.25" />
      <path d="M10 2v2M10 16v2M2 10h2M16 10h2M4.35 4.35l1.4 1.4M14.25 14.25l1.4 1.4M15.65 4.35l-1.4 1.4M5.75 14.25l-1.4 1.4" />
    </svg>
  ) : (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <path d="M16.6 12.45A7 7 0 0 1 7.55 3.4 7 7 0 1 0 16.6 12.45Z" />
    </svg>
  );
}

function MenuIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <path d="M3 6h14M3 10h14M3 14h14" />
    </svg>
  );
}

/**
 * Switching project changes the base of every API call, so reset the route
 * and reload — every page (and the query cache) restarts cleanly against the
 * new project.
 */
export function switchProject(projects: ProjectSummary[], projectId: string): void {
  const isDefault = projects.find((p) => p.id === projectId)?.default ?? false;
  setCurrentProject(isDefault ? null : projectId);
  window.location.hash = "#/";
  window.location.reload();
}

/**
 * The topbar crumb, derived from the *matched* leaf route (never from raw
 * pathname parsing): an unmatched URL falls to the root's not-found Overview
 * body, and the title must agree with what actually rendered.
 */
function titleFor(routeId: string, params: Record<string, string | undefined>): ReactNode {
  const scoped = (id: string | undefined, label: string) => (
    <>
      <span className="mono">{id}</span> <span className="crumb">/ {label}</span>
    </>
  );
  switch (routeId) {
    case "/drift":
      return <span className="crumb">drift</span>;
    case "/runs":
      return <span className="crumb">runs</span>;
    case "/governance":
      return <span className="crumb">governance</span>;
    case "/personas/governance":
      return <span className="crumb">personas / governance</span>;
    case "/personas/security":
      return <span className="crumb">personas / security posture</span>;
    case "/personas/operations":
      return <span className="crumb">personas / operations</span>;
    case "/personas/executive":
      return <span className="crumb">personas / executive</span>;
    case "/deployments":
      return <span className="crumb">deployment plans</span>;
    case "/environments/$environmentId":
      return scoped(params.environmentId, "environment definition");
    case "/policies/$policyId":
      return scoped(params.policyId, "policy definition");
    case "/profiles/$kind/$profileId":
      return scoped(params.profileId, `${params.kind} profile`);
    case "/workflows/$workflowId":
      return <span className="mono">{params.workflowId}</span>;
    case "/workflows/$workflowId/diff":
      return scoped(params.workflowId, "environment diff");
    case "/workflows/$workflowId/versions":
      return scoped(params.workflowId, "versions & drain");
    case "/workflows/$workflowId/run-diff":
      return scoped(params.workflowId, "run diff");
    case "/workflows/$workflowId/runs":
      return scoped(params.workflowId, "runs & operations");
    default:
      return "Overview";
  }
}

export function Shell() {
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const hasToken = currentApiToken() != null;
  const shell = useShellData();
  const env = useEnv();
  const navigate = useNavigate();
  const location = useLocation();
  const segments = location.pathname.split("/").filter(Boolean);
  const activeWorkflow = segments[0] === "workflows" ? segments[1] : null;
  const matches = useMatches();
  const leaf = matches[matches.length - 1];
  const title = titleFor(
    leaf?.routeId ?? "",
    (leaf?.params ?? {}) as Record<string, string | undefined>,
  );

  function editApiToken(): void {
    // Prompt with an empty field (never prefill the existing token, so it is
    // not shown back); blank clears it.
    const next = window.prompt(
      "API bearer token for a token-protected control plane (leave blank to clear):",
      "",
    );
    if (next === null) return; // cancelled
    setApiToken(next.trim() || null);
    // Re-fetch everything with the new token — mirrors project switching, so the
    // console recovers from an auth error (and refreshes capability flags)
    // without a manual reload (#323).
    window.location.reload();
  }

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document
      .querySelector<HTMLMetaElement>('meta[name="theme-color"]')
      ?.setAttribute("content", theme === "dark" ? "#101012" : "#fafafa");
    localStorage.setItem("typeflux.theme", theme);
  }, [theme]);

  useEffect(() => {
    setSidebarOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    if (!sidebarOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSidebarOpen(false);
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [sidebarOpen]);

  let body: ReactNode;
  if (shell.isPending) {
    body = <Loading what="project" />;
  } else if (shell.isError) {
    body = (
      <ErrorPanel
        message={errorMessage(shell.error) ?? "no data"}
        hint="Start the API with: python -m typeflux.controlplane serve <typeflux.project.yaml> — the dev console proxies /api to 127.0.0.1:8400."
      />
    );
  } else {
    body = <Outlet />;
  }

  const projects = shell.data?.projects ?? [];
  const controlPlaneState = shell.isPending
    ? "connecting"
    : shell.isError
      ? "unavailable"
      : "connected";
  const controlPlaneDot = shell.isPending
    ? "status-dot status-dot-pending"
    : shell.isError
      ? "status-dot status-dot-error"
      : "status-dot";

  return (
    <div className="app">
      <div className="app-body">
        <aside className={`sidebar ${sidebarOpen ? "open" : ""}`} id="app-sidebar">
          <div className="brand">
            <Link className="brand-lockup" to="/" search={{ env }} aria-label="Typeflux Console overview">
              <BrandIcon />
              <span className="brand-word">typeflux<span>.</span></span>
              <span className="brand-product">Console</span>
            </Link>
            {projects.length > 1 ? (
              <select
                className="project-switcher"
                value={
                  currentProject() ??
                  projects.find((p) => p.default)?.id ??
                  projects[0]?.id ??
                  ""
                }
                onChange={(e) => switchProject(projects, e.target.value)}
                title="Switch project"
              >
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                    {project.default ? " (default)" : ""}
                    {project.resolvable ? "" : " — not resolvable here"}
                    {project.available === false ? " — unavailable" : ""}
                  </option>
                ))}
              </select>
            ) : (
              <div className="brand-project">{shell.data?.meta.project ?? "…"}</div>
            )}
          </div>
          <nav className="nav" aria-label="Console navigation">
            <Link
              className={`nav-item ${segments.length === 0 ? "active" : ""}`}
              to="/"
              search={{ env }}
            >
              <NavIcon icon="overview" />
              <span>Overview</span>
            </Link>
            <Link
              className={`nav-item ${segments[0] === "drift" ? "active" : ""}`}
              to="/drift"
              search={{ env }}
            >
              <NavIcon icon="drift" />
              <span>Drift</span>
            </Link>
            <Link
              className={`nav-item ${segments[0] === "runs" ? "active" : ""}`}
              to="/runs"
              search={{ env }}
            >
              <NavIcon icon="runs" />
              <span>Runs</span>
            </Link>
            <Link
              className={`nav-item ${segments[0] === "governance" ? "active" : ""}`}
              to="/governance"
              search={{ env }}
            >
              <NavIcon icon="governance" />
              <span>Governance</span>
            </Link>
            <Link
              className={`nav-item ${segments[0] === "deployments" ? "active" : ""}`}
              to="/deployments"
              search={{ env }}
            >
              <NavIcon icon="deployments" />
              <span>Deployments</span>
            </Link>
            <div className="nav-label">Personas</div>
            {PERSONA_LINKS.map(({ to, slug, label }) => (
              <Link
                key={to}
                className={`nav-item ${segments[0] === "personas" && segments[1] === slug ? "active" : ""}`}
                to={to}
                search={{ env }}
              >
                {label}
              </Link>
            ))}
            <div className="nav-label">Workflows</div>
            {(shell.data?.workflows ?? []).map((workflow) => (
              <Link
                key={workflow.id}
                className={`nav-item ${activeWorkflow === workflow.id ? "active" : ""}`}
                to="/workflows/$workflowId"
                params={{ workflowId: workflow.id }}
                search={{ env }}
              >
                <span className="mono">{workflow.id}</span>
              </Link>
            ))}
            <div className="nav-label">Environments</div>
            {(shell.data?.environments ?? []).map((environment) => (
              <Link
                key={environment.id}
                className={`nav-item ${segments[0] === "environments" && segments[1] === environment.id ? "active" : ""}`}
                to="/environments/$environmentId"
                params={{ environmentId: environment.id }}
                search={{ env }}
              >
                <span className="mono">{environment.id}</span>
              </Link>
            ))}
            {(shell.data?.policies ?? []).length > 0 ? (
              <div className="nav-label">Policies</div>
            ) : null}
            {(shell.data?.policies ?? []).map((policy) => (
              <Link
                key={policy.id}
                className={`nav-item ${segments[0] === "policies" && segments[1] === policy.id ? "active" : ""}`}
                to="/policies/$policyId"
                params={{ policyId: policy.id }}
                search={{ env }}
              >
                <span className="mono">{policy.id}</span>
              </Link>
            ))}
            {(shell.data?.profiles ?? []).length > 0 ? (
              <div className="nav-label">Profiles</div>
            ) : null}
            {(shell.data?.profiles ?? []).map((profile) => (
              <Link
                key={`${profile.kind}:${profile.id}`}
                className={`nav-item ${segments[0] === "profiles" && segments[1] === profile.kind && segments[2] === profile.id ? "active" : ""}`}
                to="/profiles/$kind/$profileId"
                params={{ kind: profile.kind, profileId: profile.id }}
                search={{ env }}
              >
                <span className="mono">{profile.id}</span>{" "}
                <span className="faint">{profile.kind}</span>
              </Link>
            ))}
          </nav>
          <div className="sidebar-system" aria-label="Control plane status">
            <div className="system-state">
              <span className={controlPlaneDot} />
              <span>Control plane</span>
              <span className="system-state-label">{controlPlaneState}</span>
            </div>
            <div className="system-meta">
              API {shell.data?.meta.api_version ?? "?"} · bundle {shell.data?.meta.bundle_version ?? "?"}
              {" · "}catalog {shell.data?.meta.catalog_version ?? "?"}
            </div>
            <div className="system-origin" title={apiBase || "same-origin"}>{apiBase || "same-origin"}</div>
          </div>
        </aside>
        <button
          type="button"
          className={`sidebar-scrim ${sidebarOpen ? "visible" : ""}`}
          aria-label="Close navigation"
          onClick={() => setSidebarOpen(false)}
          tabIndex={sidebarOpen ? 0 : -1}
        />
        <div className="main">
          <header className="topbar">
            <button
              type="button"
              className="icon-button menu-button"
              aria-label="Open navigation"
              aria-controls="app-sidebar"
              aria-expanded={sidebarOpen}
              onClick={() => setSidebarOpen(true)}
            >
              <MenuIcon />
            </button>
            <div className="topbar-title">
              <span className="topbar-kicker">Control plane</span>
              <h1>{title}</h1>
            </div>
            <div className="topbar-actions">
              <label className="field environment-field">
                <span>Environment</span>
                <select
                  value={env}
                  onChange={(event) =>
                    void navigate({
                      to: ".",
                      search: (previous) => ({ ...previous, env: event.target.value }),
                    })
                  }
                >
                  {(shell.data?.environments ?? []).map((environment) => (
                    <option key={environment.id} value={environment.id}>
                      {environment.id}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="icon-button"
                onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
                title={theme === "dark" ? "Use light mode" : "Use dark mode"}
                aria-label={theme === "dark" ? "Use light mode" : "Use dark mode"}
              >
                <ThemeIcon theme={theme} />
              </button>
              <button
                type="button"
                className={`auth-button ${hasToken ? "has-token" : ""}`}
                onClick={editApiToken}
                title="Set the Authorization bearer token for a token-protected control plane"
              >
                <span className="auth-dot" />
                {hasToken ? "Token set" : "Set token"}
              </button>
            </div>
          </header>
          <main className="content">{body}</main>
        </div>
      </div>
    </div>
  );
}
