"""Settings for the hosted UI: where the built Studio app lives, and the page addresses it serves.

Follows the ``metrics_settings`` precedent — a ``TaiBaseSettings`` subclass plus a
``@settings_cache`` accessor, so a reload re-reads it and tests monkeypatch the
accessor. The dist path is a per-deployment/env concern (like the metrics ports),
NOT manifest content: it is deliberately NOT the contract Manifest's ``static_dir``
field, so the two static-dir concepts stay separate.
"""

import re
from typing import ClassVar

from pydantic import ValidationInfo, field_validator
from pydantic_settings import SettingsConfigDict
from tai42_contract.app import HostedUiPaths
from tai42_kit.settings import TaiBaseSettings, settings_cache


class PluginsSettings(TaiBaseSettings):
    """Settings for the Studio-plugin SPA host — where the built Studio app dist lives."""

    # Absolute (or CWD-relative) path to the built Studio SPA dist directory —
    # the ``index.html``, the hashed asset bundles, the stable ``vendor/`` ESM
    # assets, and the static OAuth pages. Unset (None) means this deployment does
    # not host the Studio UI: the SPA catch-all route is always registered (this
    # setting is reload-mutable, so registration cannot depend on it) and answers
    # every request with 404 while the path is unset.
    studio_dist_path: str | None = None


@settings_cache
def plugins_settings() -> PluginsSettings:
    """Return the process-wide :class:`PluginsSettings`, cached after first load."""
    return PluginsSettings()


_URL_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")

SAME_ORIGIN_PATH_RE = re.compile(r"/[A-Za-z0-9_\-./?=&%]*")
"""A same-origin page path (fullmatch, and never ``//``): the characters a composed address keeps as they are.

``//host`` is a protocol-relative URL a browser treats as another origin; a ``#`` or a space would
cut or break the address built from the path.
"""


def is_same_origin_path(value: str) -> bool:
    """Whether ``value`` is a same-origin page path by :data:`SAME_ORIGIN_PATH_RE`."""
    return SAME_ORIGIN_PATH_RE.fullmatch(value) is not None and not value.startswith("//")


class HostedUiSettings(TaiBaseSettings):
    """``HOSTED_UI_*``: the page addresses of the hosted UI this deployment serves.

    Claim links, invite links, the OAuth redirect URI and the connectors landing page are
    composed from these, so a hosted UI that serves its pages elsewhere declares it here.
    ``oauth_callback_path`` is registered with external OAuth providers: changing it registers
    a new dynamic client per origin, and every hand-registered app must allow the new address.
    """

    model_config = SettingsConfigDict(env_prefix="HOSTED_UI_")
    env_prefix_owned: ClassVar[bool] = True

    login_path: str = "/login"
    claim_fragment_key: str = "claim"
    invite_query_key: str = "invite"
    oauth_callback_path: str = "/oauth-bridge.html"
    connectors_path: str = "/connectors"

    @field_validator("login_path", "oauth_callback_path", "connectors_path")
    @classmethod
    def _same_origin_path(cls, value: str, info: ValidationInfo) -> str:
        name = f"HOSTED_UI_{str(info.field_name).upper()}"
        if not is_same_origin_path(value):
            raise ValueError(
                f"{name} must be a same-origin path of [A-Za-z0-9_\\-./?=&%] (start with '/', not '//'): {value!r}"
            )
        return value

    @field_validator("login_path")
    @classmethod
    def _login_path_carries_no_query(cls, value: str) -> str:
        # An invite link appends its own ``?<key>=<token>`` to the login path.
        if "?" in value:
            raise ValueError(
                f"HOSTED_UI_LOGIN_PATH must carry no '?' (an invite link appends its own query): {value!r}"
            )
        return value

    @field_validator("claim_fragment_key", "invite_query_key")
    @classmethod
    def _url_key(cls, value: str, info: ValidationInfo) -> str:
        if _URL_KEY_RE.fullmatch(value) is None:
            raise ValueError(f"HOSTED_UI_{str(info.field_name).upper()} must match [A-Za-z0-9_-]+")
        return value

    def paths(self) -> HostedUiPaths:
        """The declared addresses as the contract value plugins read through ``tai42_app.http.hosted_ui()``."""
        return HostedUiPaths(
            login_path=self.login_path,
            claim_fragment_key=self.claim_fragment_key,
            invite_query_key=self.invite_query_key,
            oauth_callback_path=self.oauth_callback_path,
            connectors_path=self.connectors_path,
        )


@settings_cache
def hosted_ui_settings() -> HostedUiSettings:
    """Return the process-wide :class:`HostedUiSettings`, cached after first load."""
    return HostedUiSettings()


def check_hosted_ui_settings() -> None:
    """Read the declaration at boot and on every reload, so a malformed value refuses there, naming the setting.

    Its readers sit behind public doors (the login page, a Connect) that would otherwise fail on first use.
    """
    hosted_ui_settings()
