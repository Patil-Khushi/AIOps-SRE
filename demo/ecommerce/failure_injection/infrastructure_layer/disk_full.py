"""Payment Service — disk pressure from a large file.

Targets /tmp with a bounded size rather than filling a volume to 95%. The pods
have no dedicated data volume: `/` is the containerd overlay, which is the
*node's* filesystem shared with etcd and every other pod, so a percentage-based
fill there is a cluster-wide outage rather than a service-level scenario.

Consequence to be honest about: a bounded write to a ~1 TB shared filesystem
moves disk_usage_percent barely at all. The observable signal is the write
itself and any application-level ENOSPC handling — not a node disk-pressure
alert. Making this scenario produce a real DiskPressure signal needs an emptyDir
with a sizeLimit mounted into the pod; see k8s/20-app.yaml.
"""

from .._base import Failure, InjectionLayer
from . import _infra_backend

TARGET_PATH = "/tmp"
SIZE_MB = 256


def inject() -> None:
    """Write SIZE_MB into TARGET_PATH on payment-service."""
    _infra_backend.fill_disk("payment-service", target_path=TARGET_PATH, size_mb=SIZE_MB)


def recover() -> None:
    """Delete the fill file."""
    _infra_backend.clear_disk("payment-service", target_path=TARGET_PATH)


failure = Failure(
    key="payment_service.disk_full",
    service="payment-service",
    title=f"Disk pressure ({SIZE_MB}MB fill)",
    layer=InjectionLayer.INFRASTRUCTURE,
    inject=inject,
    recover=recover,
    l1="filesystem available bytes dropping on payment-service; write errors in logs",
    l2=f"A {SIZE_MB}MB file present under {TARGET_PATH}; application write paths failing",
    rca="Disk space exhaustion on the payment service filesystem",
)
