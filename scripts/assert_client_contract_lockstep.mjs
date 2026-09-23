// The control-plane client's major version is locked to the contract's API
// major (#616 slice 3). Run from clients/typescript. With RELEASE_VERSION
// set (the release workflow), also asserts the release equals package.json.
import { readFileSync } from "node:fs";

const pkg = JSON.parse(readFileSync("package.json", "utf-8"));
const contract = JSON.parse(
  readFileSync("../../contracts/controlplane/openapi.v1.json", "utf-8"),
);

const release = process.env.RELEASE_VERSION;
if (release !== undefined && pkg.version !== release) {
  console.error(`package.json version ${pkg.version} != release ${release}`);
  process.exit(1);
}

const major = pkg.version.split(".")[0];
if (major !== contract.info.version) {
  console.error(`client major ${major} != contract info.version ${contract.info.version}`);
  process.exit(1);
}

console.log(`version lockstep ok: ${pkg.version} (contract major ${contract.info.version})`);
