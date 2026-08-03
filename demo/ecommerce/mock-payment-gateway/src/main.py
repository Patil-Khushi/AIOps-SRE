"""Mock Payment Gateway.

Stands in for an external payment processor. Deliberately tiny. Its only
interesting behavior is a configurable delay, which is what drives:
    - Order Service Failure 2 (payment timeout), when the delay exceeds the
      order service's PAYMENT_TIMEOUT_SECONDS, and
    - Payment Service Failure 2 (gateway timeout), when it exceeds the payment
      service's GATEWAY_TIMEOUT_SECONDS.

Endpoints:
    POST /charge   { amount } -> { status: "approved", txn_id }
    GET  /health
"""
import os
import time
import uuid

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="mock-payment-gateway")


class ChargeRequest(BaseModel):
    amount: float
    order_id: int | None = None


@app.post("/charge")
def charge(req: ChargeRequest):
    # Simulate a slow external processor when configured.
    try:
        delay = float(os.getenv("INJECT_DELAY_SECONDS", "0"))
    except ValueError:
        delay = 0
    if delay > 0:
        time.sleep(delay)

    return {"status": "approved", "txn_id": f"txn_{uuid.uuid4().hex[:12]}", "amount": req.amount}


@app.get("/health")
def health():
    return {"status": "ok"}