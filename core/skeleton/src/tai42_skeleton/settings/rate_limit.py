"""``TAI_RATE_LIMIT_*`` config for the app-level public-door rate limiter.

The limiter's COVERAGE is not configured here: it is derived from the route
registry, so every route registered ``authed=False`` is throttled and a new public
door is protected the moment it registers. What this config holds is the BUDGET —
a per-minute limit, a 10-second burst window, and an enable switch. The proxy-trust
statement the client-address resolver reads is a deployment-wide fact owned by
:mod:`tai42_kit.utils.client_address` (env ``TAI_RATE_LIMIT_TRUSTED_*``), read here
through that resolver, not declared on this config.

Budgets resolve in one order, most specific first: a ``families`` override for the
door family, else the ``default_*`` budget every derived family starts from. A
family is the door's own path stem (``trigger``, ``universal_webhook``,
``interactions_callback``, ``channels_web``, …) as
:func:`tai42_skeleton.middleware.rate_limit.family_of` derives it, so families keep
disjoint counters and a flood on one public door cannot exhaust another's budget.

Settings are read at call time.
"""

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import SettingsConfigDict
from tai42_kit.clients import RedisConnectionSettings
from tai42_kit.settings import TaiBaseSettings, settings_cache


class FamilyBudget(BaseModel):
    """One door family's resolved budget: whether the limiter charges it at all, and
    the two window ceilings it charges against."""

    enabled: bool
    limit: int
    burst: int


class FamilyOverride(BaseModel):
    """An operator's per-family override. Every field is optional and an unset one
    falls through to the ``default_*`` budget, so tuning ONE window of ONE door
    family never restates the rest."""

    model_config = {"extra": "forbid"}

    enabled: bool | None = None
    limit: int | None = Field(default=None, gt=0)
    burst: int | None = Field(default=None, gt=0)


# The budgets the skeleton's own doors ship with, keyed by family. These are
# DEFAULTS, not coverage: a family absent here is still throttled, at the
# ``default_*`` budget. An operator's ``families`` entry overrides whatever a family
# resolves to here.
#
# The three single-request doors are held at the tighter 60/min + 10/10s: one
# request buys real work (a webhook fan-out, a ticket redemption, a tool run per
# trigger GET). The web chat family is wider because ONE first page load is the page
# plus every bundle file it links, all in one burst.
SHIPPED_FAMILY_BUDGETS: dict[str, FamilyOverride] = {
    "universal_webhook": FamilyOverride(limit=60, burst=10),
    "interactions_callback": FamilyOverride(limit=60, burst=10),
    "trigger": FamilyOverride(limit=60, burst=10),
    "channels_web": FamilyOverride(limit=120, burst=30),
}


def _first_set[T](*candidates: T | None) -> T:
    """The first candidate that is not ``None``. The last one is always a concrete
    default, so a missing value is a programming error and raises rather than
    resolving to something invented."""
    for candidate in candidates:
        if candidate is not None:
            return candidate
    raise ValueError("no budget value resolved: the last candidate must be a concrete default")


class RateLimitRedisSettings(RedisConnectionSettings):
    """Redis holding the per-bucket fixed-window counters. Connection values come
    from the ``TAI_RATE_LIMIT_REDIS_*`` env, or the shared ``TAI_DEFAULT_REDIS_URL``;
    absent = rate limiting is OFF (pass-through)."""

    model_config = SettingsConfigDict(env_prefix="TAI_RATE_LIMIT_")

    redis_url: str | None = None
    redis_max_connections: int | None = 10

    # A black-holed Redis fails the rate-limit counter op loudly within 5s instead
    # of hanging the request: the connect phase and each command read are both
    # bounded. Must be positive.
    socket_connect_timeout: float | None = Field(default=5, gt=0)
    socket_timeout: float | None = Field(default=5, gt=0)


class RateLimitSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="TAI_RATE_LIMIT_", env_nested_delimiter="__")

    # Infra: the redis connection is composed from the kit (a field, not a base),
    # so this config declares no connection fields of its own.
    redis: RateLimitRedisSettings = Field(default_factory=RateLimitRedisSettings)

    # Namespace prefix for every rate-limit counter key.
    key_prefix: str = "ratelimit:"

    # The budget every derived door family starts from — the flood backstop a public
    # door gets with no configuration at all. Deliberately wide: it must not throttle
    # a legitimate browser fan-out (one page load is the shell plus every asset) or a
    # busy vendor webhook, and a family that needs a tighter ceiling states it in
    # SHIPPED_FAMILY_BUDGETS or in ``families``. Must be positive.
    default_enabled: bool = True
    default_limit: int = Field(default=600, gt=0)
    default_burst: int = Field(default=120, gt=0)

    # Per-family overrides, keyed by the family name the middleware derives from the
    # door's path. Set from env as JSON
    # (``TAI_RATE_LIMIT_FAMILIES='{"trigger": {"limit": 10}}'``) or per field
    # (``TAI_RATE_LIMIT_FAMILIES__TRIGGER__LIMIT=10``). An entry for a family no route
    # declares is inert — it can never open a door, only tune one.
    families: dict[str, FamilyOverride] = Field(default_factory=dict)

    @field_validator("families")
    @classmethod
    def _validate_family_names(cls, value: dict[str, FamilyOverride]) -> dict[str, FamilyOverride]:
        """Family names are lower-case; env-set keys arrive in whatever case the
        operator typed, so they are folded once here rather than at every lookup."""
        return {name.lower(): override for name, override in value.items()}

    def budget_for(self, family: str) -> FamilyBudget:
        """The budget charged to ``family``: the operator's ``families`` override
        first, then the shipped per-family default, then the ``default_*`` budget.
        Each of the three fields resolves independently, so an override naming only a
        limit keeps the shipped burst."""
        shipped = SHIPPED_FAMILY_BUDGETS.get(family, FamilyOverride())
        operator = self.families.get(family, FamilyOverride())
        return FamilyBudget(
            enabled=_first_set(operator.enabled, shipped.enabled, self.default_enabled),
            limit=_first_set(operator.limit, shipped.limit, self.default_limit),
            burst=_first_set(operator.burst, shipped.burst, self.default_burst),
        )

    def any_family_enabled(self) -> bool:
        """Whether the limiter can charge ANY door family — false only when the
        default is off and no override turns a family back on. The readiness probe
        rides this to decide whether the counter store is a wired dependency."""
        if self.default_enabled:
            return True
        return any(override.enabled for override in self.families.values())


@settings_cache
def rate_limit_settings() -> RateLimitSettings:
    return RateLimitSettings()
