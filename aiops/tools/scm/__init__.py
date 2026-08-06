"""SCM tool providers — read-only source, IaC and deployment-config access.

Importing this package side-effect-registers the providers with the global
``aiops.tools`` registry via their ``@tool`` decorators, matching how
``aiops.tools.observability`` works. Capabilities exposed:

- ``scm.file.read``       (provider ``github``)
- ``scm.repo.tree``       (provider ``github``)
- ``scm.commit.history``  (provider ``github``)
- ``scm.diff``            (provider ``github``)
- ``scm.pr.list``         (provider ``github``)

**Why this seam exists.** Metrics, logs and traces say *what* is broken. They
cannot say *what changed*. Change correlation — matching an incident's onset
against commits, diffs and merges touching the affected service — is the signal
that turns "payment latency is high" into "payment latency is high and this
commit to the gateway client landed four minutes earlier". It is the difference
between the RCA Agent producing a ranked cause list and producing an
executable fix.

**Autonomy: NONE.** Every capability is read-only, like
``aiops.tools.observability``. Nothing here can create, update or delete, so no
HITL gate is required. Keep it that way — if a future agent needs to open a PR,
that belongs behind a *separate*, HITL-gated capability
(``scm.pr.create``), never by adding a write path to these functions.

**Vendor neutrality** (non-negotiable #1): the capability names are
``scm.*``, not ``github.*``. A GitLab or Bitbucket provider registers against
the same capability names and agents need no change. Only the provider name
(``github``) is vendor-specific.

Configuration — see ``github.py``::

    AIOPS_GITHUB_REPO    owner/name (required)
    AIOPS_GITHUB_TOKEN   read-only fine-grained PAT
    AIOPS_GITHUB_REF     default branch (default: main)

Unset, every call returns ``ToolResult(ok=False, ...)`` rather than raising, so
the platform runs unconfigured exactly like the other seams.
"""

from __future__ import annotations

from aiops.tools.scm import github

__all__ = ["github"]
