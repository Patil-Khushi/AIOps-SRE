"""User Service — authentication & user management.

Endpoints:
    POST /register   create a user (MySQL)
    POST /login      authenticate -> JWT
    GET  /profile    current user (JWT-protected)
    GET  /health     liveness + MySQL reachability
    GET  /metrics    Prometheus exposition

Observability: Prometheus metrics, structured JSON logs, optional OTel traces.
Failure modes are driven by env toggles (see .env.example).
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .db import mysql_client as db
from .observability.logging_config import log
from .routes import login, profile, register


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A bad MYSQL_HOST / missing env var makes this fail on boot -> the
    # container never becomes healthy (Failure 4: CrashLoopBackOff).
    log.info("user-service starting")
    try:
        db.init_schema()
    except Exception as exc:
        log.error("startup schema init failed", extra={"error": str(exc)})
        raise
    db.ping()
    yield


app = FastAPI(title="ecommerce-user-service", lifespan=lifespan)

# The SPA is served from a different origin (:3000 in Docker, :5173 under
# `npm run dev`) and sends Content-Type: application/json, which makes the
# browser issue a CORS preflight. Without this middleware the preflight gets
# 405 and the browser blocks every call — even though the API itself is fine.
# Bearer tokens travel in the Authorization header, not cookies, so
# allow_credentials is deliberately left off (it cannot be combined with "*").
_cors_origins = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(register.router)
app.include_router(login.router)
app.include_router(profile.router)


@app.get("/health")
def health():
    ok = db.ping()
    return {"status": "ok" if ok else "degraded", "mysql": ok}


@app.get("/metrics")
def metrics():
    # Refresh the DB gauge at scrape time so it reflects current reality.
    db.ping()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --- Optional OpenTelemetry tracing ---------------------------------------
# Instruments FastAPI only when an OTLP endpoint is configured, so local runs
# without a collector are unaffected.
_otlp = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
if _otlp:
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {"service.name": os.getenv("OTEL_SERVICE_NAME", "ecommerce-user-service")}
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=_otlp)))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app)
        log.info("otel tracing enabled", extra={"endpoint": _otlp})
    except Exception as exc:
        log.warning("otel init failed; continuing without tracing", extra={"error": str(exc)})
