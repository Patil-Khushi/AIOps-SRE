"""Infrastructure chaos backend - low-level primitives.

Provides abstractions over tc, stress-ng, kubectl, iptables, etc.
All operations log their commands for transparency.
"""

from __future__ import annotations

import logging
import os
import subprocess

from .._base import ChaosUnavailable

logger = logging.getLogger(__name__)

DRY_RUN = os.getenv("FI_DRY_RUN", "").lower() in ("1", "true", "yes")
NAMESPACE = os.getenv("FI_NAMESPACE", "ecommerce")

CHAOS_MARKER = "aiops-chaos"

# Backgrounded chaos PIDs are appended here so recovery can kill exactly those
# without restarting the pod — a restart would reset restartCount and clear the
# OOMKilled terminated-reason that the truth files assert on.
#
# A pidfile rather than `pkill -f <marker>`: these service images are slim and
# ship no procps, so neither pkill nor ps exists in them. Only python3, dd and
# sh are available, so every in-container step has to be expressible in those.
CHAOS_PIDFILE = f"/tmp/{CHAOS_MARKER}.pids"


# Re-exported from _base so the orchestrator can recognise it without importing
# this module. Raised in preference to letting kubectl exec fail with a bare
# non-zero exit: "rebuild the image" / "grant a capability" is not something the
# caller can infer from "command terminated with exit code 127".
ChaosUnavailable = ChaosUnavailable


# CAP_NET_ADMIN. Without it `tc qdisc add` fails with EPERM even when tc is
# installed — the containerd default capability set grants CAP_NET_RAW but not
# this one, so network chaos needs an explicit securityContext.
_CAP_NET_ADMIN_BIT = 12


def _run(cmd: list[str], description: str = "") -> int:
    """Run a shell command and return exit code."""
    printable = " ".join(cmd)
    if DRY_RUN:
        print(f"[dry-run] {printable}")
        return 0
    if description:
        logger.info(f"{description}: {printable}")
    else:
        logger.info(f"+ {printable}")
    return subprocess.call(cmd)


def _run_output(cmd: list[str]) -> str | None:
    """Run a command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(f"command failed: {' '.join(cmd)}\n{result.stderr}")
            return None
        return result.stdout.strip()
    except Exception as e:
        logger.error(f"command error: {e}")
        return None


# ─── Preflight ────────────────────────────────────────────────────────────────


def require_binary(pod_name: str, binary: str) -> None:
    """Raise ChaosUnavailable unless `binary` exists inside the pod."""
    if DRY_RUN:
        return
    found = _kubectl_exec_capture(
        pod_name, f"command -v {binary} >/dev/null 2>&1 && echo yes || echo no"
    )
    if found != "yes":
        raise ChaosUnavailable(
            f"{binary!r} is not installed in {pod_name}. Add it to the service image "
            f"(see demo/ecommerce/*/Dockerfile) or use a chaos backend that runs "
            f"outside the container, e.g. Chaos Mesh."
        )


def require_net_admin(pod_name: str) -> None:
    """Raise ChaosUnavailable unless the pod holds CAP_NET_ADMIN.

    tc needs it to touch a qdisc; the containerd default set omits it.
    """
    if DRY_RUN:
        return
    line = _kubectl_exec_capture(pod_name, "grep CapEff /proc/self/status")
    if not line:
        logger.warning("could not read CapEff from %s; attempting tc anyway", pod_name)
        return
    try:
        caps = int(line.split()[-1], 16)
    except (IndexError, ValueError):
        logger.warning("unparseable CapEff %r from %s", line, pod_name)
        return
    if not (caps >> _CAP_NET_ADMIN_BIT) & 1:
        raise ChaosUnavailable(
            f"{pod_name} lacks CAP_NET_ADMIN (CapEff={caps:#x}), so tc cannot modify "
            f"a qdisc. Add to the container spec:\n"
            f"    securityContext:\n"
            f"      capabilities:\n"
            f'        add: ["NET_ADMIN"]\n'
            f"...or drive network chaos from outside the container (Chaos Mesh)."
        )


# ─── Network Chaos (tc qdisc) ──────────────────────────────────────────────────


def _netem(pod_or_service: str, spec: str, description: str) -> None:
    """Attach a netem qdisc described by `spec` to the pod's root qdisc."""
    pod_name = _require_pod(pod_or_service)
    require_binary(pod_name, "tc")
    require_net_admin(pod_name)
    iface = _get_pod_interface(pod_name)
    _kubectl_exec(
        pod_name,
        f"tc qdisc add dev {iface} root netem {spec}",
        f"{description} on {pod_name} ({iface})",
    )


def _clear_netem(pod_or_service: str, description: str) -> None:
    """Detach the root qdisc, undoing any netem attached to it."""
    pod_name = _get_pod_name(pod_or_service)
    if not pod_name:
        logger.warning("no pod found for %s; nothing to clear", pod_or_service)
        return
    # Preflight on the recovery path too. If tc is absent then nothing was ever
    # injected, so there is genuinely nothing to clear — but running the doomed
    # command anyway would leak "tc: not found" to stderr and still report
    # success. ChaosUnavailable is safe here: the orchestrator scores it as
    # "unavailable" rather than an error, so a hybrid recovery whose application
    # half succeeded still comes back ok.
    require_binary(pod_name, "tc")
    iface = _get_pod_interface(pod_name)
    _kubectl_exec(
        pod_name,
        f"tc qdisc del dev {iface} root",
        f"{description} on {pod_name} ({iface})",
    )


def inject_network_delay(pod_or_service: str, delay_ms: int | float) -> None:
    """Inject `delay_ms` of network latency via tc netem."""
    _netem(pod_or_service, f"delay {int(delay_ms)}ms", f"injecting {delay_ms}ms latency")


def remove_network_delay(pod_or_service: str) -> None:
    """Remove injected network latency."""
    _clear_netem(pod_or_service, "removing network delay")


def inject_packet_loss(pod_or_service: str, loss_percent: int | float) -> None:
    """Inject `loss_percent`% packet loss via tc netem."""
    _netem(
        pod_or_service,
        f"loss {float(loss_percent)}%",
        f"injecting {loss_percent}% packet loss",
    )


def remove_packet_loss(pod_or_service: str) -> None:
    """Remove injected packet loss."""
    _clear_netem(pod_or_service, "removing packet loss")


# ─── Pod Operations ────────────────────────────────────────────────────────────


def kill_pod(pod_or_service: str) -> None:
    """Kill a pod by name or service name.

    The pod will auto-restart due to Kubernetes RestartPolicy.
    This causes real application errors.

    Args:
        pod_or_service: Pod name or service name (e.g., 'payment-service')
    """
    pod_name = _get_pod_name(pod_or_service)
    if not pod_name:
        raise ValueError(f"could not find pod for {pod_or_service}")

    _run(
        ["kubectl", "-n", NAMESPACE, "delete", "pod", pod_name, "--ignore-not-found"],
        f"killing pod {pod_name}",
    )


def wait_for_pod_ready(pod_or_service: str, timeout_sec: int = 120) -> None:
    """Wait for a pod to be ready after being killed/restarted."""
    pod_name = _get_pod_name(pod_or_service)
    if not pod_name:
        return

    selector = pod_name.rsplit("-", 2)[
        0
    ]  # e.g., 'payment-service' from 'payment-service-abc123-def456'
    _run(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "rollout",
            "status",
            f"deployment/{selector}",
            f"--timeout={timeout_sec}s",
        ],
        f"waiting for {selector} to be ready",
    )


# ─── Resource Stress ───────────────────────────────────────────────────────────


def stress_cpu(
    pod_or_service: str,
    cores: int = 1,
    duration_sec: int = 600,
    utilization: float = 0.85,
) -> None:
    """Drive `cores` worth of CPU to `utilization` inside the pod.

    Uses the container's own Python rather than stress-ng: these are Python
    service images and do not ship stress-ng, so requiring it would mean
    rebuilding every service image to run one scenario. A busy multiply-mod loop
    is indistinguishable from stress-ng's --cpu as far as cgroup CPU accounting
    and the resulting Prometheus series are concerned.

    **Why duty-cycled rather than a flat spin.** These containers have
    `limits.cpu: 1` and a liveness probe of `timeout=1s, failureThreshold=3`.
    A flat 100% spin exhausts the cgroup quota, starves the health endpoint of
    the scheduler time it needs to answer within 1s, and the kubelet restarts the
    container roughly a minute in. The scenario then reads as a crashloop rather
    than as CPU saturation — the wrong signal, and it destroys the burn process
    along with the container. Leaving ~15% headroom keeps /health responsive
    while still pinning the CPU series high enough to alert on.

    One process per core: the GIL serialises the loop, so a single process cannot
    exceed one core no matter the duty cycle.
    """
    pod_name = _require_pod(pod_or_service)
    require_binary(pod_name, "python3")

    duty = min(max(utilization, 0.05), 1.0)
    burn = (
        f"import time\n"
        f"end = time.time() + {int(duration_sec)}\n"
        f"slice_s = 0.05\n"
        f"busy_s = slice_s * {duty}\n"
        f"idle_s = slice_s - busy_s\n"
        f"x = 0\n"
        f"while time.time() < end:\n"
        f"    t0 = time.time()\n"
        f"    while time.time() - t0 < busy_s:\n"
        f"        x = (x * x + 1) % 2147483647\n"
        f"    if idle_s > 0:\n"
        f"        time.sleep(idle_s)\n"
    )
    _kubectl_exec(
        pod_name,
        _spawn_tracked(burn, count=max(1, cores)),
        f"driving {cores} core(s) to {duty:.0%} on {pod_name} for {duration_sec}s",
    )


def stress_memory(
    pod_or_service: str,
    memory_mb: int,
    duration_sec: int = 600,
) -> None:
    """Hold `memory_mb` MiB resident inside the pod for `duration_sec`.

    bytearray() zero-fills, so the pages are touched and genuinely resident —
    an untouched allocation would not count against the cgroup limit and so
    would never trigger the OOMKill this is meant to produce.
    """
    pod_name = _require_pod(pod_or_service)
    require_binary(pod_name, "python3")

    hog = (
        f"import time\n"
        f"buf = bytearray({int(memory_mb)} * 1024 * 1024)\n"
        f"time.sleep({int(duration_sec)})\n"
    )
    _kubectl_exec(
        pod_name,
        _spawn_tracked(hog, count=1),
        f"holding {memory_mb}MiB resident on {pod_name} for {duration_sec}s",
    )


def _spawn_tracked(payload: str, count: int) -> str:
    """Shell snippet that backgrounds `payload` `count` times, recording each PID."""
    one = f"python3 -c {_shquote(payload)} >/dev/null 2>&1 & echo $! >> {CHAOS_PIDFILE}"
    return "; ".join([one] * count)


def stop_stress(pod_or_service: str) -> None:
    """Kill tracked chaos processes, leaving the pod running.

    Preferred over kill_pod() for stress recovery: a restart resets restartCount
    and clears the OOMKilled terminated-reason the truth files assert on.

    Idempotent — a missing pidfile or an already-dead PID is a no-op, so
    recovering a clean pod is not an error.
    """
    pod_name = _get_pod_name(pod_or_service)
    if not pod_name:
        logger.warning("no pod found for %s; nothing to stop", pod_or_service)
        return

    killer = (
        f"import os, signal\n"
        f"try:\n"
        f"    pids = open({CHAOS_PIDFILE!r}).read().split()\n"
        f"except OSError:\n"
        f"    pids = []\n"
        f"for p in pids:\n"
        f"    try:\n"
        f"        os.kill(int(p), signal.SIGKILL)\n"
        f"    except (OSError, ValueError):\n"
        f"        pass\n"
        f"try:\n"
        f"    os.remove({CHAOS_PIDFILE!r})\n"
        f"except OSError:\n"
        f"    pass\n"
        f"print('stopped', len(pids))\n"
    )
    _kubectl_exec(
        pod_name,
        f"python3 -c {_shquote(killer)}",
        f"stopping tracked chaos processes on {pod_name}",
    )


def count_chaos_procs(pod_or_service: str) -> int:
    """How many tracked chaos processes are still alive in the pod.

    Checks /proc/<pid> rather than shelling to ps, which these images lack.
    Returns 0 when the pod or pidfile is absent.
    """
    pod_name = _get_pod_name(pod_or_service)
    if not pod_name:
        return 0
    counter = (
        f"import os\n"
        f"try:\n"
        f"    pids = open({CHAOS_PIDFILE!r}).read().split()\n"
        f"except OSError:\n"
        f"    pids = []\n"
        f"print(sum(os.path.exists('/proc/' + p) for p in pids))\n"
    )
    out = _kubectl_exec_capture(pod_name, f"python3 -c {_shquote(counter)}")
    return int(out) if out and out.strip().isdigit() else 0


# ─── Disk Operations ──────────────────────────────────────────────────────────

# Ceiling on a single disk-fill, regardless of what the caller asks for.
#
# Deliberately not a "fill to N%" knob. These pods have no dedicated data volume:
# `/` is the containerd overlay, which on Rancher Desktop is the *node's* disk
# (~1 TB, shared with etcd and every other pod). "Fill to 95%" there means
# writing ~950 GB to the node and can take the cluster down with it — an
# unrecoverable outcome for a demo scenario. An explicit, bounded byte count
# produces the same disk-pressure signal without betting the cluster on it.
MAX_DISK_FILL_MB = 512


def fill_disk(
    pod_or_service: str,
    target_path: str = "/tmp",
    size_mb: int = 256,
) -> None:
    """Write a `size_mb` file into `target_path` to create disk pressure.

    Capped at MAX_DISK_FILL_MB. `target_path` defaults to /tmp; note that unless
    /tmp is a tmpfs or emptyDir mount it lands on the shared node filesystem, so
    the cap is what keeps this safe rather than the path.
    """
    pod_name = _require_pod(pod_or_service)
    require_binary(pod_name, "dd")

    if size_mb > MAX_DISK_FILL_MB:
        logger.warning(
            "requested %sMB disk fill exceeds MAX_DISK_FILL_MB=%s; clamping",
            size_mb,
            MAX_DISK_FILL_MB,
        )
        size_mb = MAX_DISK_FILL_MB

    avail = _kubectl_exec_capture(
        pod_name, f"df -Pm {_shquote(target_path)} 2>/dev/null | awk 'NR==2 {{print $4}}'"
    )
    if avail and avail.isdigit() and int(avail) < size_mb:
        raise ChaosUnavailable(
            f"{target_path} on {pod_name} has only {avail}MB free; refusing to write {size_mb}MB"
        )

    _kubectl_exec(
        pod_name,
        f"dd if=/dev/zero of={_shquote(_fill_path(target_path))} bs=1M count={int(size_mb)}",
        f"writing {size_mb}MB to {target_path} on {pod_name}",
    )


def clear_disk(pod_or_service: str, target_path: str = "/tmp") -> None:
    """Remove the disk-fill file."""
    pod_name = _get_pod_name(pod_or_service)
    if not pod_name:
        logger.warning("no pod found for %s; nothing to clear", pod_or_service)
        return

    _kubectl_exec(
        pod_name,
        f"rm -f {_shquote(_fill_path(target_path))}",
        f"clearing disk fill on {pod_name}",
    )


def _fill_path(target_path: str) -> str:
    """Path of the fill file. Fixed name so clear_disk finds it.

    Not parameterised by pod name: the pod that injected may have been replaced
    by the time recovery runs, and a name-keyed file would then be orphaned.
    """
    return f"{target_path.rstrip('/')}/{CHAOS_MARKER}-fill.img"


# ─── DNS Chaos ────────────────────────────────────────────────────────────────


def break_dns(pod_or_service: str) -> None:
    """Break DNS resolution by poisoning /etc/resolv.conf."""
    pod_name = _get_pod_name(pod_or_service)
    if not pod_name:
        raise ValueError(f"could not find pod for {pod_or_service}")

    _kubectl_exec(
        pod_name,
        "echo 'nameserver 1.2.3.4' > /etc/resolv.conf",  # Invalid nameserver
        f"breaking DNS on {pod_name}",
    )


def restore_dns(pod_or_service: str) -> None:
    """Restore DNS by restarting pod (or manual fix)."""
    # Easiest way: kill the pod so it restarts with correct DNS
    kill_pod(pod_or_service)


# ─── Connection Pool Exhaustion ────────────────────────────────────────────────


def start_connection_holder(
    pod_or_service: str,
    connection_count: int = 155,
    duration_sec: int = 600,
    host: str = "mysql",
    port: int = 3306,
    user: str = "appuser",
    password: str = "apppass",
    database: str = "users",
) -> None:
    """Open and hold `connection_count` MySQL connections from inside the pod.

    Exhausts the **server's** ``max_connections`` (151 by default) rather than
    the application's own SQLAlchemy pool. Saturating the app's internal pool
    would require the app to hold its own connections, which is an application
    concern; starving the server is the infrastructure-level equivalent and is
    what an application actually meets in production — new connections are
    refused with "Too many connections" no matter how healthy its local pool is.

    Uses pymysql, which ships in these images as SQLAlchemy's MySQL driver.
    mysql-connector-python is *not* installed; requiring it would mean an image
    rebuild. Credentials default to the same ones user-service reads from env
    (see user-service/src/db/mysql_client.py).
    """
    pod_name = _require_pod(pod_or_service)
    require_binary(pod_name, "python3")

    holder = (
        f"import time, pymysql\n"
        f"held = []\n"
        f"for _ in range({int(connection_count)}):\n"
        f"    try:\n"
        f"        held.append(pymysql.connect(\n"
        f"            host={host!r}, port={int(port)}, user={user!r},\n"
        f"            password={password!r}, database={database!r},\n"
        f"            connect_timeout=5))\n"
        f"    except Exception:\n"
        f"        break\n"  # server refused: the limit is reached, which is the goal
        f"time.sleep({int(duration_sec)})\n"
    )
    _kubectl_exec(
        pod_name,
        _spawn_tracked(holder, count=1),
        f"holding up to {connection_count} MySQL connections from {pod_name}",
    )


def stop_connection_holder(pod_or_service: str) -> None:
    """Release the held connections.

    Kills the holder process, which drops its sockets; the server reaps the
    sessions. Does not restart the pod — see stop_stress().
    """
    stop_stress(pod_or_service)


# ─── Helper Functions ──────────────────────────────────────────────────────────


def _shquote(s: str) -> str:
    """Single-quote `s` for /bin/sh.

    The stress payloads are multi-line Python with '%' and parens in them, so
    they must reach sh as one opaque argument.
    """
    return "'" + s.replace("'", "'\\''") + "'"


def _require_pod(pod_or_service: str) -> str:
    """Resolve to a pod name or raise — for callers that cannot proceed without one."""
    pod_name = _get_pod_name(pod_or_service)
    if not pod_name:
        raise ChaosUnavailable(
            f"no running pod found for {pod_or_service!r} in namespace {NAMESPACE!r}"
        )
    return pod_name


def _get_pod_name(pod_or_service: str) -> str | None:
    """Resolve a service name to its first running pod; pass pod names through.

    A generated pod name ends in "-<replicaset hash>-<suffix>", which no service
    name in this stack does — so counting trailing dash-segments distinguishes
    them without a length heuristic (`payment-service` and `mock-payment-gateway`
    are both over 20 characters, which is what the previous check keyed on).
    """
    if pod_or_service.count("-") >= 3 or _looks_like_statefulset_pod(pod_or_service):
        return pod_or_service

    output = _run_output(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "get",
            "pods",
            "-l",
            f"app={pod_or_service}",
            "--field-selector=status.phase=Running",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ]
    )
    return output or None


def _looks_like_statefulset_pod(name: str) -> bool:
    """True for StatefulSet-style ordinals like 'mysql-0'."""
    head, _, tail = name.rpartition("-")
    return bool(head) and tail.isdigit()


def _get_pod_interface(pod_name: str) -> str:
    """Best-effort primary interface name inside the pod, defaulting to eth0.

    These service images ship neither `ip` nor `ifconfig`, so discovery reads
    /sys/class/net (sysfs is always present) instead of parsing command output.
    eth0 is the CNI default and the near-certain answer; the lookup exists only
    so a non-default CNI does not silently target the wrong device.
    """
    listing = _kubectl_exec_capture(
        pod_name,
        'for d in /sys/class/net/*; do n=$(basename $d); [ "$n" != lo ] && echo $n; done | head -1',
    )
    if listing:
        return listing.strip()
    logger.debug("interface discovery failed on %s; assuming eth0", pod_name)
    return "eth0"


def _kubectl_exec(
    pod_name: str,
    command: str,
    description: str = "",
) -> int:
    """Execute a command inside a pod."""
    return _run(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "exec",
            pod_name,
            "--",
            "sh",
            "-c",
            command,
        ],
        description,
    )


def _kubectl_exec_capture(pod_name: str, command: str) -> str | None:
    """Execute a command in a pod and capture output."""
    return _run_output(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "exec",
            pod_name,
            "--",
            "sh",
            "-c",
            command,
        ]
    )


__all__ = [
    "CHAOS_MARKER",
    "CHAOS_PIDFILE",
    "MAX_DISK_FILL_MB",
    "ChaosUnavailable",
    "break_dns",
    "clear_disk",
    "count_chaos_procs",
    "fill_disk",
    "inject_network_delay",
    "inject_packet_loss",
    "kill_pod",
    "remove_network_delay",
    "remove_packet_loss",
    "require_binary",
    "require_net_admin",
    "restore_dns",
    "start_connection_holder",
    "stop_connection_holder",
    "stop_stress",
    "stress_cpu",
    "stress_memory",
    "wait_for_pod_ready",
]
