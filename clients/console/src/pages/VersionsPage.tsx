import { Badge, ErrorPanel, InsightList, KV, Loading, Mono, Section } from "../components";
import { deriveDrainInsights } from "../insights";
import { errorMessage, useVersions } from "../queries";

export function VersionsPage({ workflowId, env }: { workflowId: string; env: string }) {
  const state = useVersions(workflowId, env);

  if (state.isPending) return <Loading what="cross-version drain status" />;
  if (state.isError || !state.data) {
    return (
      <ErrorPanel
        message={errorMessage(state.error) ?? "no data"}
        hint="The drain view queries the Temporal cluster; check that Temporal is reachable from the control-plane server."
      />
    );
  }

  const drain = state.data;
  const types = Object.keys(drain.running);

  return (
    <>
      <Section id="drain" title="Cross-version drain">
        <div className="panel">
          <KV
            rows={[
              ["Logical workflow", <Mono key="l">{drain.logical_workflow}</Mono>],
              ["Current type", <Mono key="c">{drain.current_workflow_type}</Mono>],
              [
                "Drained",
                drain.drained ? (
                  <span key="d" className="badge badge-ok">
                    drained
                  </span>
                ) : (
                  <span key="d" className="badge badge-warning">
                    not drained
                  </span>
                ),
              ],
              ["Total running", String(drain.total_running)],
              ["Visibility query", <Mono key="q">{drain.query}</Mono>],
            ]}
          />
          <div className="hint">
            Fail-safe gating: type-prefix matching can at worst over-match a same-prefix sibling
            (false "not drained"), never under-match. Old versions are safe to decommission only
            when drained.
          </div>
        </div>
      </Section>

      <Section id="versions" title="Running executions per version">
        <div className="panel">
          {types.length === 0 ? (
            <span className="dim">No running executions.</span>
          ) : (
            <table className="grid">
              <thead>
                <tr>
                  <th>Workflow type</th>
                  <th>Running</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {types.map((workflowType) => (
                  <tr key={workflowType}>
                    <td>
                      <Mono>{workflowType}</Mono>
                    </td>
                    <td>{drain.running[workflowType]}</td>
                    <td>
                      {workflowType === drain.current_workflow_type ? (
                        <Badge kind="info">current</Badge>
                      ) : (
                        <Badge kind="warning">old version</Badge>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </Section>

      <Section id="insights" title="Insights">
        <div className="panel">
          <InsightList insights={deriveDrainInsights(workflowId, env, drain)} />
        </div>
      </Section>
    </>
  );
}
