"""Named errors for unconfigured connection settings.

A feature whose connection identity is unset must fail LOUDLY at use, naming the
env var that turns it on — never a raw ``AttributeError``/``TypeError`` from a
``None`` reaching connection building, and never a silent localhost default. The
message shape is fixed so every gate reads the same:

    ``<subject> is not configured: set <ENV_VAR>.``

extended, where the field also participates in the shared ``TAI_DEFAULT_*``
namespace, with that alternative:

    ``<subject> is not configured: set <ENV_VAR> (or TAI_DEFAULT_REDIS_URL).``
"""

from pydantic import SecretStr


def not_configured_message(subject: str, env_var: str, default_var: str | None = None) -> str:
    """The named-error message for an unset connection setting.

    ``default_var`` is the field's ``TAI_DEFAULT_*`` alternative, named in the
    message when the field participates in the shared namespace; omit it for a
    field that only its own env var can set."""
    if default_var is None:
        return f"{subject} is not configured: set {env_var}."
    return f"{subject} is not configured: set {env_var} (or {default_var})."


def require[T](value: T | None, subject: str, env_var: str, default_var: str | None = None) -> T:
    """The configured ``value``, or raise the named error when it is ``None``."""
    if value is None:
        raise ValueError(not_configured_message(subject, env_var, default_var))
    return value


def require_secret(value: SecretStr | None, subject: str, env_var: str, default_var: str | None = None) -> str:
    """The secret's plaintext, or raise on unset OR empty — fail CLOSED.

    A secret that gates a trust boundary must not be satisfiable by an empty
    value, so both the unset and the empty cases raise."""
    secret = require(value, subject, env_var, default_var).get_secret_value()
    if not secret:
        raise ValueError(f"{env_var} is set but empty")
    return secret
