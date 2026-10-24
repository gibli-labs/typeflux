# Diagnostic evaluation and remaining study

On 2026-10-24, the twelve curated tasks in `navigation.json` retrieved their
expected file in 12/12 bounded graph queries and 10/12 bounded lexical file
searches. The lexical misses were the Python HTTP review surface and authorization
documentation. Reproduce with `python -m tools.project_graph benchmark`; the output
includes revision/digest, each expected file, hits, context counts and timing.

These are deliberately small, authored navigation probes. The graph authors knew
the repository and target capability. Search uses one regular expression without
interactive refinement; graph output includes non-file entities in the same item
budget. This result is not an unbiased agent comparison, a cost/productivity
measurement, or permission to skip CI. Impact correctness is separately exercised
with deleted/renamed files, changed contracts/configuration, unknown mappings,
dependency cycles and published-versus-workspace dependencies.

The subsequent matched development study remains pending an agreed model and
budget. Build 30–50 tasks stratified across SDKs, contracts, console, documentation
and deployment. Freeze task inputs, tool/model versions, resource/time/spend caps
and held-out tests before trials. Compare existing tools, graph queries and bounded
context assembly with repeated matched runs. Include sequential changes to the
same implementation and interruption/resume cases. Report failures, infrastructure
noise, human corrections, cost and uncertainty alongside successes. Do not give
historical tasks descriptors or solutions authored after their starting revision.
