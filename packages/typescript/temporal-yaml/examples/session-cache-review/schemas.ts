/** Schemas for the session-cache showcase: a map over items that share a large
 *  stable prefix (the system instructions), so a per-fan-out session cache pays off. */

import { z } from "zod";

export const Item = z.object({
  id: z.string().describe("Stable item id."),
  text: z.string().describe("The per-item content (the part that varies)."),
});
export type Item = z.infer<typeof Item>;

export const BatchInput = z.object({
  items: z.array(Item).min(1),
});
export type BatchInput = z.infer<typeof BatchInput>;

export const Review = z.object({
  id: z.string(),
  ok: z.boolean(),
});
export type Review = z.infer<typeof Review>;

export const Batch = z.object({
  reviews: z.array(Review),
});
export type Batch = z.infer<typeof Batch>;

export const schemas = {
  "schemas:Item": Item,
  "schemas:BatchInput": BatchInput,
  "schemas:Review": Review,
  "schemas:Batch": Batch,
};
