"""``TAI_AGENT_*`` config for the synchronous agent run surface.

Co-located with the run-tool binding: a wall-clock budget for one synchronous
agent run (the run tool holds the HTTP request for the whole turn). Read at call
time through the ``settings_cache`` accessor so a ``.env`` override applied by the
CLI bootstrap is honoured.
"""

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings, settings_cache


class AgentRunSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="TAI_AGENT_")

    # Wall-clock budget for one synchronous agent run. None = unbounded (a slow
    # turn is indistinguishable from a hung one behind a reverse proxy); set it to
    # cancel a stalled turn on expiry and surface a loud, typed error instead of a
    # bare proxy 504. Must be positive when set.
    run_timeout_seconds: float | None = Field(default=None, gt=0)


@settings_cache
def agent_run_settings() -> AgentRunSettings:
    return AgentRunSettings()
