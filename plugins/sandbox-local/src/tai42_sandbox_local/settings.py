"""Settings for the direct/host sandbox provider (the ``SANDBOX_LOCAL_`` group).

``SandboxLocalSettings`` reads the ``SANDBOX_LOCAL_`` env group and mixes in
:class:`~tai42_kit.sandbox.SandboxDispatchSettings`, the lifecycle surface the
kit sandbox base reads (``default_ttl_seconds`` / ``reap_interval_seconds`` /
``exec_default_timeout_seconds``): the names, defaults and reload classes are
declared once there, under this group's own prefix.

Two provider-specific fields:

- ``root`` — the host workspace ROOT. A ``persistent`` session's workspace is the
  NAMED directory ``<root>/<workspace_key>``; changing it strands the existing
  durable workspaces, so it is RECYCLE-class (it binds provisioned on-disk state).
- ``base_path`` — the ``PATH`` seeded into the CLEAN subprocess env so a host
  subprocess can resolve its binary WITHOUT inheriting the host ``os.environ``. A
  ``spec.env`` / per-exec ``env`` ``PATH`` overrides it. It binds no provisioned
  state, so it is HOT.

There is NO resource-cap or isolation knob here: the direct host mode has no
cap/isolation machinery, so a spec asking for one is REJECTED loudly by the
provider, never silently run uncapped/unisolated.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.sandbox import SandboxDispatchSettings
from tai42_kit.settings import settings_cache


class SandboxLocalSettings(SandboxDispatchSettings):
    model_config = SettingsConfigDict(env_prefix="SANDBOX_LOCAL_")

    # The host workspace root every provisioned workspace lives under. RECYCLE-class:
    # it binds the on-disk location of durable workspaces, so a change strands them
    # and only converges through a process recycle.
    root: str = Field(default="/var/lib/tai-sandbox-local", json_schema_extra={"reload": "recycle"})

    # The PATH seeded into the clean subprocess env (never the host os.environ), so a
    # host subprocess can resolve its binary. A spec.env / per-exec env PATH overrides
    # it. HOT: it binds no provisioned state.
    base_path: str = "/usr/local/bin:/usr/bin:/bin"


@settings_cache
def sandbox_local_settings() -> SandboxLocalSettings:
    return SandboxLocalSettings()
