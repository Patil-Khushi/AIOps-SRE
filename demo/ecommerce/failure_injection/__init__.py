"""Failure-injection toolkit for the ecommerce demo.

Twelve failures across the three services, each with inject()/recover() logic
plus reference L1/L2/RCA signals. Use the CLI:

    python -m failure_injection list
    python -m failure_injection inject user_service.mysql_down
    python -m failure_injection recover user_service.mysql_down
    python -m failure_injection inject payment_service.high_cpu --load 30
    FI_DRY_RUN=1 python -m failure_injection inject order_service.memory_leak_oom

FI_DRY_RUN=1 prints the docker commands instead of running them.
"""
from ._base import Failure, LoadHint
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

_ALL = [
    _u_mysql.failure, _u_lat.failure, _u_cpu.failure, _u_crash.failure,
    _o_pg.failure, _o_pt.failure, _o_500.failure, _o_oom.failure,
    _p_redis.failure, _p_gt.failure, _p_cpu.failure, _p_500.failure,
]

FAILURES: dict[str, Failure] = {f.key: f for f in _ALL}

__all__ = ["FAILURES", "Failure", "LoadHint"]