"""The dispatch surface the host and every execution backend must agree on."""

from typing import ClassVar

from pydantic import Field

from tai42_kit.settings import TaiBaseSettings


class BackendDispatchSettings(TaiBaseSettings):
    """The three fields tool dispatch needs both sides to name identically: the
    env key the resolved manifest is exported under, the synchronous dispatch
    result timeout, and the kwarg name carrying the target tool name into a
    queued job.

    Mixed into each side's own env group rather than shared as one group, so the
    NAMES, DEFAULTS and RELOAD CLASSES are declared once while each side keeps
    its own ``env_prefix`` (``model_config`` merges down the MRO, so the
    subclass's prefix wins and the env surface is unchanged).

    ``manifest_key`` and ``tool_name_arg`` pin wiring built at boot — a forked
    child reads the env key it was given and a queued job carries the kwarg name
    the producer used — so a change converges through a process recycle, not an
    in-process flip.
    """

    # Abstract group — its field names are unprefixed and it claims no env of its
    # own; excluded from the settings registry. Own-attribute flag, so the
    # concrete per-side subclasses still register.
    registry_exclude: ClassVar[bool] = True

    manifest_key: str = Field(default="MANIFEST_KEY", json_schema_extra={"reload": "recycle"})
    task_timeout: int = 300
    tool_name_arg: str = Field(default="backend_tool_name", json_schema_extra={"reload": "recycle"})
