# ecommerce

A self-contained e-commerce application used as a **system under test** for the
AIOps agents in this repository. It ships with deliberate, toggle-able failure
modes so the agents (RCA, remediation, resolution verification, etc.) can be
exercised against realistic incidents.

This folder is fully standalone. It does **not** modify `agents/`, `aiops/`, or
`demo/otel-demo/`. It is wired to the agents later, only through the central
observability stack and the `scenarios/` + `truth_files/` it exposes here.

## Architecture

```
frontend (React)
    |
    v
order-service ──> user-service ──> MySQL
   (Postgres)         │
    |                 v
    └──> payment-service ──> Redis
              │
              v
        mock-payment-gateway   (simulated external processor)
```

| Service                | Stack            | Datastore | Port (host) |
|------------------------|------------------|-----------|-------------|
| frontend               | React            | —         | 3000        |
| user-service           | FastAPI          | MySQL     | 8001        |
| order-service          | FastAPI          | PostgreSQL| 8002        |
| payment-service        | FastAPI          | Redis     | 8003        |
| mock-payment-gateway   | FastAPI          | —         | 8004        |

Each service exposes `/metrics` (Prometheus), emits structured JSON logs, and
exports OpenTelemetry traces — the standard interfaces the AIOps stack consumes.

## Quick start

```bash
cp .env.example .env        # then edit if needed
docker compose up --build
```

Then open the frontend at http://localhost:3000 and exercise the full flow:
register → login → create order → payment → order status.

Verify services are healthy:

```bash
curl localhost:8001/health   # user-service
curl localhost:8002/health   # order-service
curl localhost:8003/health   # payment-service
```

## Failure scenarios

Twelve failure modes span the three services. Each is either an environment
toggle (see `.env.example`) or a container action, and each has a matching
`scenarios/*.yaml` definition and expected root cause in `truth_files/*.json`.

| Service         | Failures                                                        |
|-----------------|-----------------------------------------------------------------|
| user-service    | MySQL down · high latency · high CPU · CrashLoopBackOff          |
| order-service   | Postgres down · payment timeout · HTTP 500 · memory leak / OOM  |
| payment-service | Redis down · gateway timeout · high CPU · HTTP 500              |

Injection helpers live in `failure_injection/`, grouped per service. Example:

```bash
# Failure: MySQL down (user-service)
docker compose stop mysql
# ...observe login failures, then recover:
docker compose start mysql
```

## Folder layout

```
ecommerce/
├── frontend/               # React client
├── user-service/           # auth + user management (MySQL)
├── order-service/          # order workflow (PostgreSQL)
├── payment-service/        # payment processing (Redis)
├── mock-payment-gateway/   # simulated external processor
├── failure_injection/      # 12 failure-injection helpers, grouped by service
├── scenarios/              # scenario definitions (one per failure)
├── truth_files/            # expected RCA / ground truth per scenario
├── docker-compose.yml
└── .env.example
```

## Connecting to the AIOps agents

Do this only after the app runs standalone:

1. Add the three services as scrape targets / log sources / trace exporters in
   the repo's existing central observability stack (do **not** stand up a second
   Prometheus/Loki/Tempo here).
2. Namespace the service names distinctly (they are prefixed `ecommerce-*` via
   `OTEL_SERVICE_NAME`) so they don't collide with the OTel Demo's services.
3. Point the agents' scenario runner at this folder's `scenarios/` and
   `truth_files/`.