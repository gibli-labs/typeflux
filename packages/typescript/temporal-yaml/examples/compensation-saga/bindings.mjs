// Safe fake counterpart of the Python booking/charge example; no external writes.
import {z} from "zod";
import {ProviderTransientError} from "@typeflux/temporal";
import {currentActivityContext} from "@typeflux/temporal-worker";

export const schemas = {
  "schemas:OrderRequest": z.object({order_id: z.string(), idempotency_key: z.string(), room: z.string(), amount: z.number()}),
  "schemas:Booking": z.object({order_id: z.string(), confirmation_id: z.string(), idempotency_key: z.string()}),
  "schemas:ChargeResult": z.object({order_id: z.string(), charge_id: z.string(), idempotency_key: z.string(), amount: z.number()}),
  "schemas:CancelResult": z.object({order_id: z.string(), cancelled: z.boolean()}),
  "schemas:RefundResult": z.object({order_id: z.string(), refunded: z.boolean()}),
  "schemas:Fulfillment": z.object({order_id: z.string(), fulfilled: z.boolean()}),
};
export const input = {order_id: "ORDER-1", idempotency_key: "idem-ORDER-1", room: "Deluxe King", amount: 100};

export class AdoptionProvider {
  providerName = "fake";
  calls = [];
  forwardFakeWrites = {book_room: 0, charge_card: 0};
  constructor(scenario) { this.scenario = scenario; }
  structuredCall(params) {
    const properties = params.outputSchema.properties;
    const action = "confirmation_id" in properties ? "book_room"
      : "charge_id" in properties ? "charge_card"
      : "fulfilled" in properties ? "fulfill_order"
      : "refunded" in properties ? "refund_card" : "cancel_room";
    const attempt = currentActivityContext().attempt;
    this.calls.push([action, attempt]);
    if (action === "fulfill_order") throw new ProviderTransientError("demonstration forward failure; fake effects only");
    if (action === "refund_card" && (this.scenario === "partial" || (this.scenario === "replacement" && attempt === 1))) {
      throw new ProviderTransientError("demonstration compensation failure; fake effects only");
    }
    if (action === "book_room") {
      this.forwardFakeWrites.book_room++;
      return {order_id: "ORDER-1", confirmation_id: "CONF-1", idempotency_key: "idem-ORDER-1"};
    }
    if (action === "charge_card") {
      this.forwardFakeWrites.charge_card++;
      return {order_id: "ORDER-1", charge_id: "CHG-1", idempotency_key: "idem-ORDER-1", amount: 100};
    }
    return action === "refund_card" ? {order_id: "ORDER-1", refunded: true} : {order_id: "ORDER-1", cancelled: true};
  }
}
