# Compliance Readiness

This document maps the deployment requirements of regulated buyers (financial
services, healthcare, insurance, legal, public sector) to concrete Typeflux
features and to the certifications those features support.

It is a readiness map, not a claim of compliance. Typeflux is an early
experiment. Status is marked explicitly per row:

- **Shipped** — exists today.
- **Partial** — foundation exists, gaps remain.
- **Gap** — designed or discussed, not built.

The throughline: a small set of features (encryption, redaction, immutable
audit/provenance, RBAC/SSO, residency) satisfies most certifications at once.
The genuinely new category for agent-authored and autonomous workflows is AI
governance (ISO/IEC 42001, EU AI Act), which governs the autonomy itself.

> Keep this matrix and the [Production Readiness](production-readiness.md)
> checklist in sync — they cover the same features for different readers, and a
> status change in one is a status change in both.

## Feature → Certification Matrix

| Feature | Status | Certifications / regimes it supports |
| --- | --- | --- |
| Regex PII redaction before observability egress (email, phone, SSN, Luhn-validated card) | Shipped | PCI DSS (scope reduction), HIPAA, GDPR, CCPA |
| Execution manifests + code/schema/prompt provenance | Shipped | SOX, SOC 2, ISO 27001, EU AI Act (logging) |
| Trace list / search / inspect / diff / export | Shipped | SOC 2, SOX, audit evidence generally |
| Typed contracts + validation-repair retries | Shipped | EU AI Act (accuracy/robustness), general assurance |
| Durable execution, retries, replay (Temporal) | Shipped | SOC 2 (availability), operational resilience |
| Configurable PII catalog beyond the four default rules; per-jurisdiction rules — [`observability.redaction.custom_rules`](privacy.md#custom-redaction-rules) + `require_custom_rules` policy gate | Shipped | HIPAA, GDPR, IRS Pub 1075, CJIS |
| Temporal payload encryption (data converter) — PII encrypted at rest in history — AES-256-GCM [`payload_codec`](privacy.md#payload-codec--encryption-at-rest) + `require_payload_codec` policy gate | Shipped | PCI DSS, HIPAA, ISO 27001, GDPR |
| In-VPC / non-LLM provider path; prompt-side redaction (not just observability) | Gap | HIPAA (BAA precondition), data residency |
| Data residency / regional routing (provider + Langfuse) | Gap | GDPR, FedRAMP, sector data-localization rules |
| RBAC on the control-plane API + console (built-in bearer tokens, reverse-proxy SSO integration) | Shipped | SOC 2, ISO 27001, FedRAMP |
| Unified SSO across observability (Langfuse) + control surfaces | Partial | SOC 2, ISO 27001, FedRAMP |
| Secret-manager integration (steered toward, not enforced) | Partial | SOC 2, ISO 27001 |
| Data-subject erasure tooling (#715) — [`typeflux erase` + `ErasureReceipt`](privacy.md#retention--erasure): subject identity + index, per-subject crypto-shred + `DeleteWorkflowExecution`, Langfuse trace deletion, cache invalidation, dry-run-first CLI with an auditable receipt; provider logs + exported artifacts remain document-only (owner-erased), retention TTLs remain namespace-admin-owned | Shipped | GDPR (erasure), CCPA-CPRA |
| Risk-tiered governance (safe / policy_gated / human_gated / prohibited) — declared tier + policy floor, fail-closed macro-expansion into existing controls, closure cascade (per-activity tiers deferred) | Shipped | EU AI Act (human oversight), ISO 42001 |
| Saga compensation as a first-class construct — first-class YAML `compensate:` (LIFO unwind on failure AND cancellation), `side_effecting` declaration, `require_compensation` risk-tier macro, `CompensationContributor` manifest evidence | Shipped | EU AI Act (reversibility of automated decisions) |
| Admission gate for agent-authored specs (#298, #300) — `admit_spec`/`AdmissionReport` structural checks, composition step/width/depth ceilings, risk-tier closure cascade; [`project admit` CLI](yaml.md#admission-of-agent-authored--externally-submitted-specs) | Shipped | ISO 42001, EU AI Act |
| Human-approval signals with timeout-to-escalate — lifecycle review gates with [`timeout: { seconds, on_timeout: route }`](yaml.md#lifecycle-queries-cancellation-and-review-gates) | Shipped | EU AI Act (human-in-the-loop), SOX (authorization) |
| Per-execution spec pinning + versioning | Gap | EU AI Act (logging/traceability), reproducibility |
| Authoring provenance (prompt + model + input that generated each spec) | Partial | ISO 42001, EU AI Act (transparency) |
| ETL lineage (source rows → transforms → outputs) | Gap | GDPR (processing records), SOX |

## Certification Notes

**Cross-industry baseline.** SOC 2 Type II and ISO/IEC 27001 gate almost every
enterprise deal. They are satisfied largely by the same controls: encryption,
access control, audit logging, and incident process.

**PCI DSS.** Redaction plus payload encryption is the argument for *reduced
scope* — card numbers never land in workflow history or observability. Luhn
validation before masking avoids over-redacting order and account identifiers.

**HIPAA / HITRUST.** Payload encryption ships (see the matrix); the in-VPC
provider path is the remaining precondition to signing a BAA. Without it, PHI
cannot be processed.

**GDPR / CCPA-CPRA.** Needs DPAs, residency, retention, and data-subject
erasure. Erasure is covered by the shipped #715 tooling (`typeflux erase` +
the `ErasureReceipt` proof); residency and DPAs remain the open items.

**FedRAMP (+ StateRAMP, DoD IL4/5).** Public-sector gate. High bar; usually the
deciding factor. Requires self-hostable / in-boundary deployment of every
component, including the model provider and Langfuse.

**SOX / GLBA.** Financial controls. The immutable audit trail and provenance are
the control evidence. Niche-but-real: IRS Pub 1075 (tax data), CJIS
(criminal-justice data).

**ISO/IEC 42001 + EU AI Act.** The AI-governance category, and the one the
agent-authored direction introduces. Several discussed use cases (credit
scoring, insurance pricing) are EU AI Act **high-risk**, which carries hard
legal obligations: human oversight, logging, transparency, and accuracy. The
oversight side is covered by shipped controls above (risk-tiered governance,
the admission gate, approval signals with timeout-to-escalate, saga
compensation); the remaining gaps are spec pinning/versioning and full
authoring provenance. NIST AI RMF alignment is increasingly requested
in procurement even though it is a framework, not a certification.
