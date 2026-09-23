/**
 * Console-assisted deployment plan generation (#290): assemble the exact
 * `deploy --plan-out` command from an operator's selections. Pure — the console
 * writes nothing; the operator runs the command, and promotion stays approved
 * by merging the plan file's PR.
 */

const DEPLOY_PREFIX = "uv run typeflux-project deploy";

export interface PlanCommandOptions {
  manifest: string;
  workflow: string;
  environment: string;
  policies: string[];
  image: string;
}

/** A worker image is reproducible only when pinned to a content digest. */
export function isDigestPinned(image: string): boolean {
  return /@sha256:[0-9a-f]{64}$/i.test(image.trim());
}

/** Single-quote a value so it survives being pasted into a shell verbatim. */
function shellQuote(value: string): string {
  return `'${value.replace(/'/g, "'\\''")}'`;
}

/**
 * The exact CLI command that writes the plan. The manifest path and image are
 * shell-quoted (they can hold spaces or, for the operator-typed image,
 * metacharacters); an empty image renders a quoted placeholder so the command
 * is a safe, copyable template. Workflow/environment/policy ids are constrained
 * identifiers and stay bare for readability.
 */
export function planCommand(opts: PlanCommandOptions): string {
  const image = opts.image.trim() || "<digest-pinned-image>";
  const parts = [
    DEPLOY_PREFIX,
    shellQuote(opts.manifest),
    `--environment ${opts.environment}`,
    `--workflow ${opts.workflow}`,
    ...opts.policies.map((policy) => `--policy ${policy}`),
    `--image ${shellQuote(image)}`,
    "--plan-out deployments/",
  ];
  return parts.join(" \\\n  ");
}
