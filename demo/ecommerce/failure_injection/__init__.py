"""Failure-injection toolkit for the ecommerce demo.

Twelve failures across the three services, each with inject()/recover() logic
plus reference L1/L2/RCA signals. Use the CLI:

    python -m failure_injection list [--show-layers]
    python -m failure_injection inject user_service.mysql_down [--mode hybrid]
    python -m failure_injection recover user_service.mysql_down
    python -m failure_injection inject payment_service.high_cpu --load 30
    FI_DRY_RUN=1 python -m failure_injection inject order_service.memory_leak_oom

Injection modes (controlled by --mode flag or FI_MODE env var):
    application     -> Environment variables, ConfigMaps (current)
    infrastructure  -> tc, stress-ng, kubectl, iptables (new)
    hybrid          -> Both layers simultaneously (default)

FI_DRY_RUN=1 prints the docker commands instead of running them.
"""
from ._base import Failure, InjectionLayer, LoadHint
from ._orchestrator import OrchestrationMode, inject, recover
from .infrastructure_layer import disk_full as _i_disk
from .infrastructure_layer import dns_failure as _i_dns
from .infrastructure_layer import memory_exhaust as _i_mem
from .infrastructure_layer import packet_loss as _i_loss
from .infrastructure_layer import pool_exhaustion as _i_pool
from .order_service import http_500 as _o_500
from .order_service import memory_leak_oom as _o_oom
from .order_service import payment_timeout as _o_pt
from .order_service import postgres_down as _o_pg
from .payment_service import gateway_timeout as _p_gt
from .payment_service import high_cpu as _p_cpu
from .payment_service import http_500 as _p_500
from .payment_service import redis_down as _p_redis
from .user_service import crashloop as _u_crash
from .user_service import high_cpu as _u_cpu
from .user_service import high_latency as _u_lat
from .user_service import mysql_down as _u_mysql

# The original twelve. Four of these (high_latency, payment_timeout,
# order http_500, payment high_cpu) now declare layer=HYBRID and carry an
# infrastructure implementation alongside the env-var one.
_APP_FAILURES = [
    _u_mysql.failure, _u_lat.failure, _u_cpu.failure, _u_crash.failure,
    _o_pg.failure, _o_pt.failure, _o_500.failure, _o_oom.failure,
    _p_redis.failure, _p_gt.failure, _p_cpu.failure, _p_500.failure,
]

# Infrastructure-only failures: no env-var equivalent exists, so these run
# nothing under FI_MODE=application.
_INFRA_FAILURES = [
    _i_loss.failure, _i_mem.failure, _i_disk.failure,
    _i_dns.failure, _i_pool.failure,
]

_ALL = [*_APP_FAILURES, *_INFRA_FAILURES]

# Guard the flat namespace: two modules claiming the same key would silently
# shadow each other here, and the loser would be unreachable from the CLI,
# dashboard and RCA agent alike.
_dupes = sorted({f.key for f in _ALL if sum(g.key == f.key for g in _ALL) > 1})
if _dupes:
    raise RuntimeError(f"duplicate failure keys registered: {_dupes}")

FAILURES: dict[str, Failure] = {f.key: f for f in _ALL}

__all__ = [
    "FAILURES", "Failure", "InjectionLayer", "LoadHint",
    "OrchestrationMode", "inject", "recover",
]