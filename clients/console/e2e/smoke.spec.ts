import { expect, test } from "@playwright/test";

import { EXPECT, LANE, rx } from "./expectations";

/**
 * End-to-end smoke (#293): a real served control-plane API and the rendered
 * console agree on routing and the key read-only pages render. Read-only
 * pages only — no operations, so no Temporal connection is needed.
 */

test("overview renders bundles and the project switch rescopes requests", async ({ page }) => {
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));

  await page.goto("/");

  // The Overview table is API-backed: a versioned workflow type only appears
  // once its bundle resolved end-to-end through the served API.
  await expect(
    page.locator("#workflows").getByText(EXPECT.overviewWorkflowText, { exact: false }),
  ).toBeVisible();

  // The operate tier is served on BOTH lanes now (#563): the default project is resolvable +
  // operable, so /meta advertises can_start true (previously honest-false on the ts-cp lane while
  // the TS operate tier was absent). Assert it directly against the served API — the browser-level
  // mirror of the conformance meta capability parity.
  const meta = await page.request.get("/api/v1/meta");
  expect(meta.ok()).toBe(true);
  const capabilities = (await meta.json()).capabilities as {
    can_start: boolean;
    can_review: boolean;
    can_cancel: boolean;
  };
  expect(capabilities.can_start).toBe(true);
  expect(capabilities.can_review).toBe(true);
  expect(capabilities.can_cancel).toBe(true);

  // Two projects in the registry → the switcher renders.
  const switcher = page.locator(".project-switcher");
  await expect(switcher).toBeVisible();

  // Switching rescopes every request to the project's scoped routes — this
  // exercises the fetch middleware + the /api/v1/projects/{id}/... routing.
  const scoped = page.waitForResponse((r) =>
    r.url().includes(`/api/v1/projects/${EXPECT.secondProject}/workflows`),
  );
  await switcher.selectOption(EXPECT.secondProject);
  await scoped;

  expect(errors, "no uncaught page errors").toEqual([]);
});

test("deployments and workflow-detail pages render", async ({ page }) => {
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));

  await page.goto("/#/deployments");
  await expect(page.getByText(/deployment plans/i).first()).toBeVisible();
  // The plan generator renders from the project's workflows/environments.
  await expect(page.getByRole("heading", { name: "Generate a plan" })).toBeVisible();

  await page.goto(`/#/workflows/${EXPECT.workflowId}`);
  await expect(page.getByText(EXPECT.workflowName, { exact: false }).first()).toBeVisible();

  // Run diff page mounts and degrades cleanly when there are no executions.
  await page.goto(`/#/workflows/${EXPECT.workflowId}/run-diff?env=${EXPECT.environment}`);
  await expect(page.getByRole("heading", { name: "Run diff" })).toBeVisible();

  expect(errors, "no uncaught page errors").toEqual([]);
});

test("a run URL cold-loads with the execution addressed (#580)", async ({ page }) => {
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));

  // status/correlation are Temporal-tier endpoints and this smoke is
  // non-live: stub them at the browser edge so no request reaches the API —
  // a real status call against an unreachable Temporal wedges the
  // single-worker dev API for ~30s and starves the following tests (#581
  // tracks fixing that server-side; the stub is scoping, not a workaround
  // we intend to keep once reads are isolated from the Temporal tier).
  await page.route(/\/api\/v1\/workflows\/[^/]+\/(status|correlation)/, (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({
        error: "unavailable",
        message: "temporal unreachable (stubbed in the non-live smoke)",
      }),
    }),
  );

  // The inspector degrades to its error panel without Temporal — but the
  // address must survive the cold load: the inspector banner shows the
  // execution id and the lookup input is pre-filled from the URL.
  await page.goto(`/#/workflows/${EXPECT.workflowId}/runs?env=${EXPECT.environment}&run=smoke-run-001`);
  await expect(page.getByText("inspecting", { exact: true })).toBeVisible();
  await expect(page.getByText("smoke-run-001").first()).toBeVisible();
  await expect(page.getByRole("textbox", { name: "execution id" }).last()).toHaveValue(
    "smoke-run-001",
  );

  // Inspecting through the lookup form addresses the run in the URL.
  await page.getByRole("textbox", { name: "execution id" }).last().fill("smoke-run-002");
  await page.getByRole("button", { name: "Inspect" }).click();
  await expect(page).toHaveURL(new RegExp(`runs\\?env=${rx(EXPECT.environment)}&run=smoke-run-002`));

  // Back returns to the previously inspected run.
  await page.goBack();
  await expect(page).toHaveURL(/run=smoke-run-001/);

  expect(errors, "no uncaught page errors").toEqual([]);
});

test("probe results are cached across navigation (#580)", async ({ page }) => {
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));
  let connectionProbes = 0;
  page.on("request", (request) => {
    if (request.url().includes("/connections")) connectionProbes += 1;
  });

  await page.goto(`/#/workflows/${EXPECT.workflowId}?env=${EXPECT.environment}`);
  await page.getByRole("button", { name: "Check connections" }).click();
  await expect(page.locator("#connections").getByText("registry")).toBeVisible();
  await expect(page.locator("#connections").getByText(/checked \d/)).toBeVisible();

  // Navigate away and back: pressing the probe button again renders the
  // cached result — within staleTime the query must NOT refetch.
  await page
    .locator(".sidebar")
    .getByRole("link", { name: EXPECT.secondWorkflowId, exact: true })
    .click();
  await expect(page.getByRole("heading", { name: EXPECT.secondWorkflowId })).toBeVisible();
  await page
    .locator(".sidebar")
    .getByRole("link", { name: EXPECT.workflowId, exact: true })
    .click();
  await page.locator("#connections").getByRole("button", { name: "Check connections" }).click();
  await expect(page.locator("#connections").getByText("registry")).toBeVisible();
  expect(connectionProbes, "second check served from the query cache").toBe(1);

  expect(errors, "no uncaught page errors").toEqual([]);
});

test("the drift page renders the N-way environment matrix with diff links (#611)", async ({ page }) => {
  await page.goto(`/#/drift?env=${EXPECT.environment}`);
  await expect(page.getByRole("heading", { name: /Environment matrix/ })).toBeVisible();
  // The matrix renders one row per workflow with a linkable state badge per other env.
  const matrixRow = page
    // Row textContent concatenates cells without whitespace — scope by the exact id cell.
    .locator("#env-matrix tr", { has: page.getByText(EXPECT.workflowId, { exact: true }) })
    .first();
  await expect(matrixRow).toBeVisible();
  // The fixture's other environment (cloud/unreachable per lane) does not
  // resolve locally — the honest cell state is the unlinked "—" badge, and the not-comparable
  // wiring shows through the badge presence. Link hrefs are pinned by the engine unit tests.
  await expect(matrixRow.locator(".badge").first()).toBeVisible();
});

test("the drift page aggregates plan, environment, and prompt drift (#583)", async ({ page }) => {
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));

  await page.goto(`/#/drift?env=${EXPECT.environment}`);
  await expect(page.getByRole("heading", { name: "Plan drift" })).toBeVisible();
  await expect(page.getByRole("heading", { name: /Environment drift/ })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Prompt drift" })).toBeVisible();

  // The examples project ships no approved plans, so every workflow is a
  // coverage gap — a warning row per workflow, deep-linked to Deployments.
  await expect(
    // exact: the subworkflow fixture's row ("subworkflow: no approved…") contains
    // this text as a suffix, so a substring match resolves to two elements (#55).
    page
      .locator("#plan-drift")
      .getByText(`${EXPECT.workflowId}: no approved deployment plan`, { exact: true }),
  ).toBeVisible();

  // Environment drift settles to rows and/or not-comparable notes (the
  // cloud environment does not resolve without its variables locally).
  await expect(
    page.locator("#environment-drift").getByText(/no drift|drift between|Not comparable/).first(),
  ).toBeVisible({ timeout: 15_000 });

  // Prompt drift stays behind its explicit check, then reports per workflow.
  // "unknown" (no last-run baseline) is neutral, never a green "in sync".
  await page.getByRole("button", { name: "Check prompt drift" }).click();
  await expect(
    page
      .locator("#prompt-drift")
      .getByText(/in sync|drifting|unknown|unavailable|no prompts/)
      .first(),
  ).toBeVisible({ timeout: 15_000 });

  expect(errors, "no uncaught page errors").toEqual([]);
});

test("the drift page's Temporal-tier drain + pin-skew classes check on demand and degrade honestly (#577)", async ({ page }) => {
  // The two Temporal-tier reads answer only at their client bound against a dead-but-routable
  // cluster (ts-cp lane), so both probes fire concurrently and the test is given headroom.
  test.setTimeout(60_000);
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));

  await page.goto(`/#/drift?env=${EXPECT.environment}`);
  // Both classes are on-demand probes (like prompt drift) — no Temporal read fires until checked.
  await expect(page.getByRole("heading", { name: "Version drain" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Runtime-pin skew" })).toBeVisible();

  // Fire both probes concurrently, then assert each degrades LOUDLY. No Temporal in the smoke (both
  // lanes): every read errors, so each class renders its explicit "unknown" state — never an empty
  // all-clear, never a crash. The regex matches whether the outage is total (the "…unknown" badge)
  // or partial (the per-workflow "…unknown for:" note).
  await page.getByRole("button", { name: "Check version drain" }).click();
  await page.getByRole("button", { name: "Check runtime-pin skew" }).click();
  await expect(
    page.locator("#version-drain").getByText(/drain status unknown/).first(),
  ).toBeVisible({ timeout: 25_000 });
  await expect(
    page.locator("#pin-skew").getByText(/pin skew unknown/).first(),
  ).toBeVisible({ timeout: 25_000 });

  expect(errors, "no uncaught page errors").toEqual([]);
});

test("the GitHub drift section degrades honestly per the lane's capability (#727)", async ({ page }) => {
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));

  await page.goto(`/#/drift?env=${EXPECT.environment}`);
  const section = page.locator("#github-drift");
  await expect(page.getByRole("heading", { name: "GitHub drift" })).toBeVisible();

  if (EXPECT.githubProvenance.supported) {
    // A Git-sourced fixture with a server token would render the live drift class: an in-sync
    // confirmation, a HEAD-drift row, or a loud partial banner — never an error, never empty.
    await expect(
      section.getByText(/HEAD|behind|in sync|not configured|rate limited|unreachable|unknown/i).first(),
    ).toBeVisible({ timeout: 15_000 });
  } else {
    // Both lanes today: the fixture is a local mount with no Git source/token, so the control
    // plane serves github_provenance: false — the console fetches NOTHING and renders the explicit
    // not-supported panel naming the capability token (feature-detection, never an empty panel).
    await expect(section.getByText("not supported", { exact: true })).toBeVisible();
    await expect(section.getByText("github_provenance", { exact: true })).toBeVisible();
  }

  expect(errors, "no uncaught page errors").toEqual([]);
});

test("insight acknowledgements: the annotations read is served per lane and stale acks render (#733)", async ({ page }) => {
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));

  await page.goto("/");

  // The project-level annotations projection is served on BOTH lanes — pure-YAML, project-level,
  // NO capability gate (ApiCapabilities carries no `annotations` flag). Probe the served API at the
  // browser edge, the same discipline the enforcement/github surfaces use, and assert the lane's
  // fixture ledger size: the examples project (python-cp) ships none, the conformance fixture
  // (ts-cp) ships two.
  const resp = await page.request.get("/api/v1/annotations");
  expect(resp.ok()).toBe(true);
  const body = (await resp.json()) as { annotations: unknown[] };
  expect(Array.isArray(body.annotations)).toBe(true);
  expect(body.annotations.length).toBe(EXPECT.annotations.count);

  const insights = page.locator("#insights");
  await expect(insights).toBeVisible();
  if (EXPECT.annotations.staleAckPattern) {
    // The fixture ships an already-EXPIRED ack, so the console derives a warning-severity stale-ack
    // insight on the project feed naming the entry — expiry surfaces loudly, never silently.
    await expect(
      insights.getByText(`Acknowledgement expired: ${EXPECT.annotations.staleAckPattern}`, {
        exact: false,
      }),
    ).toBeVisible({ timeout: 20_000 });
  } else {
    // The empty-ledger lane derives no stale-ack warning — the feed shows only real findings.
    await expect(insights.getByText(/Acknowledgement expired/)).toHaveCount(0);
  }

  expect(errors, "no uncaught page errors").toEqual([]);
});

test("the runs surface degrades per workflow without Temporal (#589)", async ({ page }) => {
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));

  // No Temporal in the smoke: every workflow's executions fan-out returns a
  // bounded error (#586), so the page renders the empty state plus the
  // per-workflow unavailable notes — never a crash or a stall.
  await page.goto(`/#/runs?env=${EXPECT.environment}`);
  // The heading renders immediately (progressive fan-out, no page gate)…
  await expect(page.getByRole("heading", { name: /Recent executions/ })).toBeVisible();
  // …and the cells settle fast against the bounded backend (#591).
  await expect(page.getByText(/no executions readable/)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/Executions unavailable:/)).toBeVisible();
  await expect(
    page.locator("#runs").getByText(EXPECT.workflowId, { exact: false }).first(),
  ).toBeVisible();

  expect(errors, "no uncaught page errors").toEqual([]);
});

test("the workflow topology renders the #55 composition DAG — parallel branches, collect joins, labeled conditions", async ({ page }) => {
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));

  // Both lanes ship a byte-identical composition fixture: the resolved bundle's
  // topology projection carries `parallel` nodes with `branch`/`collect` edges and a
  // `when`-gated (labeled) edge. The console must render the graph shape — not the old
  // linear spine — so this asserts the ranked-DAG output end-to-end against the served API.
  await page.goto(`/#/workflows/${EXPECT.compositionWorkflowId}?env=${EXPECT.environment}&section=topology`);

  const topology = page.locator("#topology svg.topology-svg");
  await expect(topology).toBeVisible();

  // The fan-out root: a `parallel` block, badged PARALLEL on its node.
  await expect(topology.getByText("PARALLEL", { exact: true }).first()).toBeVisible();

  // Composition edges render with their kind on the path (CSS hooks) — a fan-out
  // `branch` and its `collect` fan-in back to the parallel node. Stroke-only SVG
  // paths have no fill box, so assert they're in the DOM rather than "visible".
  await expect(topology.locator("path.topo-edge.branch").first()).toBeAttached();
  await expect(topology.locator("path.topo-edge.collect").first()).toBeAttached();

  // A when-gate predicate rides its edge as a `when` pill; hovering reveals the exact
  // predicate string in the instant overlay panel — review-lane labels carry decision
  // NAMES, never predicates — so this specifically covers the gated edge, not a lane.
  const whenPill = topology.locator(`g.topo-when[data-condition=${JSON.stringify(EXPECT.compositionCondition)}]`).first();
  await expect(whenPill.locator("rect.topo-when-pill")).toBeVisible();
  await whenPill.hover();
  await expect(
    topology.locator("g.topo-when-detail text", { hasText: EXPECT.compositionCondition }),
  ).toBeVisible();

  // Sub-workflow nodes deep-link to the child workflow's page, and the panel lists the
  // children as links. Navigated separately: on ts-cp the SUB coverage lives in the
  // dedicated `subworkflow` fixture workflow, not the composition one (see expectations).
  await page.goto(
    `/#/workflows/${EXPECT.subWorkflowParentId}?env=${EXPECT.environment}&section=topology`,
  );
  const parentTopology = page.locator("#topology svg.topology-svg");
  await expect(parentTopology.locator("a.topo-node-link").first()).toBeAttached();
  // The dedicated Sub-workflows section (after Activities) links each child, with its
  // calling steps and source path as summary columns.
  await expect(
    page
      .locator("#sub-workflows table.grid")
      .getByRole("link", { name: EXPECT.subWorkflowId, exact: true })
      .first(),
  ).toBeVisible();

  expect(errors, "no uncaught page errors").toEqual([]);
});

test("the governance page renders the policy coverage matrix (#612)", async ({ page }) => {
  await page.goto(`/#/governance?env=${EXPECT.environment}`);
  await expect(page.getByRole("heading", { name: "Policy coverage matrix" })).toBeVisible();
  // The row renders only after the full bundle-matrix fan-out settles — same 15s the
  // adjacent governance coverage smoke gives that settle (codex).
  const row = page
    .locator("#policy-matrix tr", { has: page.getByText(EXPECT.workflowId, { exact: true }) })
    .first();
  await expect(row).toBeVisible({ timeout: 15_000 });
  await expect(row.locator(".badge").first()).toBeVisible();
});

test("the governance page shows coverage, gaps, and structured policies (#587)", async ({ page }) => {
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));

  await page.goto(`/#/governance?env=${EXPECT.environment}`);
  await expect(page.getByRole("heading", { name: "Governance gaps" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Policy coverage" })).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.getByRole("heading", { name: "Policies" })).toBeVisible();

  // The lane's policy fixture: chain/name text plus a structurally-rendered rule
  // path (never raw JSON). python-cp additionally pins the extends-chain rendering.
  await expect(page.getByText(EXPECT.policyChainText).first()).toBeVisible();
  await expect(
    page.locator("#policies").getByText(EXPECT.policyRuleText).first(),
  ).toBeVisible();
  if (LANE === "python-cp") {
    await expect(
      page.locator("#policies").getByText("require_review_routes").first(),
    ).toBeVisible();
  }

  // Coverage matrix renders a covered cell for a local workflow.
  await expect(
    page.locator("#coverage").getByText("covered").first(),
  ).toBeVisible({ timeout: 15_000 });

  expect(errors, "no uncaught page errors").toEqual([]);
});

test("the environment page renders the resolved-workflows slice (#605)", async ({ page }) => {
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));

  await page.goto(`/#/environments/${EXPECT.environment}`);
  // The environment-centric slice: every workflow's row with an admission state.
  await expect(
    page.getByRole("heading", { name: `Workflows in ${EXPECT.environment}` }),
  ).toBeVisible();
  // Scope by the row's own deep-link — hasText substrings collide with header/issue text.
  const row = page
    .locator("tr", { has: page.getByRole("link", { name: EXPECT.workflowId, exact: true }) })
    .first();
  await expect(row).toBeVisible();
  // Rows deep-link into the workflow pinned to THIS environment.
  await row.getByRole("link", { name: EXPECT.workflowId, exact: true }).click();
  await expect(page).toHaveURL(
    new RegExp(`workflows/${rx(EXPECT.workflowId)}\\?env=${rx(EXPECT.environment)}`),
  );

  // The definition detail is still present below the slice sections.
  await page.goto(`/#/environments/${EXPECT.environment}`);
  await expect(page.getByRole("heading", { name: "Environment definition", exact: true })).toBeVisible();
  // The Source row's affordance is never silently absent (#718 §A): it links the file at the
  // resolved sha, or degrades loudly to "source link unavailable" — one is always shown.
  await expect(
    page.locator("#definition").getByText(/View source|source link unavailable/).first(),
  ).toBeVisible();
  expect(errors).toEqual([]);
});

test("in-app navigation carries the environment through links (#578)", async ({ page }) => {
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));

  // Deep-link with a non-default environment, then navigate through the
  // app's own sidebar link — the router must preserve `env` rather than
  // pages silently falling back to the default environment.
  await page.goto(`/#/?env=${EXPECT.secondEnvironment}`);
  await page
    .locator(".sidebar")
    .getByRole("link", { name: EXPECT.workflowId, exact: true })
    .click();
  await expect(page).toHaveURL(
    new RegExp(`workflows/${rx(EXPECT.workflowId)}\\?env=${rx(EXPECT.secondEnvironment)}`),
  );

  // Page-action navigation goes through the typed router too: the diff page
  // mounts with the current environment pre-selected on the left.
  await page.goto(`/#/workflows/${EXPECT.workflowId}?env=${EXPECT.environment}`);
  await page.getByRole("button", { name: "Compare environments" }).click();
  await expect(page).toHaveURL(
    new RegExp(`workflows/${rx(EXPECT.workflowId)}/diff\\?left=${rx(EXPECT.environment)}`),
  );
  await expect(page.getByLabel("left")).toHaveValue(EXPECT.environment);

  expect(errors, "no uncaught page errors").toEqual([]);
});

test("a foreign-runtime project degrades honestly — never a raw error (#621 slice 3)", async ({ page }) => {
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));

  // Switch to the registry entry this lane's server cannot resolve. Both lane
  // registries carry one (python lane: a typescript project; ts lane: a python
  // project) — the browser-level mirror of the conformance capability-honesty
  // variants.
  await page.goto("/");
  await expect(page.locator(".project-switcher")).toBeVisible();
  await page.locator(".project-switcher").selectOption(EXPECT.foreignProject);

  // The switcher itself says why the project is limited here.
  await expect(page.locator(".project-switcher option:checked")).toContainText("not resolvable here");

  // INSPECTION STAYS AVAILABLE: the pure-YAML surfaces (the workflows table) still
  // render for an unresolvable project — only the resolution-backed validation
  // section degrades to the honest unavailable prose. No error panels anywhere.
  await expect(page.locator("#workflows")).toBeVisible();
  await expect(
    page.locator("#project-issues").getByText(/cannot resolve the project's declared runtime/),
  ).toBeVisible();
  await expect(page.locator(".error-panel")).toHaveCount(0);

  // First-class navigation targets degrade too — the Deployments route is
  // resolution-backed on the Python edition (codex).
  await page.goto("/#/deployments");
  await expect(page.getByText(/cannot resolve the project's declared runtime/).first()).toBeVisible();
  await expect(page.locator(".error-panel")).toHaveCount(0);

  expect(errors, "no uncaught page errors").toEqual([]);
});

test("the governance persona view leads with coverage, policy hashes, and the enforcement feed (#721/#723)", async ({ page }) => {
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));

  await page.goto(`/#/personas/governance?env=${EXPECT.environment}`);
  await expect(page.getByRole("heading", { name: "Governance coverage" })).toBeVisible();

  // The policy-hash table renders each declared policy with a source-of-truth affordance that
  // is never silently absent (#718 §A): a link or the loud "unavailable" note — one is shown.
  await expect(
    page.locator("#policy-hashes").getByText(/View source|source link unavailable/).first(),
  ).toBeVisible();

  // The violations panel is the REAL enforcement feed now (#723 slice 3) — same section
  // anchor the planned panel held. The lane's fixture behavior is asserted in its own test.
  await expect(
    page.getByRole("heading", { name: "Policy violations & enforcement" }),
  ).toBeVisible();
  await expect(page.locator("#violations").getByText("planned", { exact: true })).toHaveCount(0);

  // The coverage rollup settles to a real percentage from the resolved-bundle matrix.
  await expect(
    page.locator("#coverage-rollup").getByText(/under policy/).first(),
  ).toBeVisible({ timeout: 20_000 });

  expect(errors, "no uncaught page errors").toEqual([]);
});

test("the enforcement feed serves the fixture's real verdicts with loud partial state (#723)", async ({ page }) => {
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));

  await page.goto(`/#/personas/governance?env=${EXPECT.environment}`);
  const feed = page.locator("#violations");
  await expect(feed).toBeVisible();

  // Neither lane's fixture configures a Langfuse observer, so the runtime portion degrades
  // LOUDLY (not_configured) — never a silent omission over the runtime source.
  await expect(feed.getByText("runtime not traced", { exact: true })).toBeVisible({
    timeout: 15_000,
  });

  // The filter controls render (workflow / verdict / window presets).
  await expect(feed.getByLabel("verdict")).toBeVisible();
  await expect(feed.getByLabel("window")).toBeVisible();

  if (EXPECT.enforcementFeed.kind === "rows") {
    // The fixture project yields real admission events (probed live: the examples
    // project's anthropic/custom workflows are rejected under the openai-only policy).
    // Assert on the STABLE rule code + row-presence semantics (verdict badge, evidence
    // link) rather than the free-prose detail, which is server wording and brittle.
    await expect(feed.getByText(EXPECT.enforcementFeed.ruleText).first()).toBeVisible();
    const verdictBadge = feed.locator(".badge", { hasText: "rejected" }).first();
    await expect(verdictBadge).toBeVisible();
    // Admission events are point-in-time-now: the row renders an explicit "—", never a
    // fabricated timestamp, and its evidence hands off to the validation section.
    await expect(feed.getByRole("link", { name: "Validation" }).first()).toBeVisible();

    // Filtering to the OTHER verdict (blocked) empties the feed — the wiring is live and
    // the empty state stays scoped-honest (runtime portion not read in full).
    await feed.getByLabel("verdict").selectOption("blocked");
    await expect(feed.getByText(/No admission enforcement events/).first()).toBeVisible({
      timeout: 15_000,
    });
  } else {
    // The fixture admits cleanly: the honest empty state is the SCOPED confirmation —
    // admission-only, because the runtime portion is not_configured, never a green
    // all-clear over an unread source.
    await expect(feed.getByText(/No admission enforcement events/).first()).toBeVisible({
      timeout: 15_000,
    });
  }

  // The same shared feed mounts on the Governance page proper (one component, two mounts).
  await page.goto(`/#/governance?env=${EXPECT.environment}`);
  await expect(
    page.getByRole("heading", { name: "Policy violations & enforcement" }),
  ).toBeVisible();
  await expect(
    page.locator("#violations").getByText("runtime not traced", { exact: true }),
  ).toBeVisible({ timeout: 15_000 });

  expect(errors, "no uncaught page errors").toEqual([]);
});

test("the security-posture persona view shows runtime controls and policy-enforced controls (#721)", async ({ page }) => {
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));

  await page.goto(`/#/personas/security?env=${EXPECT.environment}`);
  await expect(page.getByRole("heading", { name: "Security posture" })).toBeVisible();

  // The policy-enforced controls table renders from policy definitions (redaction requirement,
  // module allowlist, artifact allowlist) with per-row source links.
  await expect(page.getByRole("heading", { name: "Policy-enforced controls" })).toBeVisible();
  await expect(page.locator("#policy-controls").getByText("Redaction required")).toBeVisible();

  // The per-(workflow × environment) posture matrix settles with a real workflow row and its
  // source-link hand-off.
  await expect(
    page.locator("#posture-matrix").getByText(EXPECT.workflowId, { exact: false }).first(),
  ).toBeVisible({ timeout: 20_000 });

  expect(errors, "no uncaught page errors").toEqual([]);
});

test("the operations persona view rolls up failure-first executions and degrades on no Temporal (#721)", async ({ page }) => {
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));

  await page.goto(`/#/personas/operations?env=${EXPECT.environment}`);
  await expect(page.getByRole("heading", { name: /Operational health/ })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Failure-first triage" })).toBeVisible();

  // The rollup tiles render (labels are lane-agnostic) once the executions fan-out settles —
  // the tiles are gated behind settle (#721 F2) so pending cells never show as confident zeros,
  // so give them the same bounded-settle window the triage below uses.
  await expect(page.locator("#ops-rollup").getByText("failing")).toBeVisible({ timeout: 15_000 });

  // No Temporal in the smoke: the triage degrades outage-honestly (bounded errors), never a
  // false green all-clear, and settles fast against the bounded backend.
  await expect(
    page.locator("#ops-triage").getByText(/no executions|Temporal|Executions unavailable/i).first(),
  ).toBeVisible({ timeout: 15_000 });

  expect(errors, "no uncaught page errors").toEqual([]);
});

test("the executive persona view renders a plain-language governance rollup (#721)", async ({ page }) => {
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));

  await page.goto(`/#/personas/executive?env=${EXPECT.environment}`);
  await expect(page.getByRole("heading", { name: "Governance at a glance" })).toBeVisible();

  // The rollup settles to the plain-language narrative + stat tiles from resolved bundles.
  await expect(
    page.locator("#executive-rollup").getByText(/AI workflow/).first(),
  ).toBeVisible({ timeout: 20_000 });
  await expect(
    page.locator("#executive-rollup").getByText(/under policy|governance coverage cannot/i).first(),
  ).toBeVisible();

  expect(errors, "no uncaught page errors").toEqual([]);
});
