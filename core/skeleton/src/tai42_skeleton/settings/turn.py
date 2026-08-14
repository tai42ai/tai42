"""``TAI_TURN_*`` config for the synchronous turn budget.

A wall-clock budget for one synchronous turn: every synchronous tool call holds a
live caller's connection open for its whole duration, so one generic setting bounds
them all. Read at call time through the ``settings_cache`` accessor so a ``.env``
override applied by the CLI bootstrap is honoured.
"""

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings, settings_cache


class TurnSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="TAI_TURN_")

    # Wall-clock budget for one synchronous turn. None = unbounded (a slow turn is
    # indistinguishable from a hung one behind a reverse proxy); set it so expiry cancels
    # the awaiting turn and answers with a loud, typed error instead of a bare proxy 504.
    # The budget guards a live caller's held connection; a detached run (no live caller)
    # runs unbounded regardless. Expiry answers the caller loudly; an offloaded synchronous
    # tool body may still finish in the background — see the turn-budget primitive. Must be
    # positive when set.
    timeout_seconds: float | None = Field(default=None, gt=0)


@settings_cache
def turn_settings() -> TurnSettings:
    return TurnSettings()
