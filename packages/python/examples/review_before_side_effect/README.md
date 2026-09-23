# Review before side effect

The safest compensation pattern (#299): never take an irreversible action unattended. A
human **approves before** the side-effecting disbursement, not after — see
[docs/yaml.md → Review before side effect](../../../../docs/yaml.md#review-before-side-effect-the-safest-pattern).

```
assess  ──▶  [ human review gate ]  ──approve──▶  disburse (side_effecting, compensate: reverse_payment)
```

- **Prevention.** The `lifecycle.review` gate fires `after_step: assess` and routes
  `approve` → `disburse`, so the side-effecting payment runs only once a human approves.
- **Recovery net.** `disburse_payment` still declares `compensate: reverse_payment` for the
  failures no gate can foresee (a downstream error after the transfer). The disbursement is
  idempotency-keyed and the fake external system dedups on that key, so a retry never
  double-pays — `tests/test_review_before_side_effect.py` proves a repeated disburse is a
  no-op (`transfers_performed == 1`).

## Governance

`workflow.risk_tier: human_gated` opts into governance. Under a policy whose `human_gated`
tier sets `require_compensation: true`, admission passes because the one `side_effecting`
step (`disburse`) declares `compensate:`; drop it and admission fails under
`policy_risk_tier` naming `require_compensation`.

## Run it

```bash
cd packages/python
temporal server start-dev            # in another shell
uv run python -m examples.review_before_side_effect.main run
```

The demo pauses at the review gate (before any side effect), approves, then disburses —
against a real dev server with a scripted provider (no API key).

## Tests

`tests/test_review_before_side_effect.py` asserts the gate precedes the side-effecting step,
`require_compensation` admission (passing here, failing when the compensate is dropped), that
the disbursement only runs after review approval, and that a repeated disburse is idempotent
(one real transfer).
