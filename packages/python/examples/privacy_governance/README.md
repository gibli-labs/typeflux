# Privacy governance example (#188)

One spec that turns on **both** privacy controls from #188 slice 2, plus the policy
that requires them:

- `typeflux.yaml` — an AES-256-GCM `runtime.temporal.payload_codec` (two keys, rotation-ready)
  **and** two `observability.redaction.custom_rules` (a case-reference and a UK NINO pattern)
  appended to the built-in email/SSN/phone/card catalog. The workflow declares
  `risk_tier: human_gated`.
- `policies/regulated.yaml` — requires the payload codec (`runtime.temporal.require_payload_codec`),
  requires the two named custom rules (`observability.redaction.require_custom_rules`), and makes
  the `human_gated` tier imply the codec via the risk-tier macro. All fail-closed at admission;
  `require_*` knobs OR-merge and `require_custom_rules` unions across composed policies.

The codec encrypts the whole Temporal payload at rest (workflow/activity IO, review notes,
inlined artifact bytes); the redaction rules mask PII on the separate observability egress path.
See [`docs/privacy.md`](../../../../docs/privacy.md) for the key model, rotation runbook, threat
model, and the retention/erasure design. Patterns use the **Python `re`** dialect.

Provide the key material (32 raw bytes each) via `TYPEFLUX_PAYLOAD_KEY_2026_Q3` /
`TYPEFLUX_PAYLOAD_KEY_2026_Q2` before enabling the codec against a live worker.
