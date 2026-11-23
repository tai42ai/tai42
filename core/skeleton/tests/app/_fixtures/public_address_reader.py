"""A plugin module that hands out an absolute URL of the deployment through ``tai42_app.http``.

It reads the deployment's public address only through the contract handle, as any plugin
that registers a webhook or mints a link does, and adds its own path under it.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app

CALLBACK_PATH = "/hooks/acme"


def callback_url() -> str | None:
    """The absolute URL a caller outside the deployment reaches this plugin's hook at; ``None`` without an address."""
    base = tai42_app.http.public_base_url()
    return None if base is None else f"{base}{CALLBACK_PATH}"
