/**
 * Governance panels (#587): structured policy rules (shared with the sidebar
 * policy pages), the coverage matrix, and the composition chain. Panels never
 * import pages; effective rule values stay the backend's job — these render
 * what each policy file says and where it applies.
 */

import { Link } from "@tanstack/react-router";

import type { PolicyDefinition } from "../api";
import { Badge, JsonView, KV, Mono, Section, ShortDigest } from "../components";
import type { CoverageRow, SectionProvenance } from "../governance";
import { isUnsetRuleValue, policySections } from "../governance";

/**
 * An explicit empty value is the opposite of unset: for allowlists it means
 * *nothing is allowed*. Render it unmistakably, never as a dash.
 */
function ExplicitlyEmpty() {
  return (
    <span
      className="badge badge-warning"
      title="An explicit empty value — for an allowlist this means nothing is allowed."
    >
      explicitly empty
    </span>
  );
}

/** One rule section as a compact definition table; unknown shapes stay JSON. */
function RuleValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="faint">—</span>;
  if (typeof value === "boolean") return <Mono>{value ? "true" : "false"}</Mono>;
  if (typeof value === "number" || typeof value === "string") return <Mono>{String(value)}</Mono>;
  if (Array.isArray(value)) {
    if (value.length === 0) return <ExplicitlyEmpty />;
    if (value.every((item) => item === null || typeof item !== "object")) {
      return <Mono>{value.map(String).join(", ")}</Mono>;
    }
  }
  if (!Array.isArray(value) && typeof value === "object" && Object.keys(value).length === 0) {
    return <ExplicitlyEmpty />;
  }
  return <JsonView label="value" value={value} />;
}

function RuleRows({ value, prefix }: { value: unknown; prefix: string }) {
  if (value === null || value === undefined || typeof value !== "object" || Array.isArray(value)) {
    return (
      <tr>
        <td>
          <Mono>{prefix}</Mono>
        </td>
        <td>
          <RuleValue value={value} />
        </td>
      </tr>
    );
  }
  return (
    <>
      {Object.entries(value as Record<string, unknown>).map(([key, child]) => {
        const path = prefix ? `${prefix}.${key}` : key;
        // Unset leaves (nulls, all-unset skeletons) are schema noise, not
        // policy content; explicit empties are content and render loudly.
        if (isUnsetRuleValue(child)) return null;
        const isLeaf =
          child === null ||
          typeof child !== "object" ||
          Array.isArray(child) ||
          Object.keys(child as Record<string, unknown>).length === 0;
        return isLeaf ? (
          <tr key={path}>
            <td>
              <Mono>{path}</Mono>
            </td>
            <td>
              <RuleValue value={child} />
            </td>
          </tr>
        ) : (
          <RuleRows key={path} value={child} prefix={path} />
        );
      })}
    </>
  );
}

/**
 * A policy's rules, structurally: one table per rule section in spec order,
 * flattened to dotted paths. New backend rule kinds land in an "unknown"
 * section rendered as JSON — they degrade, never disappear.
 */
export function PolicyRulesPanel({ rules }: { rules: unknown }) {
  const sections = policySections((rules ?? null) as Record<string, unknown> | null);
  if (sections.length === 0) {
    return <span className="dim">This policy declares no rules of its own.</span>;
  }
  return (
    <>
      {sections.map((section) => (
        <div key={section.key} style={{ marginBottom: 10 }}>
          <div className="row" style={{ marginBottom: 4 }}>
            <Mono>{section.key}</Mono>
            {section.known ? null : (
              <span
                className="badge badge-neutral"
                title="A rule section this console version doesn't know — shown raw so it never disappears."
              >
                unknown section
              </span>
            )}
          </div>
          {section.known ? (
            <table className="grid">
              <thead>
                <tr>
                  <th>Rule</th>
                  <th>Value</th>
                </tr>
              </thead>
              <tbody>
                <RuleRows value={section.value} prefix="" />
              </tbody>
            </table>
          ) : (
            <JsonView label={section.key} value={section.value} />
          )}
        </div>
      ))}
    </>
  );
}

/** Which layers of the extends chain speak to each rule section. */
export function CompositionChainPanel({
  chain,
  provenance,
}: {
  chain: PolicyDefinition[];
  provenance: SectionProvenance[];
}) {
  return (
    <>
      <KV
        rows={[
          [
            "Composition order",
            <Mono key="o">{chain.map((policy) => policy.name).join(" → ")}</Mono>,
          ],
        ]}
      />
      <table className="grid" style={{ marginTop: 8 }}>
        <thead>
          <tr>
            <th>Rule section</th>
            <th>Defined by</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {provenance.map((entry) => (
            <tr key={entry.section}>
              <td>
                <Mono>{entry.section}</Mono>
              </td>
              <td>
                <Mono>{entry.definedBy.join(", ")}</Mono>
              </td>
              <td>
                {entry.overridden ? (
                  <span
                    className="badge badge-info"
                    title="More than one layer defines this section; later layers override earlier ones."
                  >
                    layered
                  </span>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="hint">
        Section-level provenance only: effective values are composed by the backend (the policy
        hash is the composed identity) — the console never re-implements the merge.
      </div>
    </>
  );
}

const COVERAGE_BADGE: Record<string, { className: string; label: string; title: string }> = {
  covered: { className: "badge-ok", label: "covered", title: "Policies applied in this environment." },
  none: { className: "badge-warning", label: "none", title: "No policy applied — constraints unenforced." },
  composition_failed: {
    className: "badge-critical",
    label: "failed",
    title: "Selected policies could not be composed; nothing is enforced.",
  },
  unresolvable: {
    className: "badge-neutral",
    label: "unresolvable",
    title: "The workflow does not resolve in this environment; coverage is unknown.",
  },
};

/** Policies × workflows × environments in one glance. */
export function CoverageMatrixPanel({
  rows,
  environmentIds,
  env,
}: {
  rows: CoverageRow[];
  environmentIds: string[];
  env: string;
}) {
  return (
    <Section id="coverage" title="Policy coverage">
      <div className="panel">
        <table className="grid">
          <thead>
            <tr>
              <th>Workflow</th>
              {environmentIds.map((environmentId) => (
                <th key={environmentId}>{environmentId}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.workflowId}>
                <td>
                  <Link
                    to="/workflows/$workflowId"
                    params={{ workflowId: row.workflowId }}
                    search={{ env, section: "policy" }}
                  >
                    <Mono>{row.workflowId}</Mono>
                  </Link>
                </td>
                {environmentIds.map((environmentId) => {
                  const cell = row.byEnv[environmentId];
                  const badge = COVERAGE_BADGE[cell?.state ?? "unresolvable"];
                  return (
                    <td key={environmentId}>
                      <span className={`badge ${badge.className}`} title={badge.title}>
                        {badge.label}
                      </span>{" "}
                      {cell?.state === "covered" ? (
                        <>
                          <Mono>{cell.appliedPolicyIds.join(", ")}</Mono>{" "}
                          {cell.policyHash ? <ShortDigest value={cell.policyHash} /> : null}
                        </>
                      ) : null}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
        <div className="hint">
          From each (workflow, environment) resolved bundle — the policies actually applied, not
          just selected. <Badge kind="ok">covered</Badge> cells show the applied ids <em>in
          composition order</em> (later layers override earlier ones) and the composed policy
          hash, which is the effective identity.
        </div>
      </div>
    </Section>
  );
}
