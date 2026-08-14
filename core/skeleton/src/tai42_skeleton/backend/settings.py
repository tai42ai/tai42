from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings, settings_cache


class BackendSettings(TaiBaseSettings):
    # Declared here rather than inherited from
    # ``tai42_kit.backend.BackendDispatchSettings``, which carries the same three
    # fields for the backend plugins: the API-diff gate loads this package with
    # only itself on the search path, so an inherited member is invisible to it
    # and reads as a removed public attribute. The two declaration sites are
    # pinned equal by a drift guard in the settings tests.
    model_config = SettingsConfigDict(
        env_prefix="BACKEND_",
    )
    # The manifest key and the tool-name arg pin the backend dispatch wiring built
    # at boot; a change converges through a process recycle, not an in-process flip.
    manifest_key: str = Field(default="MANIFEST_KEY", json_schema_extra={"reload": "recycle"})
    task_timeout: int = 300
    tool_name_arg: str = Field(default="backend_tool_name", json_schema_extra={"reload": "recycle"})


@settings_cache
def base_backend_settings() -> BackendSettings:
    return BackendSettings()
