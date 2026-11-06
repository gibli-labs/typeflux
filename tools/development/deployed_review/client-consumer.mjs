import { createControlPlaneClient } from "@typeflux/control-plane-client";

const [expectedWorkflow] = process.argv.slice(2);
const client = createControlPlaneClient({baseUrl: "http://api:8400",
  headers: {Authorization: "Bearer test-reader"}});
const {data, error, response} = await client.GET("/api/v1/projects/{project}/workflows/{workflow_id}/bundle", {
  params: {path: {project: "deployment", workflow_id: "workflow"}, query: {environment_id: "local"}},
});
if (!response.ok || error || data?.workflow?.workflow_name !== expectedWorkflow)
  throw new Error("installed generated client did not read the expected real bundle");
console.log(JSON.stringify({package: "@typeflux/control-plane-client", realApiStatus: response.status,
  workflowName: data.workflow.workflow_name, policyHash: data.policy.policy_hash}));
