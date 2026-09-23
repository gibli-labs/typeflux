import type React from "react";

import type { ProjectSummary } from "../api";
import { ErrorPanel, JsonView, KV, Loading, Mono, Section, ShortDigest, SourceLinks } from "../components";
import { definitionSourceLinks } from "../links";
import { PolicyRulesPanel } from "../panels/governance";
import { errorMessage, useDefinition } from "../queries";

/** Read-only definition detail: environments, policies, profiles. */
export function DefinitionPage({
  kind,
  id,
  profileKind,
  env,
  project,
  sourcePath,
}: {
  kind: "policy" | "profile";
  id: string;
  profileKind?: string;
  env: string;
  project?: ProjectSummary;
  /** The policy's manifest-relative source (from the shell listing — the detail DTO has none). */
  sourcePath?: string;
}) {
  const state = useDefinition(kind, id, profileKind);

  if (state.isPending) return <Loading what={`${kind} ${id}`} />;
  if (state.isError || !state.data) {
    return <ErrorPanel message={errorMessage(state.error) ?? "no data"} />;
  }
  const payload = state.data;

  const usedBy = (payload.data.used_by ?? []) as string[];
  const sourceValue = (reference: string) => (
    <span key="s">
      <Mono>{reference}</Mono>{" "}
      <SourceLinks {...definitionSourceLinks(project, reference)} />
    </span>
  );
  return (
    <>
      <Section id="definition" title={`${kind} definition`}>
        <div className="panel">
          {payload.type === "policy" ? (
            <>
              <KV
                rows={[
                  ["Name", <Mono key="n">{payload.data.name}</Mono>],
                  ["Description", payload.data.description ?? <span className="faint">—</span>],
                  ["Extends", <Mono key="e">{(payload.data.extends ?? []).join(", ") || "—"}</Mono>],
                  ...(sourcePath !== undefined ? [["Source", sourceValue(sourcePath)] as [string, React.ReactNode]] : []),
                  [
                    "Policy hash",
                    payload.data.policy_hash ? (
                      <ShortDigest key="h" value={payload.data.policy_hash} />
                    ) : (
                      <span className="faint">composition failed</span>
                    ),
                  ],
                ]}
              />
              <PolicyRulesPanel rules={payload.data.rules} />
            </>
          ) : payload.type === "profile" ? (
            <>
              <KV
                rows={[
                  ["Kind", <Mono key="k">{payload.data.kind}</Mono>],
                  ["Name", <Mono key="n">{payload.data.name}</Mono>],
                  ["Source", sourceValue(payload.data.path)],
                  ["Content hash", <ShortDigest key="h" value={payload.data.content_hash} />],
                ]}
              />
              <JsonView label="owned runtime subtree" value={payload.data.runtime} />
              <div className="hint">Secrets appear as value_from references only.</div>
            </>
          ) : null}
          <div className="hint">Managed in code — edit the source file, not the console.</div>
        </div>
      </Section>
      <Section id="used-by" title="Used by">
        <div className="panel">
          {usedBy.length === 0 ? (
            <span className="dim">No declared references.</span>
          ) : (
            <ul className="insight-list">
              {usedBy.map((reference) => {
                const workflowId = reference.split(" ")[0];
                // Profile references carry their own "(environment)"; bare
                // workflow-level references use the selected environment.
                const referencedEnv = /\(([^)]+)\)/.exec(reference)?.[1] ?? env;
                return (
                  <li key={reference}>
                    <span />
                    <span>
                      <a href={`#/workflows/${workflowId}?env=${referencedEnv}`}>
                        <Mono>{reference}</Mono>
                      </a>
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </Section>
    </>
  );
}
