"""ITSM tool providers: ServiceNow (RA-003 Auto-Ticketing).

Importing this package side-effect-registers the providers with the global
``aiops.tools`` registry via their ``@tool`` decorators. Capabilities exposed:

- ``itsm.incident.create``  (provider ``servicenow``)
- ``itsm.incident.update``  (provider ``servicenow``)
- ``itsm.cmdb.lookup``      (provider ``servicenow``)

Configured via ``AIOPS_SERVICENOW_INSTANCE_URL`` / ``AIOPS_SERVICENOW_USER`` /
``AIOPS_SERVICENOW_PASSWORD`` (see ``.env.example``). If those are unset every
call returns ``ToolResult(ok=False, error="ServiceNow not configured")`` so the
registry stays loadable in tests / CI without credentials.

Co-existence with mocks: the registry uses ``setdefault`` to pick the active
provider for a capability, so import order decides whether ``mock.itsm.*`` or
``snow.itsm.*`` wins. The mock for ``itsm.cmdb.lookup`` is gated on
``AIOPS_USE_MOCK_ITSM=true`` (default in CI) so the real ServiceNow client
takes over when an agent runs against a real PDI.
"""

from __future__ import annotations

from aiops.tools.itsm import servicenow

__all__ = ["servicenow"]
