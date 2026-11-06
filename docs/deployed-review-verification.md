# Local Docker deployment verification

This runbook exercises installed candidate artifacts in a non-production Docker
target. It uses the existing Local CLI/SDK and Temporal worker/control-plane
entrypoints, a real production console build, and fixed fake providers. It adds
no hosting service, execution engine, scheduler or persistence to Local.

The selected target is a local Unix-socket Docker context. No AWS login, cloud
resources, registry publication or production access is needed. Temporal runs on
an internal network with an owned persistent SQLite volume; workers have no
Internet route. A separate owned bridge permits loopback-only API and console
ports. The API/console bridge can route outbound, but fixture providers require no
external service. Do not use real credentials or private application projects.

## Evidence and bounds

Fetch accepted CI for the exact clean **main producer revision** with the maintained
release-evidence verifier before starting the target. PR snapshots, local reports
and task journals cannot substitute. Freeze wheel/tarball/console hashes, producer
source and graph digests, fixture/observer hashes, immutable image IDs, environment
resources and thresholds. The observer's code identity is separate from the SDK
producer identity. A local inventory binds observations; it does not independently
authorize a release or validate a hand-written CI receipt.

The controller records a new `frozen.json` before target startup. Bounds are
60 seconds for startup, 30 seconds per state/result observation, a three-second
API Temporal deadline with an eight-second transport ceiling, and no automatic
mutation retries. Read-only polling observes state within its window. Each
container has two CPU, 2 GiB memory and 256-process ceilings, dropped capabilities,
`no-new-privileges` and a reaping init; editions run sequentially. The local
Docker host must have capacity for the API, worker, service and observer together.
Resource ceilings are not proof of actual utilization or production sizing.

The installed candidates must establish these observations:

- Base Python and Node Local CLI/SDK consumers execute with Temporal absent and
  networking disabled, using the packaged SDK rather than a repository fallback.
- The installed generated client reads the real bundle; both actual API editions
  enforce the policy hash, input shape, permissions and project/version binding.
- A run reaches its actual review gate. Killing its worker and persistent Temporal
  process preserves the same run/history. The backend-paused probe runs with its
  worker already stopped; a second probe isolates a reachable backend without its
  worker. Both return bounded errors. A compatible replacement observes the original
  gate and acknowledged work.
- The built console submits once over real HTTP, then displays lifecycle completion.
  Native history and validated output establish completion; headings and HTTP 204
  alone do not. No browser operation response is fulfilled or replaced.
- Explicit old-run addressing cannot signal a later run after deliberate execution-ID
  reuse. Existing adapters return generic HTTP 500 for this probe; its wire response
  is retained, but cannot identify a native rejection cause and is not a healthy
  response. A separate native read establishes that the pinned old run is terminal.
  Native history must show no signal in the new run before its separate deliberate
  decision, which must then succeed and establish completion.
- A transport proxy forwards one real review, waits for its actual 204, then discards
  the client response. Status/history reconcile one signal and completion, with no
  resubmission of the uncertain command. This is a wire fault, not an API mock.
- Actual history replays against the installed compatible interpreter. Reusing a
  frozen version label for a changed graph is refused at Python runtime construction
  or TypeScript new-start admission. A separately versioned worker executes the
  changed graph; old runs are observed as draining before the old worker is removed.
- A safe first-attempt activity fault follows the existing five-second retry policy,
  with attempts and same-run results observed. This is separate from mutation retries.

The console currently addresses the **current run by execution ID** and status DTOs
may contain a null run ID. The explicit HTTP probes use a native-described run ID;
the browser run is reconciled against native history before and afterwards. The
fixture uses synthetic identities and fixed inputs, without reviewer notes or real
personal data. Its unknown-decision policy is `warn`; the API rejection probe uses
a malformed command, not a claim that every unknown decision is rejected.

## Prepare installed candidates

Use the tool versions and frozen setup from [development.md](development.md). Use
a separate clean producer checkout so observer edits cannot be misidentified as
accepted SDK source. Run these steps from the observer repository; choose a new
temporary directory and an explicit accepted main SHA:

```sh
OBSERVER="$PWD"
CASE_DIR=$(mktemp -d /tmp/typeflux-deployed-review.XXXXXX)
PRODUCER="$CASE_DIR/producer"
ARTIFACTS="$CASE_DIR/artifacts"
BUILD="$CASE_DIR/build"
mkdir "$ARTIFACTS"
git worktree add --detach "$PRODUCER" ACCEPTED_MAIN_SHA
cd "$PRODUCER"
uv run --project packages/python python -m tools.development sync all
GITHUB_REPOSITORY=gibli-labs/typeflux uv run --project packages/python python \
  -m tools.development.release_evidence --output "$CASE_DIR/accepted-main.json"
uv build --project packages/python --wheel --out-dir "$ARTIFACTS"
for package in @typeflux/temporal @typeflux/yaml @typeflux/temporal-worker \
  @typeflux/temporal-yaml @typeflux/temporal-controlplane; do
  pnpm --filter "$package" pack --pack-destination "$ARTIFACTS"
done
(cd clients/typescript && npm pack --pack-destination "$ARTIFACTS")
npm run --prefix clients/console build
COPYFILE_DISABLE=1 tar -C clients/console/dist -czf "$ARTIFACTS/console-dist.tar.gz" .
git status --short
cd "$OBSERVER"
uv run --project packages/python python -m tools.development.deployed_review.prepare \
  --root "$PRODUCER" --artifacts "$ARTIFACTS" --destination "$BUILD" \
  --accepted-main "$CASE_DIR/accepted-main.json" --inventory-output "$CASE_DIR/inventory.json"
```

The producer must remain clean. `prepare` refuses an existing destination or
inventory, and the receipt must match its exact producer/source inputs. It copies
application fixtures and lockfiles into the build context, without SDK source or
editable installation. The wheel and all npm train dependencies are installed from
the actual candidate files. The Dockerfiles currently name the 0.1.0 SDK train;
review their names and pins when testing a later train, rather than silently mixing
versions. The generated client has its own version. Image IDs bind the actual
resolved dependency closure; retain image build logs and lockfiles for diagnosis.

Build only local images with the pinned Dockerfiles. Record resolved immutable IDs,
not mutable tags, for all five entries in `images.json`:

```sh
docker --context desktop-linux build --target base -f "$BUILD/python.Dockerfile" \
  --iidfile "$CASE_DIR/python-base.id" "$BUILD"
docker --context desktop-linux build --target temporal -f "$BUILD/python.Dockerfile" \
  --iidfile "$CASE_DIR/python.id" "$BUILD"
docker --context desktop-linux build --target base -f "$BUILD/node.Dockerfile" \
  --iidfile "$CASE_DIR/node-base.id" "$BUILD"
docker --context desktop-linux build --target temporal -f "$BUILD/node.Dockerfile" \
  --iidfile "$CASE_DIR/node.id" "$BUILD"
docker --context desktop-linux build -f "$BUILD/console.Dockerfile" \
  --iidfile "$CASE_DIR/console.id" "$BUILD"
python3 - "$CASE_DIR" <<'PY'
import json, pathlib, subprocess, sys
directory = pathlib.Path(sys.argv[1])
images = {}
for name in ["python-base", "python", "node-base", "node", "console"]:
    reference = (directory / (name + ".id")).read_text().strip()
    info = json.loads(subprocess.check_output([
        "docker", "--context", "desktop-linux", "image", "inspect", reference
    ]))[0]
    images[name] = info["Id"]
(directory / "images.json").write_text(json.dumps(images, indent=2) + "\n")
PY
```

These commands do not publish images. The Temporal dev-server digest is pinned in
the controller. Use a local context and inspect its selected engine/resources before
running; this recipe does not attach to a host's existing Temporal service.

## Run and recover

```sh
uv run --project packages/python python -m tools.development.deployed_review.run \
  --context desktop-linux --prepared "$BUILD" --inventory "$CASE_DIR/inventory.json" \
  --accepted-main "$CASE_DIR/accepted-main.json" --images "$CASE_DIR/images.json" \
  --console-modules "$OBSERVER/clients/console" --output "$CASE_DIR/attempt-1"
```

Run normal Python, without `-O`; stripped assertions cannot verify a journey.
The controller refuses an existing output directory. `report.json` can pass only
after assertions and owned cleanup complete. Inspect the full report, intermediate
edition observations and native event counts, rather than only the process exit.
Each new attempt retains its own frozen manifest and failure. A failed mutation is
never automatically repeated. Correct a demonstrated defect, freeze new inputs and
use another output directory; do not relabel a failed run after a later success.

`resources.json` records pending creates before effects, then verified names, IDs
and a `devloop.deployed-review` ownership label. Ambiguous creation is inspected once
for cleanup and the attempt still fails. Teardown verifies ID, name and label before
stopping/removing anything. It never kills by port or removes an unowned object.
After a killed controller or incomplete cleanup, reconcile that exact ledger and
Docker inspection before any manual action; do not blindly repeat create/start or
run a broad prune. Preserve the failed report and unresolved ownership as unknown.

Raw diagnostic logs remain local and may contain machine paths or fixed fixture
payloads. Publish only reviewed source/graph/artifact identities, safe operational
observations and explicit gaps. No runtime `typeflux.*` metadata or search tags are
added. Existing typed contributors and application execution manifests remain in
use; CI, client, resource and recovery observations stay in separate evidence.

This target tests one local Docker host, a dev server and the stated compatible
interpreter/application versions. It does not establish cloud HA, production
failover, arbitrary code upgrade compatibility, or exactly-once unacknowledged
external effects. An image rollback alone cannot recover in-flight workflows.
Required CI, exact-main release evidence and protected publication/deployment
controls remain unchanged. Unit tests and ordinary isolated Temporal CI do not
substitute for this separately observed deployment.

## Observed result

Attempt 8 completed on **2026-11-06 UTC** using clean main
`a2143eac495f60a59843e18aaef7d898abba231c`, accepted CI
[42161046580, attempt 1](https://github.com/gibli-labs/typeflux/actions/runs/42161046580),
and source inputs `8aa764425016639b4816a02c233c9fe2f3691361d7b360eca622ce407b2142b3`.
The [reviewed evidence record](verification/deployed-review-docker-2026-11-06.json)
binds the graph snapshot, all eight actual artifacts, five resolved image IDs,
observer bytes, native observations and retained raw-record hashes. The target was
local Docker 29.6.1 on Linux aarch64, 10 CPUs and 8,321,515,520 bytes of memory;
Temporal CLI 1.7.1 ran Server 1.31.0. Nothing was published to a registry or cloud.

| Observation | Python | TypeScript |
| --- | --- | --- |
| Packaged base Local CLI/SDK, Temporal absent, network disabled | Passed | Passed |
| Installed native SDK and generated client, real API enforcement | Passed | Passed |
| Original gate, worker kill, persistent-service restart, same native run/history | Passed | Passed |
| Paused backend with worker stopped: HTTP 503 | 3.023 s | 3.017 s |
| Reachable backend without worker: HTTP 503 | 3.054 s | 3.053 s |
| Built console: one approval, observed completion, no response fulfillment | Passed | Passed |
| Original run: 1 acknowledged activity before interruption; 3 completed activities, 1 review signal afterward; compatible replay | Passed | Passed |
| ID reuse: new run has zero signals before its separate deliberate decision | Passed | Passed |
| Real accepted 204 discarded: one signal and completion, zero resubmissions | Passed | Passed |
| Existing five-second activity retry, attempts 1 then 2 | 5.041 s | 5.035 s |
| Changed graph with new version: 4 completed activities; incompatible frozen version refused | Passed | Passed |
| Old version observed running before drain, zero running before graceful worker stop | Passed | Passed |

All **71 owned resources** were removed: 66 containers, 3 volumes and 2 networks.
The final pass was written only after assertions, byte-stability checks and cleanup.
The deployment observer did not change after the run; this result section and
evidence record were added afterward. A new candidate or observer requires a new
frozen attempt; this record does not automatically validate later revisions.

Attempts 1–7 and a separate failed native diagnostic remain failed. Early attempts
exposed setup/fixture errors; attempt 6 found the actual scoped-console HTTP/1
transport defect, fixed in [PR #160](https://github.com/gibli-labs/typeflux/pull/160).
Attempt 7 completed Python checks but found TypeScript's native no-poller status
mapping, fixed in [PR #162](https://github.com/gibli-labs/typeflux/pull/162). The record
retains each failure category; no partial attempt was promoted to a pass.

This is one full passing rehearsal with fake providers, fixed inputs and a single
local dev server. It establishes the stated in-flight recovery and drain behavior,
not a recovery SLO, cloud HA or arbitrary code compatibility. The console uses the
current run by execution ID; native probes separately pin and reconcile run IDs.
The old-closed-run rejection is generic HTTP 500 (non-JSON in Python, `HTTPError` in
TypeScript), so its native cause remains unknown from the wire. Terminal history,
zero new-run signals and the later deliberate successful decision establish the
reported boundary. No general exactly-once external-effect guarantee is inferred.
