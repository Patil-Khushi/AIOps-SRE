"""Order Service — order management & orchestration.

Endpoints:
    POST /orders          create an order (validates user -> pays -> updates status)
    GET  /orders          list the caller's orders
    GET  /orders/{id}     single order status
    GET  /health          liveness + Postgres reachability
    GET  /metrics         Prometheus exposition

Calls the User Service (validation) and Payment Service (charge). With an OTLP
endpoint set, FastAPI + httpx are auto-instrumented so the order->payment call
appears as a linked span in Tempo.
"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .db import postgres_client as db
from .observability.logging_config import log
from .routes import create_order, get_orders, order_status


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("order-service starting")
    try:
        db.init_schema()
    except Exception as exc:  # noqa: BLE001
        log.error("startup schema init failed", extra={"error": str(exc)})
        raise
    db.ping()
    yield


app = FastAPI(title="ecommerce-order-service", lifespan=lifespan)

# See user-service/src/main.py — the SPA calls this service cross-origin from
# :3000 with a JSON content type and an Authorization header, both of which
# force a CORS preflight. Without this, the browser blocks checkout entirely.
_cors_origins = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(create_order.router)
app.include_router(get_orders.router)
app.include_router(order_status.router)


@app.get("/health")
def health():
    ok = db.ping()
    return {"status": "ok" if ok else "degraded", "postgres": ok}


@app.get("/metrics")
def metrics():
    db.ping()
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
            {"service.name": os.getenv("OTEL_SERVICE_NAME", "ecommerce-order-service")}
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=_otlp)))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app)
        HTTPXClientInstrumentor().instrument()  # propagates trace context downstream
        log.info("otel tracing enabled", extra={"endpoint": _otlp})
    except Exception as exc:  # noqa: BLE001
        log.warning("otel init failed; continuing without tracing", extra={"error": str(exc)})