"""The backend's own project/credential config.

The Langfuse connection this backend emits to and reads from — the plugin's own
shape, declared in its own settings, never a contract type.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class LangfuseProject(BaseModel):
    """Credentials for one Langfuse project this backend emits to / reads from.

    ``source`` stamps every write with an environment marker and scopes every read
    to it, so callers sharing one project read back only their own data. Selectable
    at write time via ``writer.scope(public_key)`` and registered through
    ``LangfuseMonitoring.add_project`` — the plugin's own multi-project switch.
    """

    model_config = ConfigDict(frozen=True)

    public_key: str
    secret_key: str
    host: str
    timeout_seconds: int = 30
    source: str = "tai"
