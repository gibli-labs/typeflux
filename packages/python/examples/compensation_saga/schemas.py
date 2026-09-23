from __future__ import annotations

from pydantic import BaseModel, Field


class OrderRequest(BaseModel):
    """The saga input. ``idempotency_key`` is caller-supplied and threaded into every
    side-effecting external call so a retry (or a best-effort compensation retry) is a
    no-op instead of a double effect (#299)."""

    order_id: str = Field(description="Business order identifier.")
    idempotency_key: str = Field(description="Caller-supplied key for idempotent external writes.")
    room: str = Field(description="What to book.")
    amount: float = Field(description="Amount to charge.")


class Booking(BaseModel):
    order_id: str
    confirmation_id: str
    idempotency_key: str


class ChargeResult(BaseModel):
    order_id: str
    charge_id: str
    idempotency_key: str
    amount: float


class CancelResult(BaseModel):
    order_id: str
    cancelled: bool


class RefundResult(BaseModel):
    order_id: str
    refunded: bool


class Fulfillment(BaseModel):
    order_id: str
    fulfilled: bool
