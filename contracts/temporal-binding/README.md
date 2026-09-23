# Temporal binding profiles

[`binding.v1.json`](binding.v1.json) is the **normative** description of the
Temporal wire conventions a control plane relies on to operate a Typeflux
execution (epic
[#618](https://github.com/gibli-labs/typeflux-temporal/issues/618)): what gets
registered, how a start looks, which memo keys carry identity, and the
signal/query surface for lifecycle operations. From this document on,
divergence between an SDK and its written profile is a bug in the SDK.

## The two profiles, and why there are two

Per [#563](https://github.com/gibli-labs/typeflux-temporal/issues/563) the
Python and TypeScript execution ABIs **deliberately and permanently diverge**:

- **`python-versioned-type`** — Python generates one workflow class per spec
  and registers it under a versioned type name
  (`{workflow.name}.{version|digest12}`). Identity and frozen-version live in
  the type name; a start passes only the input.
- **`ts-plan-argument`** — TypeScript registers one generic
  `typefluxYamlWorkflow` and passes the resolved plan as the first start
  argument. Identity and frozen-version live in the memo
  (`typeflux_workflow_version`); enforcement happens at start time because
  nothing spec-specific is ever registered.

This divergence is **not fought, it is described**: each choice is
load-bearing for its edition's replay model. Described normatively, a control
plane operates either edition by selecting a **binding driver** per project
(the driver seam is slice 3; the registry `runtime:` field is the
resolver-seam epic, #619).

## The shareable surface

Everything else is deliberately identical across editions and lives under
`shared` in the document: the three identity memo keys, the
`typeflux_request_cancel` / `typeflux_submit_review` signals, the
`typeflux_lifecycle_status` query and its snake_case
`WorkflowLifecycleStatus` shape, the `ReviewCommand` payload, and the
keyword search-attribute convention. Payloads are contract-equal
**structurally** (decoded JSON), not byte-for-byte — Python serializes via
the pydantic data converter, TS via the default converter.

The `typeflux_spec_digest` values are **profile-opaque**: the two editions
hash different artifacts (the generated command graph vs the interpreted
plan) with different algorithm identifiers, by design. A control plane never
compares digests across profiles.

The lifecycle surface is **always answerable in both editions** (unified in
slice 2): handlers are registered unconditionally, a lifecycle-less
workflow answers `state: "disabled"`, and the signals no-op. Binding
**verification** (the type/memo identity check before lifecycle dispatch,
409 on mismatch) is an obligation on the *operator*, implemented today by
the Python runtime; every binding driver must implement it per its
profile's rules.

## Machine-readable on purpose

The document is JSON so each SDK's conformance tests assert their exported
constants **against it** (signal/query names, memo keys, type-name rules —
epic slice 2), the same spec-model-is-wired discipline as the rest of
`contracts/`. It is registered as an **interface contract** in
[`conformance.json`](../conformance.json) and covered by the suite-integrity
gates on both SDKs.

## Change process

A change to this document is an interface change reviewed as one. Adding to
the shared surface requires landing the identical convention in **both**
SDKs with the conforming change; profile sections change only when the
corresponding edition's ABI deliberately changes (a new frozen-version
mechanism, a new start-args shape), which per #563 is expected to be never.
