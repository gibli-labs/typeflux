/**
 * The two typed activities the code-defined `reviewWorkflow` calls. Their NAMES
 * (`classifyDisclosure` / `substantiateClaim`) match the `proxyActivities`
 * contract in the shipped workflow module, so the worker registers them under
 * the names the workflow proxies.
 */

import { defineActivity } from "@typeflux/temporal";

import { ClaimInput, Classification, DisclosureInput, Substantiation } from "./schemas.js";

export const classifyDisclosure = defineActivity({
  name: "classifyDisclosure",
  prompt: { name: "review/classify", label: "production" },
  input: DisclosureInput,
  output: Classification,
});

export const substantiateClaim = defineActivity({
  name: "substantiateClaim",
  prompt: { name: "review/substantiate", label: "production" },
  input: ClaimInput,
  output: Substantiation,
});
