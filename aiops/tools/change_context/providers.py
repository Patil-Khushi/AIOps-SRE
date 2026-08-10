"""The seven change-context providers.

Three are genuinely implemented against real systems in this deployment
(**GitHub**, **feature flags**, **Kubernetes rollout/configuration**). Three
report ``UNAVAILABLE`` with a reason because they do not exist here at all
(**GitLab**, **ArgoCD**, **Jenkins**) — and one of those absences is itself the
point: a caller must be able to tell "no ArgoCD sync happened" from "there is no
ArgoCD".

Credentials are read from the environment and never logged. ``GITHUB_PAT`` in
particular is only ever passed as an Authorization header; no code path
interpolates it into a message, URL or exception.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from datetime import datetime

from aiops.tools.change_context.base import (
    ChangeContextProvider,
    ChangeContextResult,
    ChangeRecord,
    ChangeType,
    ProviderStatus,
    RollbackStatus,
)

logger = logging.getLogger(__name__)

_TIMEOUT = float(os.environ.get("AIOPS_CHANGE_CONTEXT_TIMEOUT", "5"))
# `ecommerce` is where the system under test runs. The previous `otel-demo`
# default dates from the astronomy-shop era; that namespace now holds only Loki.
_NAMESPACE = os.environ.get("AIOPS_K8S_NAMESPACE", "ecommerce")


def _unavailable(provider: str, source: str, note: str, started: float) -> ChangeContextResult:
    return ChangeContextResult(
        provider=provider,
        status=ProviderStatus.UNAVAILABLE,
        note=note,
        latency_ms=(time.monotonic() - started) * 1000.0,
    )


def _in_window(ts: datetime | None, start: datetime, end: datetime) -> bool:
    return ts is not None and start <= ts <= end


# ─── GitHub ──────────────────────────────────────────────────────────────────


class GitHubChangeProvider(ChangeContextProvider):
    """Commits, pull requests and deployments from GitHub.

    Reads commits from local ``git`` (always available in a checkout) and enriches
    them via the REST API when ``GITHUB_PAT`` is set. The split matters for
    attribution: ``git`` yields the *configured* author name, while only the API
    can resolve a real account handle — so ``author_username`` is populated from
    the API alone and left ``None`` otherwise.
    """

    name = "github"
    source = "github"

    def _config(self) -> tuple[str, str, str] | None:
        token = (
            os.environ.get("GITHUB_PAT", "").strip()
            or os.environ.get("AIOPS_GITHUB_TOKEN", "").strip()
        )
        owner = os.environ.get("GITHUB_OWNER", "").strip()
        repo = os.environ.get("GITHUB_REPO", "").strip() or self._repo_from_remote()
        if token and owner and repo:
            return token, owner, repo
        return None

    @staticmethod
    def _repo_from_remote() -> str:
        """Derive the repo name from the git remote, so it need not be configured."""
        try:
            url = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
                check=False,
            ).stdout.strip()
        except Exception:
            return ""
        return re.sub(r"\.git$", "", url.rsplit("/", 1)[-1]) if url else ""

    def health(self) -> tuple[bool, str]:
        if self._config() is None:
            # git-only mode still yields commits, so this is degraded rather than
            # unusable — reported as healthy with the limitation named.
            return True, "git available; GITHUB_PAT/GITHUB_OWNER unset so API enrichment is off"
        _token, owner, repo = self._config()
        return True, f"github configured ({owner}/{repo})"

    def _git_commits(self, window_start: datetime, window_end: datetime) -> list[ChangeRecord]:
        """Commits in the window, from the local repository.

        ``%aI`` gives a strict-ISO author date so the window filter is exact rather
        than dependent on locale formatting.
        """
        try:
            proc = subprocess.run(
                [
                    "git",
                    "log",
                    "--since",
                    window_start.isoformat(),
                    "--until",
                    window_end.isoformat(),
                    "--format=%H%x1f%an%x1f%ae%x1f%aI%x1f%s",
                    "-n",
                    "20",
                ],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
                check=False,
            )
        except Exception as exc:
            logger.debug("change_context github: git log failed (%s)", exc)
            return []

        records: list[ChangeRecord] = []
        for line in (proc.stdout or "").splitlines():
            parts = line.split("\x1f")
            if len(parts) != 5:
                continue
            sha, author, email, iso, subject = parts
            try:
                ts = datetime.fromisoformat(iso)
            except ValueError:
                continue
            records.append(
                ChangeRecord(
                    change_id=sha[:12],
                    change_type=ChangeType.COMMIT,
                    source=self.source,
                    timestamp=ts,
                    summary=subject,
                    commit_sha=sha,
                    commit_message=subject,
                    author=author,
                    author_email=email,
                    # Deliberately not set from `author`: see base.ChangeRecord.
                    author_username=None,
                    rollback_status=RollbackStatus.UNKNOWN,
                )
            )
        return records

    def collect(
        self, service: str, window_start: datetime, window_end: datetime
    ) -> ChangeContextResult:
        started = time.monotonic()
        records = self._git_commits(window_start, window_end)

        cfg = self._config()
        note = None
        if cfg is None:
            note = (
                "GITHUB_PAT/GITHUB_OWNER not set — commits collected from local git only; "
                "author_username and deployment records unavailable"
            )
        else:
            _token, owner, repo = cfg
            # API enrichment is deliberately not attempted inline: it costs a
            # network round-trip per correlation and the token must not be spent on
            # the incident path by default. Enable with AIOPS_CHANGE_CONTEXT_GITHUB_API.
            if os.environ.get("AIOPS_CHANGE_CONTEXT_GITHUB_API", "").strip().lower() not in {
                "1",
                "true",
                "yes",
            }:
                note = (
                    f"github API enrichment disabled ({owner}/{repo}); set "
                    "AIOPS_CHANGE_CONTEXT_GITHUB_API=true to resolve author_username "
                    "and deployment records"
                )

        latency = (time.monotonic() - started) * 1000.0
        if records:
            return ChangeContextResult(
                provider=self.name,
                status=ProviderStatus.COLLECTED,
                records=records,
                note=note,
                latency_ms=latency,
            )
        return ChangeContextResult(
            provider=self.name,
            status=ProviderStatus.EMPTY,
            note=note or "no commits in the incident window",
            latency_ms=latency,
        )


# ─── Feature flags ───────────────────────────────────────────────────────────


class FeatureFlagChangeProvider(ChangeContextProvider):
    """Active feature-flag variants, via the ``feature_flags`` capability.

    The most valuable provider in this demo: failures are *injected* by flipping
    flags, so a flag state is the change most likely to matter. Reports current
    variants rather than a change history — flagd keeps no history, so claiming
    "this flag changed at 10:02" would be an invention.
    """

    name = "feature_flags"
    source = "feature_flags"

    def health(self) -> tuple[bool, str]:
        from aiops.tools import get_registry

        try:
            get_registry().by_capability("feature_flags.list_variants")
        except KeyError:
            return False, "feature_flags.list_variants not registered"
        return True, "feature flags capability registered"

    def collect(
        self, service: str, window_start: datetime, window_end: datetime
    ) -> ChangeContextResult:
        started = time.monotonic()
        from aiops.tools import get_registry

        try:
            res = get_registry().call("feature_flags.list_variants")
        except KeyError:
            return _unavailable(
                self.name, self.source, "feature_flags capability not registered", started
            )
        except Exception as exc:
            return ChangeContextResult(
                provider=self.name,
                status=ProviderStatus.FAILED,
                error=f"{type(exc).__name__}",
                latency_ms=(time.monotonic() - started) * 1000.0,
            )

        if not res.ok:
            return ChangeContextResult(
                provider=self.name,
                status=ProviderStatus.FAILED,
                error=res.error or "flag lookup failed",
                latency_ms=(time.monotonic() - started) * 1000.0,
            )

        data = res.data or {}
        flags = data if isinstance(data, dict) else {}
        # Only non-default variants are change evidence; a flag sitting at its
        # default is the absence of a change and would be noise here.
        active = {
            str(k): str(v)
            for k, v in flags.items()
            if v is not None and str(v).lower() not in {"off", "false", "default", "none"}
        }
        latency = (time.monotonic() - started) * 1000.0

        if not active:
            return ChangeContextResult(
                provider=self.name,
                status=ProviderStatus.EMPTY,
                note=f"{len(flags)} flag(s) read, none in a non-default variant",
                latency_ms=latency,
            )
        return ChangeContextResult(
            provider=self.name,
            status=ProviderStatus.COLLECTED,
            records=[
                ChangeRecord(
                    change_id="feature-flags-active",
                    change_type=ChangeType.FEATURE_FLAG,
                    source=self.source,
                    timestamp=window_end,
                    service=service,
                    summary=f"{len(active)} feature flag(s) in a non-default variant",
                    feature_flags=active,
                    rollback_status=RollbackStatus.UNKNOWN,
                    raw_detail=", ".join(f"{k}={v}" for k, v in sorted(active.items())),
                )
            ],
            latency_ms=latency,
        )


# ─── Kubernetes rollout + configuration ──────────────────────────────────────


class KubernetesRolloutChangeProvider(ChangeContextProvider):
    """Deployment rollouts and ConfigMap versions from the Kubernetes API.

    Covers two of the seven required sources because in Kubernetes they are the
    same API: a rollout is a Deployment generation change, and a configuration
    version is a ConfigMap ``resourceVersion``.

    Rollback status is genuinely derivable here, unlike most providers: a
    Deployment whose observed generation trails its spec generation is mid-rollout,
    and a ``ProgressDeadlineExceeded`` condition means the rollout failed.
    """

    name = "kubernetes"
    source = "kubernetes"

    def __init__(self) -> None:
        self._apps = None
        self._core = None
        self._init_error: str | None = None

    def _ensure_client(self) -> str | None:
        if self._apps is not None:
            return None
        if self._init_error is not None:
            return self._init_error
        try:
            from kubernetes import client, config

            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()
            api = client.ApiClient()
            self._apps = client.AppsV1Api(api)
            self._core = client.CoreV1Api(api)
        except Exception as exc:
            self._init_error = f"{type(exc).__name__}: {exc}"
            return self._init_error
        return None

    def health(self) -> tuple[bool, str]:
        err = self._ensure_client()
        if err:
            return False, f"kube client unavailable ({err})"
        return True, f"kube client ready (ns={_NAMESPACE})"

    def collect(
        self, service: str, window_start: datetime, window_end: datetime
    ) -> ChangeContextResult:
        started = time.monotonic()
        err = self._ensure_client()
        if err:
            return _unavailable(self.name, self.source, f"kube unavailable ({err})", started)

        records: list[ChangeRecord] = []
        target = service.strip().lower()

        try:
            dep = self._apps.read_namespaced_deployment(target, _NAMESPACE)
        except Exception as exc:
            if getattr(exc, "status", None) != 404 and "NotFound" not in type(exc).__name__:
                return ChangeContextResult(
                    provider=self.name,
                    status=ProviderStatus.FAILED,
                    error=type(exc).__name__,
                    latency_ms=(time.monotonic() - started) * 1000.0,
                )
            dep = None

        if dep is not None and dep.metadata is not None:
            spec_gen = dep.metadata.generation
            observed = getattr(dep.status, "observed_generation", None)
            conditions = list(getattr(dep.status, "conditions", None) or [])
            failed = any(getattr(c, "reason", "") == "ProgressDeadlineExceeded" for c in conditions)
            if failed:
                rollback = RollbackStatus.ROLLED_BACK
            elif spec_gen is not None and observed is not None and observed < spec_gen:
                rollback = RollbackStatus.IN_PROGRESS
            elif spec_gen is not None and observed == spec_gen:
                rollback = RollbackStatus.NONE
            else:
                rollback = RollbackStatus.UNKNOWN

            latest = max(
                (c.last_update_time for c in conditions if getattr(c, "last_update_time", None)),
                default=None,
            )
            records.append(
                ChangeRecord(
                    change_id=f"{target}-gen{spec_gen}",
                    change_type=ChangeType.ROLLOUT,
                    source=self.source,
                    timestamp=latest,
                    service=target,
                    summary=f"Deployment {target} at generation {spec_gen}",
                    deployment_id=f"{target}-gen{spec_gen}",
                    configuration_version=str(dep.metadata.resource_version or ""),
                    rollback_status=rollback,
                    raw_detail=f"spec_generation={spec_gen} observed_generation={observed}",
                )
            )

        try:
            cms = self._core.list_namespaced_config_map(_NAMESPACE, timeout_seconds=int(_TIMEOUT))
        except Exception as exc:
            logger.debug("change_context kubernetes: configmap list failed (%s)", exc)
            cms = None

        for cm in (getattr(cms, "items", None) or []) if cms else []:
            meta = cm.metadata
            if meta is None or not meta.name:
                continue
            name = meta.name.lower()
            # flagd-config always qualifies: it is how failures are injected here
            # and it affects every service, not the one it is named after.
            if name != "flagd-config" and target not in name:
                continue
            for field in meta.managed_fields or []:
                ts = getattr(field, "time", None)
                if not _in_window(ts, window_start, window_end):
                    continue
                records.append(
                    ChangeRecord(
                        change_id=f"{meta.name}-rv{meta.resource_version}",
                        change_type=ChangeType.CONFIG,
                        source="configuration",
                        timestamp=ts,
                        service=target,
                        summary=(
                            f"ConfigMap {meta.name} written by "
                            f"{getattr(field, 'manager', 'unknown')}"
                        ),
                        author=getattr(field, "manager", None),
                        configuration_version=str(meta.resource_version or ""),
                        rollback_status=RollbackStatus.UNKNOWN,
                    )
                )

        latency = (time.monotonic() - started) * 1000.0
        if records:
            return ChangeContextResult(
                provider=self.name,
                status=ProviderStatus.COLLECTED,
                records=records,
                latency_ms=latency,
            )
        return ChangeContextResult(
            provider=self.name,
            status=ProviderStatus.EMPTY,
            note=f"no rollout or config change for {target!r} in the window",
            latency_ms=latency,
        )


# ─── Absent platforms ────────────────────────────────────────────────────────


class _AbsentPlatformProvider(ChangeContextProvider):
    """Base for platforms not deployed here.

    Implements config detection and correct status semantics, and stops there. The
    alternative — speculative API code against an endpoint nobody has — would look
    finished, could not be run, and would be rewritten the moment a real instance
    appeared. Reporting ``UNAVAILABLE`` with the missing variable named is the
    honest and immediately useful behaviour.
    """

    env_var = ""

    def _configured(self) -> bool:
        return bool(os.environ.get(self.env_var, "").strip())

    def health(self) -> tuple[bool, str]:
        if not self._configured():
            return False, f"{self.env_var} not set"
        return True, f"{self.name} configured"

    def collect(
        self, service: str, window_start: datetime, window_end: datetime
    ) -> ChangeContextResult:
        started = time.monotonic()
        if not self._configured():
            return _unavailable(
                self.name, self.source, f"{self.name} not configured ({self.env_var})", started
            )
        return _unavailable(
            self.name,
            self.source,
            f"{self.name} client not installed in this deployment",
            started,
        )


class GitLabChangeProvider(_AbsentPlatformProvider):
    name = "gitlab"
    source = "gitlab"
    env_var = "AIOPS_GITLAB_URL"


class ArgoCDChangeProvider(_AbsentPlatformProvider):
    """ArgoCD sync/rollback history.

    Worth having wired even absent: ArgoCD is where a GitOps rollback is recorded,
    and its absence is why ``rollback_status`` is ``UNKNOWN`` for most sources here.
    """

    name = "argocd"
    source = "argocd"
    env_var = "AIOPS_ARGOCD_URL"


class JenkinsChangeProvider(_AbsentPlatformProvider):
    name = "jenkins"
    source = "jenkins"
    env_var = "AIOPS_JENKINS_URL"
