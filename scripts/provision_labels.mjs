/**
 * Provision repository labels from .github/label-policy.json (the label
 * source of truth). Idempotent: creates missing labels, updates color or
 * description drift, touches nothing else.
 *
 * Needed because a fresh repository starts with only GitHub's default
 * labels — issue forms silently skip labels that don't exist, which would
 * disarm the auto-fix blocking set. Run once when seeding a new repository
 * (and any time the policy file changes):
 *
 *   node scripts/provision_labels.mjs <owner>/<repo>
 *
 * Requires an authenticated `gh` CLI with repo scope.
 */

import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repo = process.argv[2];
if (!repo || !/^[\w.-]+\/[\w.-]+$/.test(repo)) {
  process.stderr.write("usage: node scripts/provision_labels.mjs <owner>/<repo>\n");
  process.exit(2);
}

const HERE = dirname(fileURLToPath(import.meta.url));
const policy = JSON.parse(
  readFileSync(resolve(HERE, "../.github/label-policy.json"), "utf8"),
);

const gh = (args, input) =>
  execFileSync("gh", args, { encoding: "utf8", input });

const existing = new Map(
  // --slurp folds --paginate's one-array-per-page output into a single array
  // of pages; flatten to labels before mapping.
  JSON.parse(gh(["api", `repos/${repo}/labels?per_page=100`, "--paginate", "--slurp"]))
    .flat()
    .map((l) => [l.name, l]),
);

let created = 0;
let updated = 0;
for (const [name, { color, description }] of Object.entries(policy.labels)) {
  const current = existing.get(name);
  if (!current) {
    gh([
      "api", `repos/${repo}/labels`, "-X", "POST",
      "-f", `name=${name}`, "-f", `color=${color}`, "-f", `description=${description}`,
    ]);
    created += 1;
  } else if (
    current.color.toLowerCase() !== color.toLowerCase() ||
    (current.description ?? "") !== description
  ) {
    gh([
      "api", `repos/${repo}/labels/${encodeURIComponent(name)}`, "-X", "PATCH",
      "-f", `color=${color}`, "-f", `description=${description}`,
    ]);
    updated += 1;
  }
}
process.stderr.write(
  `provision-labels: ${created} created, ${updated} updated, ${Object.keys(policy.labels).length} total in policy\n`,
);
