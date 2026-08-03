"""Request/response schemas for the Order Service."""
from typing import Any

from pydantic import BaseModel


class OrderItem(BaseModel):
    sku: str
    qty: int
    price: float


class CreateOrderRequest(BaseModel):
    items: list[OrderItem]
    amount: float


class OrderResponse(BaseModel):
    id: int
    user_id: int
    amount: float
    status: str          # PENDING | PAID | FAILED
    items: Any = None
    created_at: str | None = None