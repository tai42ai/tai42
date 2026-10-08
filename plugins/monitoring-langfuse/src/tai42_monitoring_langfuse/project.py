"""The backend's own project/credential config.

The Langfuse connection this backend reads from — the plugin's own
shape, declared in its own settings, never a contract type.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class LangfuseProject(BaseModel):
    """Credentials for the Langfuse project this backend reads from.

    ``source`` stamps every write with an environment marker and scopes every read
    to it, so callers sharing one project read back only their own data.
    """

    model_config = ConfigDict(frozen=True)

    public_key: str
    secret_key: str
    host: str
    timeout_seconds: int = 30
    source: str = "tai"
