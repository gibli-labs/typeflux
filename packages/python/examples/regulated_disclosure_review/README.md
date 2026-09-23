# Regulated Disclosure Review Example

Showcases the `regulated` project policy end to end: an **OpenAI**-backed
review-gated workflow with **Langfuse observability + redaction** and a
**fail-closed** human review gate.

The workflow mirrors the lifecycle-review topology — assess → package →
mandatory review → routed finalize/route/compliance — but is configured to
satisfy `regulated`:

- provider `openai` (`gpt-4.1`), which the `regulated` policy allows;
- `observability: langfuse` with `redaction.enabled` and
  `preserve_typeflux_metadata`;
- `lifecycle.review.invalid_user_decision: fail` with routed decisions
  (`require_review_routes`);
- Temporal Cloud TLS + API key + `us-east` region supplied by the
  `temporal_cloud_dev` environment.

The project manifest pins a `temporal_cloud_regulated` validation target
(this workflow, `temporal_cloud_dev`, policy `regulated`); `project
validate --environment temporal_cloud_dev` admits it under `regulated`,
and the console's Policies explorer shows `regulated` used by this
workflow. Under `local` it resolves with the `base` policy like the other
examples (observability is overridden to `none` there).
