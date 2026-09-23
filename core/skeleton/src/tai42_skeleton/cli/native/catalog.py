"""``tai catalog`` — the ecosystem catalog, marketplace-backed.

The tai42-skeleton builtins are a static table colocated here (the core is not a
marketplace listing, so nothing else carries them); every other row is queried live
from the marketplace registry's item-enumeration route. The network is REQUIRED —
offline is a loud error, never a silent empty or cached fallback.
"""

from __future__ import annotations

import asyncio
from typing import Any

import click
import typer
from tai42_cli.commands._common import app_context
from tai42_cli.render import print_records

from tai42_skeleton.marketplace.client import RegistryClient
from tai42_skeleton.marketplace.errors import MarketplaceError, RegistryResponseError

# The identity fields a marketplace item enumeration row MUST carry to project into a
# catalog row. The top-level fields name the owning listing; the nested ``item`` object
# carries the per-registration identity. A dict-shaped-but-key-missing row — or a
# missing/non-object ``item`` — is garbled registry data → a typed
# RegistryResponseError (a MarketplaceError), so ``catalog()`` renders the uniform CLI
# error instead of letting a bare KeyError/TypeError escape as a raw traceback.
# ``module``/``description`` are non-identifying and default rather than forcing the
# whole catalog to fail.
_ROW_IDENTITY_FIELDS = ("namespace", "listing", "package")
_ITEM_IDENTITY_FIELDS = ("kind", "name")

# Columns rendered in the human table (JSON output carries the raw records). The
# catalog does not surface the item's optional editorial ``group`` label; ``source`` is
# ``"builtin"`` for the static rows and ``"<namespace>/<listing>"`` for marketplace
# rows; ``module`` renders empty for mcp-server items (the contract forbids a module
# there).
_COLUMNS = ["name", "kind", "package", "source", "module", "description"]

# The tai42-skeleton builtins — the ONE place they live (the core is not a
# marketplace listing, so nothing else carries them). test_native_catalog.py pins this
# table to the actual builtin registrations so it cannot rot silently.
_BUILTIN_ROWS: list[dict[str, str]] = [
    {
        "name": "ask",
        "kind": "tool",
        "package": "tai42-skeleton",
        "source": "builtin",
        "module": "tai42_skeleton.tools.builtin.interactions",
        "description": "Ask a human a question mid-run and block until they answer.",
    },
    {
        "name": "list_parked",
        "kind": "tool",
        "package": "tai42-skeleton",
        "source": "builtin",
        "module": "tai42_skeleton.tools.builtin.interactions",
        "description": "List the interactions parked on the current run's subject.",
    },
    {
        "name": "resume_parked",
        "kind": "tool",
        "package": "tai42-skeleton",
        "source": "builtin",
        "module": "tai42_skeleton.tools.builtin.interactions",
        "description": "Resume a parked caller ask with an answer, or take a resolved run's waiting outcome.",
    },
    {
        "name": "cancel_parked",
        "kind": "tool",
        "package": "tai42-skeleton",
        "source": "builtin",
        "module": "tai42_skeleton.tools.builtin.interactions",
        "description": "Cancel the parked interactions named on the current run's subject.",
    },
    {
        "name": "state_read",
        "kind": "tool",
        "package": "tai42-skeleton",
        "source": "builtin",
        "module": "tai42_skeleton.tools.builtin.states",
        "description": "Read the calling subject's document for a state.",
    },
    {
        "name": "state_replace",
        "kind": "tool",
        "package": "tai42-skeleton",
        "source": "builtin",
        "module": "tai42_skeleton.tools.builtin.states",
        "description": "Replace the calling subject's whole document for a state.",
    },
    {
        "name": "state_merge",
        "kind": "tool",
        "package": "tai42-skeleton",
        "source": "builtin",
        "module": "tai42_skeleton.tools.builtin.states",
        "description": "Shallow-merge a patch into the calling subject's document for a state.",
    },
    {
        "name": "state_apply",
        "kind": "tool",
        "package": "tai42-skeleton",
        "source": "builtin",
        "module": "tai42_skeleton.tools.builtin.states",
        "description": "Apply a batch of path operations to the calling subject's document for a state.",
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
        "name": "set_conversation_mode",
        "kind": "tool",
        "package": "tai42-skeleton",
        "source": "builtin",
        "module": "tai42_skeleton.tools.builtin.set_conversation_mode",
        "description": "Flip the current conversation between agent and manual control.",
    },
    {
        "name": "send_conversation_message",
        "kind": "tool",
        "package": "tai42-skeleton",
        "source": "builtin",
        "module": "tai42_skeleton.tools.builtin.doors",
        "description": "Send a message to a conversation route under the deployment's own identity.",
    },
    {
        "name": "send_conversation_event",
        "kind": "tool",
        "package": "tai42-skeleton",
        "source": "builtin",
        "module": "tai42_skeleton.tools.builtin.doors",
        "description": "Deliver a structured event to a conversation thread under the deployment's own identity.",
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


def _project(row: dict[str, Any]) -> dict[str, str]:
    """Project one marketplace item enumeration row into the catalog columns.

    A row carries the owning ``namespace``/``listing``/``package`` at the top level and
    the per-registration ``item`` object (``kind``/``name``/``module``/``description``/
    ``group``). ``source`` is the owning ``<namespace>/<listing>``; ``module`` is empty
    for an mcp-server item (its route field is ``null``, never ``""``, so normalize here).

    A row missing a top-level identity field, or whose ``item`` is missing/not an object
    or is itself missing an identity field, is garbled registry data →
    :class:`RegistryResponseError` (a :class:`MarketplaceError`), so the caller renders
    the uniform CLI error rather than a bare ``KeyError``/``TypeError`` traceback.
    ``description`` is non-identifying and defaults to ``""``.
    """
    for field in _ROW_IDENTITY_FIELDS:
        if field not in row:
            raise RegistryResponseError(f"marketplace item row is missing the required {field!r} field", status=None)
    item = row.get("item")
    if not isinstance(item, dict):
        raise RegistryResponseError("marketplace item row is missing the required 'item' object", status=None)
    for field in _ITEM_IDENTITY_FIELDS:
        if field not in item:
            raise RegistryResponseError(
                f"marketplace item row is missing the required 'item.{field}' field", status=None
            )
    return {
        "name": item["name"],
        "kind": item["kind"],
        "package": row["package"] or "",
        "source": f"{row['namespace']}/{row['listing']}",
        "module": item.get("module") or "",
        "description": item.get("description") or "",
    }


def load_catalog() -> list[dict[str, Any]]:
    """The full catalog: the static skeleton builtins followed by every listed plugin's items.

    The plugin items are queried live from the marketplace registry. The network is required — a
    dead or garbled registry raises a
    :class:`~tai42_skeleton.marketplace.errors.MarketplaceError`, never a silent empty list or a
    cached snapshot.
    """
    rows = asyncio.run(RegistryClient().items())
    return [*_BUILTIN_ROWS, *(_project(row) for row in rows)]


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
