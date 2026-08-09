"""``tai catalog`` — the ecosystem catalog, marketplace-backed.

The 6 tai42-skeleton builtins are a static table colocated here (the core is not a
marketplace listing, so nothing else carries them); every other row is queried live
from the marketplace registry's item-enumeration route. The network is REQUIRED —
offline is a loud error, never a silent empty or cached fallback (R3).
"""

from __future__ import annotations

import asyncio
from typing import Any

import click
import typer

from tai42_skeleton.cli.commands._common import app_context
from tai42_skeleton.cli.render import print_records
from tai42_skeleton.marketplace.client import RegistryClient
from tai42_skeleton.marketplace.errors import MarketplaceError, RegistryResponseError

# The identity fields every marketplace item row MUST carry to project into a catalog
# row. A dict-shaped-but-key-missing row is garbled registry data → a typed
# RegistryResponseError (a MarketplaceError), so ``catalog()`` renders the uniform CLI
# error instead of letting a bare KeyError escape as a raw traceback. ``description`` is
# non-identifying and defaults to "" rather than forcing the whole catalog to fail.
_ITEM_IDENTITY_FIELDS = ("name", "kind", "package", "namespace", "listing")

# Columns rendered in the human table (JSON output carries the raw records). There is
# no per-item editorial ``group`` (the marketplace model has none); ``source`` is
# ``"builtin"`` for the static rows and ``"<namespace>/<listing>"`` for marketplace
# rows; ``module`` renders empty for mcp-server items (the contract forbids a module
# there).
_COLUMNS = ["name", "kind", "package", "source", "module", "description"]

# The 6 tai42-skeleton builtins — the ONE place they live (the core is not a
# marketplace listing, so nothing else carries them). test_native_catalog.py pins this
# table to the actual builtin registrations so it cannot rot silently.
_BUILTIN_ROWS: list[dict[str, str]] = [
    {
        "name": "ask_user",
        "kind": "tool",
        "package": "tai42-skeleton",
        "source": "builtin",
        "module": "tai42_skeleton.tools.builtin.interactions",
        "description": "Ask a human a question mid-run and block until they answer.",
    },
    {
        "name": "file_loader",
        "kind": "tool",
        "package": "tai42-skeleton",
        "source": "builtin",
        "module": "tai42_skeleton.tools.builtin.file_loader",
        "description": "Load a file from a url or a storage resource id and return its content.",
    },
    {
        "name": "get_pairing_code",
        "kind": "tool",
        "package": "tai42-skeleton",
        "source": "builtin",
        "module": "tai42_skeleton.tools.builtin.get_pairing_code",
        "description": "Mint a single-use pair code for a channel conversation.",
    },
    {
        "name": "monitor",
        "kind": "extension",
        "package": "tai42-skeleton",
        "source": "builtin",
        "module": "tai42_skeleton.extensions.builtin.monitor",
        "description": "Trace a standalone tool call as one live span.",
    },
    {
        "name": "ask_external",
        "kind": "extension",
        "package": "tai42-skeleton",
        "source": "builtin",
        "module": "tai42_skeleton.extensions.builtin.ask_external",
        "description": "Wrap a callback-url tool into an external human-in-the-loop question.",
    },
    {
        "name": "shared_secret",
        "kind": "webhook-verifier",
        "package": "tai42-skeleton",
        "source": "builtin",
        "module": "tai42_skeleton.webhooks.builtin.shared_secret",
        "description": "Verify a universal_webhook topic against a shared header secret.",
    },
]


def _project(item: dict[str, Any]) -> dict[str, str]:
    """One marketplace item row → the catalog columns. ``source`` is the owning
    ``<namespace>/<listing>``; ``module`` is empty for an mcp-server item (its route
    field is ``null``, never ``""``, so normalize here).

    A dict-shaped row missing any identity field is garbled registry data →
    :class:`RegistryResponseError` (a :class:`MarketplaceError`), so the caller renders
    the uniform CLI error rather than a bare ``KeyError`` traceback. ``description`` is
    non-identifying and defaults to ``""``."""
    for field in _ITEM_IDENTITY_FIELDS:
        if field not in item:
            raise RegistryResponseError(f"marketplace item row is missing the required {field!r} field", status=None)
    return {
        "name": item["name"],
        "kind": item["kind"],
        "package": item["package"],
        "source": f"{item['namespace']}/{item['listing']}",
        "module": item.get("module") or "",
        "description": item.get("description") or "",
    }


def load_catalog() -> list[dict[str, Any]]:
    """The full catalog: the static skeleton builtins followed by every listed
    plugin's items, queried live from the marketplace registry.

    The network is required — a dead or garbled registry raises a
    :class:`~tai42_skeleton.marketplace.errors.MarketplaceError`, never a silent empty
    list or a cached snapshot (R3)."""
    items = asyncio.run(RegistryClient().items())
    return [*_BUILTIN_ROWS, *(_project(item) for item in items)]


def catalog(ctx: typer.Context) -> None:
    """Print the ecosystem catalog.

    Lists the tai42-skeleton builtins plus every marketplace-listed plugin's items,
    queried live from the registry. The network is REQUIRED — offline is a loud error
    (no cache, no offline fallback). ``--json`` (global) emits the raw records for
    scripting.
    """
    app_ctx = app_context(ctx)
    try:
        records = load_catalog()
    except MarketplaceError as exc:
        # A dead/garbled registry (unreachable or a malformed response) reads as the
        # uniform CLI error line, not a raw traceback.
        raise click.ClickException(str(exc)) from exc
    print_records(records, _COLUMNS, json_output=app_ctx.json_output)
