/** Run the installed foreground bin from a standalone consumer with no network. */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { join } from "node:path";

const guard = join(process.cwd(), "local-cli-guard.mjs");
writeFileSync(guard, `
import {registerHooks} from 'node:module';
import net from 'node:net';
registerHooks({resolve(specifier, context, next) {
  if (specifier.startsWith('@temporalio/')) throw new Error('Temporal import forbidden: '+specifier);
  return next(specifier, context);
}});
net.Socket.prototype.connect = function() { throw new Error('Network access forbidden'); };
`);
const bin = join(process.cwd(), "node_modules/.bin/typeflux-local");
for (const [target, input] of [
  [["workflow.yaml"], "input.json"],
  [["workflow.yaml"], "-"],
  [["--project", "typeflux.project.yaml", "--workflow", "echo", "--environment", "offline"], "input.json"],
]) {
  const result = spawnSync(bin, [...target, "--bindings", "bindings.mjs", "--input", input], {
    cwd: join(process.cwd(), "local-example"),
    env: {...process.env, TYPEFLUX_EXPECTED_POLICY_HASH: "", NODE_OPTIONS: `--import=${guard}`},
    encoding: "utf8", input: input === "-" ? '{"text":"stdin"}' : undefined, timeout: 15000,
  });
  assert.equal(result.status, 0, `${result.error ?? ""}\n${result.stderr}`);
  assert.equal(result.stdout, '{"text":"local result"}\n');
}
const sdk = spawnSync(process.execPath, ["local-sdk.mjs"], {
  cwd: join(process.cwd(), "local-example"),
  env: {...process.env, NODE_OPTIONS: `--import=${guard}`}, encoding: "utf8", timeout: 15000,
});
assert.equal(sdk.status, 0, sdk.stderr);
assert.equal(sdk.stdout, '{"text":"local result"}\n');
console.log("Installed local CLI/SDK examples passed without Temporal or network");
