"""Outbound HTTP for the Slack channel — kit's pooled ``HttpxClient`` and the Web API calls.

Every outbound call (the ``chat.postMessage``/``views.open``/``views.update`` Web API calls
and the loopback callback forward) shares one pooled ``httpx.AsyncClient`` per event loop,
budgeted by ``CHANNEL_SLACK_HTTP_TIMEOUT_SECONDS``. Slack answers HTTP 200 even for most Web
API errors, so a call's success is its JSON ``ok`` field; any failure raises
:class:`~tai42_contract.channels.ChannelDeliveryError` carrying Slack's documented error
fields, and an unset bot setting is the same delivery failure, raised before any network call.
The Events API handler forwards the answer before acking within Slack's 3 s window; a slower
forward surfaces as a loud handler failure that Slack's retry (plus the dedupe rollback in
``inbound``) recovers.
"""

from __future__ import annotations

import json
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
from pydantic import SecretStr
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError
from tai42_kit.clients.impl.http import HttpxClient
from tai42_kit.settings import require, require_secret

from tai42_channel_slack.settings import slack_settings


def slack_http() -> AbstractAsyncContextManager[httpx.AsyncClient]:
    """A pooled outbound client budgeted by ``CHANNEL_SLACK_HTTP_TIMEOUT_SECONDS``."""
    return tai42_app.clients.client_ctx(HttpxClient, timeout=slack_settings().http_timeout_seconds)


def _require_for_delivery[T](value: T | None, env_name: str) -> T:
    """The configured value, or :class:`ChannelDeliveryError` naming the env var.

    A config gap on the deliver/notify path is a delivery failure; wraps
    ``require``, raised before any network call.
    """
    try:
        return require(value, "the slack channel", env_name)
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc


def _require_secret_for_delivery(value: SecretStr | None, env_name: str) -> str:
    """The secret's plaintext, or :class:`ChannelDeliveryError` on unset/EMPTY.

    Fail closed; wraps ``require_secret``, message names only the env var.
    """
    try:
        return require_secret(value, "the slack channel", env_name)
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc


async def _post_message(
    token: str, target: str, text: str, blocks: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """POST one ``chat.postMessage`` and return its validated JSON body.

    ``blocks`` (Block Kit) rides alongside ``text``; ``text`` is kept as the
    notification fallback Slack requires even when blocks carry the content. Any
    failure — transport error, non-200 status, non-JSON body, or ``ok`` not true —
    raises :class:`ChannelDeliveryError`. Slack answers HTTP 200 even for errors,
    so the JSON ``ok`` field is the ONLY success signal.
    """
    payload: dict[str, Any] = {"channel": target, "text": text}
    if blocks is not None:
        payload["blocks"] = blocks
    try:
        async with slack_http() as client:
            response = await client.post(
                f"{slack_settings().api_base_url}/chat.postMessage",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise ChannelDeliveryError(f"chat.postMessage transport failure: {exc}") from exc
    if response.status_code != 200:
        raise ChannelDeliveryError(f"chat.postMessage returned HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise ChannelDeliveryError("chat.postMessage returned a non-JSON body") from exc
    if body.get("ok") is not True:
        raise ChannelDeliveryError(f"chat.postMessage failed: {_error_detail(body)}")
    return body


async def open_modal_view(trigger_id: str, view: dict[str, Any]) -> None:
    """POST one ``views.open`` for a form question's modal, or raise.

    Called inline from the interactivity door on a ``tai42_form_open`` click, with
    the ``block_actions`` ``trigger_id`` (valid ~3 s). Any failure — transport
    error, non-200 status, non-JSON body, or ``ok`` not true — raises
    :class:`ChannelDeliveryError`; the door lets it surface as a loud 500 so Slack
    reports the failure to open. Settings (bot token, api base) read fresh.
    """
    settings = slack_settings()
    token = _require_secret_for_delivery(settings.bot_token, "CHANNEL_SLACK_BOT_TOKEN")
    try:
        async with slack_http() as client:
            response = await client.post(
                f"{settings.api_base_url}/views.open",
                headers={"Authorization": f"Bearer {token}"},
                json={"trigger_id": trigger_id, "view": view},
            )
    except httpx.HTTPError as exc:
        raise ChannelDeliveryError(f"views.open transport failure: {exc}") from exc
    if response.status_code != 200:
        raise ChannelDeliveryError(f"views.open returned HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise ChannelDeliveryError("views.open returned a non-JSON body") from exc
    if body.get("ok") is not True:
        raise ChannelDeliveryError(f"views.open failed: {_error_detail(body)}")


async def update_modal_view(view_id: str, view_hash: str | None, view: dict[str, Any]) -> None:
    """POST one ``views.update`` to re-render an open form modal, or raise.

    Called inline from the interactivity door on a reacting/conditional field change: the
    modal is rebuilt (show/hide, reaction values/options/errors/display) and pushed under its
    ``view_id``. ``view_hash`` (when the payload carried one) guards against a stale update —
    Slack rejects an outdated hash. Any failure — transport error, non-200 status, non-JSON
    body, or ``ok`` not true — raises :class:`ChannelDeliveryError`; the door lets it surface
    as a loud 500. Settings (bot token, api base) read fresh.
    """
    settings = slack_settings()
    token = _require_secret_for_delivery(settings.bot_token, "CHANNEL_SLACK_BOT_TOKEN")
    payload: dict[str, Any] = {"view_id": view_id, "view": view}
    if view_hash is not None:
        payload["hash"] = view_hash
    try:
        async with slack_http() as client:
            response = await client.post(
                f"{settings.api_base_url}/views.update",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise ChannelDeliveryError(f"views.update transport failure: {exc}") from exc
    if response.status_code != 200:
        raise ChannelDeliveryError(f"views.update returned HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise ChannelDeliveryError("views.update returned a non-JSON body") from exc
    if body.get("ok") is not True:
        raise ChannelDeliveryError(f"views.update failed: {_error_detail(body)}")


# Slack's Web API error object, in the vendor's documented field order.
_ERROR_FIELDS = ("error", "warning", "needed", "provided", "response_metadata")


def _error_detail(body: dict[str, Any]) -> str:
    """Slack's full documented error fields from an ``ok: false`` body, in one deterministic format.

    Each present field renders as a ``name=<render>`` token in Slack's fixed order —
    ``repr(value)`` for a scalar, compact sorted JSON for ``response_metadata`` —
    joined by one space and bounded to 500 chars. ``response_metadata`` carries the
    per-field validation ``messages`` a scope or block error reports.
    """
    tokens: list[str] = []
    for name in _ERROR_FIELDS:
        if name not in body:
            continue
        value = body[name]
        if isinstance(value, (list, dict)):
            render = json.dumps(value, sort_keys=True, separators=(",", ":"))
        else:
            render = repr(value)
        tokens.append(f"{name}={render}")
    return " ".join(tokens)[:500]
