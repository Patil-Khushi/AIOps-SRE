# failure_injection

Toolkit to inject and recover the 12 failure modes of the ecommerce demo, and
to drive the traffic that makes some of them observable. Pure standard library
(no third-party deps).

Run from the `ecommerce/` folder (the one containing `docker-compose.yml`).

## Commands

```bash
# list every failure
python -m failure_injection list

# inject / recover
python -m failure_injection inject  user_service.mysql_down
python -m failure_injection recover user_service.mysql_down

# inject and immediately drive traffic for 30s (for CPU/latency/OOM)
python -m failure_injection inject payment_service.high_cpu --load 30

# see the expected L1 / L2 / RCA signals for a failure
python -m failure_injection signals order_service.payment_timeout

# ad-hoc load against any URL
python -m failure_injection load --url http://localhost:8002/orders \
    --method POST --duration 20
```

Set `FI_DRY_RUN=1` to print the docker commands **without executing them** — a
safe way to preview exactly what an injection will do:

```bash
FI_DRY_RUN=1 python -m failure_injection inject order_service.memory_leak_oom
```

## How it works

Two mechanisms cover all 12 failures:

* **Container stop/start** — the datastore-down failures (`mysql_down`,
  `postgres_down`, `redis_down`) just stop the container and start it again.
* **Compose override** — env-toggle, config, and resource failures write a
  small `docker-compose.faults.yml`, layer it over the base compose, and
  force-recreate the one affected service so it picks up the injected settings.
  Recovery deletes the override and recreates the service from the base compose.

Only one override-based failure should be active at a time (they share the
single override file). The stop/start failures are independent.

## The timeout chain

`order_service.payment_timeout` and `payment_service.gateway_timeout` both work
by slowing the **mock gateway** (`INJECT_DELAY_SECONDS=30`). The same slow
gateway trips a 504 at both the order layer and the payment layer, because 30s
exceeds both `PAYMENT_TIMEOUT_SECONDS` (order) and `GATEWAY_TIMEOUT_SECONDS`
(payment). Which one you *observe* depends on whether you drive `/orders` or
`/payments`.

## The 12 failures

| Key | Mechanism |
|-----|-----------|
| user_service.mysql_down | stop mysql |
| user_service.high_latency | env INJECT_LATENCY_SECONDS=10 |
| user_service.high_cpu | env INJECT_CPU_LOAD=true (+load) |
| user_service.crashloop | bad MYSQL_HOST + restart=on-failure |
| order_service.postgres_down | stop postgres |
| order_service.payment_timeout | slow gateway (30s) |
| order_service.http_500 | env INJECT_HTTP_500=true |
| order_service.memory_leak_oom | env INJECT_MEMORY_LEAK=true + mem_limit 256m (+load) |
| payment_service.redis_down | stop redis |
| payment_service.gateway_timeout | slow gateway (30s) |
| payment_service.high_cpu | env INJECT_CPU_LOAD=true (+load) |
| payment_service.http_500 | env INJECT_HTTP_500=true |