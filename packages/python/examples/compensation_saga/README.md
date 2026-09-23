# Compensation saga (idempotent external activity)

A booking saga (#299) that demonstrates two of the compensation patterns from
[docs/yaml.md → Compensation and rollback](../../../../docs/yaml.md#compensation-and-rollback-sagas):

- **First-class compensation.** Each side-effecting step declares how to undo itself with
  `compensate:`. If a later step fails, the interpreter runs the recorded compensations in
  **reverse** (refund the charge, then cancel the booking) before re-raising the original
  failure. `book_room` → `charge_card` → `fulfill_order`; a failed `fulfill_order` unwinds
  `charge` then `book`.
- **Idempotent external activity.** `book_room` and `charge_card` are marked
  `side_effecting: true` and thread a caller-supplied `idempotency_key`. Because both a
  retry and the best-effort compensation unwind can run a side-effecting activity more than
  once, an external write should be keyed so a duplicate delivery is a no-op instead of a
  double charge. The scripted provider models a **genuinely idempotent** external system (a
  dedup ledger keyed on the idempotency key), so a repeated request returns the original
  result and performs no second effect — `tests/test_saga.py` proves a repeated charge is a
  no-op (`external_writes == 1`).

The terminal `fulfill_order` is deliberately **not** side-effecting (it assembles the
result, it does not write to the world), so `require_compensation` does not demand a
compensate on it.

## Governance

`workflow.risk_tier: human_gated` opts into governance. Under a policy whose `human_gated`
tier sets `require_compensation: true`, admission passes only because every `side_effecting`
activity STEP (`book`, `charge`) declares `compensate:`. Drop either and admission fails
under `policy_risk_tier` naming `require_compensation`.

## Run it

```bash
cd packages/python
temporal server start-dev            # in another shell
uv run python -m examples.compensation_saga.main run
```

The demo runs the happy path against a real dev server with a scripted provider (no API
key): all three side-effecting steps are recorded on the compensation LIFO but never
unwound because nothing fails. The reverse-order unwind on failure is proven by
`tests/test_saga.py` and the slice-1 replay fixtures.

## Tests

`tests/test_saga.py` asserts the spec shape, `require_compensation` admission (passing here,
failing when a compensate is dropped), the reverse-order unwind when the finalizer fails
(`compensation_status: complete`), idempotency (a repeated external write is a no-op), and
side-effecting digest invariance.
