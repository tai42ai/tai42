"""The sandbox dispatch surface every provider group declares once."""

from __future__ import annotations

from typing import ClassVar

from tai42_kit.settings import TaiBaseSettings


class SandboxDispatchSettings(TaiBaseSettings):
    """The lifecycle knobs the kit sandbox base reads: the default session TTL,
    the reap sweep interval, and the default per-``exec`` timeout.

    Mixed into a concrete provider group's own env group rather than shared as one
    group, so the NAMES, DEFAULTS and RELOAD CLASSES are declared once while each
    provider keeps its own ``env_prefix`` (``model_config`` merges down the MRO,
    so the subclass's prefix wins).
    """

    # Abstract mixin — its field names are unprefixed and it claims no env of its
    # own; excluded from the settings registry. Own-attribute flag, so the
    # concrete per-provider subclasses still register.
    registry_exclude: ClassVar[bool] = True

    default_ttl_seconds: int = 3600
    reap_interval_seconds: int = 300
    exec_default_timeout_seconds: int = 300
