# Privacy governance example (#188, TypeScript)

The TypeScript mirror of `packages/python/examples/privacy_governance`. One spec that
turns on **both** privacy controls from #188 slice 2, plus the policy that requires them:

- `typeflux.yaml` — an AES-256-GCM `runtime.temporal.payload_codec` (two keys, rotation-ready)
  **and** two `observability.redaction.custom_rules` (a case-reference and a UK NINO pattern)
  appended to the built-in email/SSN/phone/card catalog. The workflow declares
  `risk_tier: human_gated`.
- `policies/regulated.yaml` — requires the payload codec (`runtime.temporal.require_payload_codec`),
  requires the two named custom rules (`observability.redaction.require_custom_rules`), and makes
  the `human_gated` tier imply the codec via the risk-tier macro. All fail-closed at admission;
  `require_*` knobs OR-merge and `require_custom_rules` unions across composed policies.
- `schemas.ts` — the zod schema resolver the runtime injects for `schemas:*` references.

The codec encrypts the whole Temporal payload at rest; the redaction rules mask PII on the
separate observability egress path. See [`docs/typescript/privacy.md`](../../../../../docs/typescript/privacy.md)
for the key model, rotation runbook, and threat model. Patterns use the **JS `RegExp`** dialect —
the identical YAML is portable to the Python edition, which parses them with Python `re`.
