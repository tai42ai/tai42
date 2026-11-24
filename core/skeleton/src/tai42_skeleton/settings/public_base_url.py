"""``TAI_PUBLIC_BASE_URL``: the deployment's one public address.

The URL at which callers reach this deployment from outside: scheme, host, optional
port, and the path prefix when the app is mounted under one. It is read wherever the
deployment hands out an absolute URL of its own: the callback URL mint and the pin
that checks a stored callback URL, the absolute served-media URL a channel delivery
carries, the MCP door's advertised resource and protected-resource metadata URLs, and
a channel plugin's webhook registration (through ``tai42_app.http.public_base_url()``).
Nothing is ever derived from a request's own host or scheme.

Stored normalised the way an MCP client normalises the value it sends back as
``resource``: scheme and host lower-cased, a default port dropped, a trailing slash
stripped, the path kept as typed.
"""

import re
from typing import ClassVar
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import SettingsConfigDict
from tai42_contract.urls import is_loopback_host
from tai42_kit.settings import TaiBaseSettings, settings_cache

PUBLIC_BASE_URL_SETTING = "TAI_PUBLIC_BASE_URL"
_DEFAULT_PORTS = {"https": 443, "http": 80}
_PATH_CHARACTERS = re.compile(r"[A-Za-z0-9._~/-]*")


def _refuse_public_base_url(value: str, reason: str) -> ValueError:
    return ValueError(f"{PUBLIC_BASE_URL_SETTING} {value!r} is refused: {reason}")


def normalise_public_base_url(value: str) -> str:
    """The normalised form of a public base URL, or ``ValueError`` naming the setting.

    Absolute ``https`` (``http`` only on a loopback host), no query, fragment or userinfo;
    scheme and host lower-cased, a default port dropped, a trailing slash stripped, the path
    kept as typed. Refuses a value a URL library would rewrite further: a non-ASCII host, a
    path character outside ``A-Z a-z 0-9 . _ ~ - /``, or a ``.``/``..``/empty path segment.
    """
    if "?" in value or "#" in value:
        raise _refuse_public_base_url(value, "it must carry no query and no fragment")
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    if "@" in parts.netloc:
        raise _refuse_public_base_url(value, "it must carry no userinfo")
    host = parts.hostname
    if not host:
        raise _refuse_public_base_url(value, "it must be an absolute URL with a host")
    if not host.isascii():
        raise _refuse_public_base_url(value, "write an internationalised host in its ASCII form")
    if scheme != "https" and not (scheme == "http" and is_loopback_host(host)):
        raise _refuse_public_base_url(value, "it must be https:// (http:// only on a loopback host)")
    try:
        port = parts.port
    except ValueError as exc:
        raise _refuse_public_base_url(value, f"its port is invalid ({exc})") from exc
    path = parts.path.removesuffix("/")
    if not _PATH_CHARACTERS.fullmatch(path):
        raise _refuse_public_base_url(value, "its path may hold only A-Z a-z 0-9 . _ ~ - /")
    if path and any(segment in ("", ".", "..") for segment in path[1:].split("/")):
        raise _refuse_public_base_url(value, "its path must have no '.', '..' or empty segment")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"
    return f"{scheme}://{netloc}{path}"


class PublicBaseUrlSettings(TaiBaseSettings):
    """``TAI_PUBLIC_*`` settings: the deployment's public address."""

    model_config = SettingsConfigDict(env_prefix="TAI_PUBLIC_")
    env_prefix_owned: ClassVar[bool] = True

    # ``https`` (or ``http`` on a loopback host, for local development), no query,
    # fragment or userinfo. The callback URL minted from it is a bearer capability and
    # its POST door carries sensitive data, so a plain-http public host is refused at
    # load. A value a URL library would rewrite further (a non-ASCII host, a path
    # character outside ``A-Z a-z 0-9 . _ ~ - /``, a ``.``/``..`` or empty path segment)
    # is refused at load too. Unset, nothing that needs an absolute URL of the
    # deployment is offered: external-format asks and channel deliveries refuse, the MCP
    # door advertises no sign-in.
    base_url: str | None = None

    @field_validator("base_url")
    @classmethod
    def _normalise_base_url(cls, value: str | None) -> str | None:
        return None if value is None else normalise_public_base_url(value)


@settings_cache
def public_base_url_settings() -> PublicBaseUrlSettings:
    """The cached :class:`PublicBaseUrlSettings`, re-read on a settings reload."""
    return PublicBaseUrlSettings()
