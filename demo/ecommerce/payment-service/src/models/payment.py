"""Request/response schemas for the Payment Service."""

from pydantic import BaseModel


class PaymentRequest(BaseModel):
    order_id: int
    amount: float


class PaymentResponse(BaseModel):
    id: str
    order_id: int
    amount: float
    status: str  # PAID | FAILED
    txn_id: str | None = None
