"""Payment Service — payment processing & external dependency.

Endpoints:
    POST /payments        charge for an order (gateway -> Redis)
    GET  /payments/{id}   payment status
    GET  /health          liveness + Redis reachability
    GET  /metrics         Prometheus exposition
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .db import redis_client as store
from .observability.logging_config import log
from .routes import create_payment, payment_status


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("payment-service starting")
    store.ping()  # non-fatal: Redis may come up slightly after us
    yield


app = FastAPI(title="ecommerce-payment-service", lifespan=lifespan)

app.include_router(create_payment.router)
app.include_router(payment_status.router)


@app.get("/health")
def health():
    ok = store.ping()
    return {"status": "ok" if ok else "degraded", "redis": ok}


@app.get("/metrics")
def metrics():
    store.ping()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --- Optional OpenTelemetry tracing ---------------------------------------
_otlp = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
if _otlp:
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {"service.name": os.getenv("OTEL_SERVICE_NAME", "ecommerce-payment-service")}
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=_otlp)))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app)
        HTTPXClientInstrumentor().instrument()
        log.info("otel tracing enabled", extra={"endpoint": _otlp})
    except Exception as exc:
        log.warning("otel init failed; continuing without tracing", extra={"error": str(exc)})
