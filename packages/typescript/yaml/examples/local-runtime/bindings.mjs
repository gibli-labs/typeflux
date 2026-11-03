import { z } from "zod";
import { FakeProvider } from "@typeflux/temporal/testing";

// Compiled JavaScript is the same bindings convention used by the YAML worker.
export const schemas = {"models:Value": z.object({text: z.string()})};
export const provider = new FakeProvider([{text: "local result"}]);
