"""Bring credentials up to date before a build lists the servers that demand one.

A build runs on the serving loop and blocks it while it lists, so inside a build the refresh owner
cannot run: a token that expired while its connection sat idle would leave that server's tools unbound
until the next re-probe. This step runs on the free serving loop just before a build (and once after
boot's), bounded by one probe budget, and lets whatever is not ready at the bound run on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from tai42_contract.connectors.errors import ConnectorError
from tai42_contract.manifest import TaiMCPConfig

from tai42_skeleton.connectors.runtime import refresh_owner
from tai42_skeleton.connectors.runtime.resolver import ConnectorConnectionError, resolve_managed_auth
from tai42_skeleton.connectors.token_injection import require_own_server
from tai42_skeleton.manifest.transport import detect_transport

logger = logging.getLogger(__name__)


def _selected(entries: Iterable[TaiMCPConfig]) -> list[TaiMCPConfig]:
    """The managed OAuth entries that talk to a ``url`` and pass the provider's-own-server check."""
    selected: list[TaiMCPConfig] = []
    for config in entries:
        if config.managed is None or detect_transport(config.config) != "http":
            continue
        try:
            descriptor = require_own_server(config)
        except ConnectorConnectionError:
            # Refused (logged at ERROR by the check): nothing is resolved for it.
            continue
        if descriptor is not None and descriptor.kind == "oauth":
            selected.append(config)
    return selected


async def _bring_one(config: TaiMCPConfig) -> None:
    """A fresh stored credential needs nothing (and nothing is written); otherwise the ordinary resolve."""
    ref = config.managed
    if ref is None:
        raise RuntimeError(f"config {config.title!r} is not connector-managed")
    stored = await resolve_managed_auth(ref.connection_id, ref.provider_id, ref.sub_service, allow_refresh=False)
    if stored is not None:
        return
    await resolve_managed_auth(ref.connection_id, ref.provider_id, ref.sub_service)


def _connection_of(config: TaiMCPConfig) -> str:
    return config.managed.connection_id if config.managed is not None else "-"


def _consume_late(config: TaiMCPConfig, task: asyncio.Task[None]) -> None:
    """Retrieve the outcome of a wrapper the step stopped waiting for; a failure other than a typed one is logged."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None and not isinstance(exc, ConnectorError):
        logger.warning(
            "connectors: bringing the credential of connection %s up to date failed after the step's bound",
            _connection_of(config),
            exc_info=exc,
        )


async def bring_credentials_up_to_date(entries: Iterable[TaiMCPConfig], *, timeout: float) -> dict[str, bool]:
    """Read, and where needed refresh, the credentials of ``entries`` concurrently, for at most ``timeout`` in total.

    ``entries`` are the ones whose server demanded a credential at their last listing; of them only the
    managed OAuth entries that talk to a ``url`` and pass the provider's-own-server check are covered. A
    fresh stored credential is left as it is (nothing written); otherwise the ordinary resolve refreshes
    through the refresh owner. Leaving at the bound cancels nothing: the owner's refreshes run on, and each
    wrapper left behind has its outcome consumed. Returns, per covered title, whether a credential is now
    in hand. Raises nothing: a typed connector error is counted; any other failure is logged at WARNING
    with the connection id; one INFO line gives the counts. With no live refresh owner it does nothing.
    """
    if not refresh_owner.is_live():
        return {}
    selected = _selected(entries)
    if not selected:
        return {}
    tasks = {config.title: (config, asyncio.ensure_future(_bring_one(config))) for config in selected}
    _done, pending = await asyncio.wait([task for _config, task in tasks.values()], timeout=timeout)
    in_hand: dict[str, bool] = {}
    failed = 0
    for title, (config, task) in tasks.items():
        if task in pending:
            task.add_done_callback(lambda done, config=config: _consume_late(config, done))
            in_hand[title] = False
            continue
        exc = task.exception()
        in_hand[title] = exc is None
        if exc is None:
            continue
        failed += 1
        if not isinstance(exc, ConnectorError):
            logger.warning(
                "connectors: bringing the credential of connection %s up to date failed",
                _connection_of(config),
                exc_info=exc,
            )
    logger.info(
        "connectors: credentials brought up to date before listing: in_hand=%d failed=%d still_running=%d",
        sum(in_hand.values()),
        failed,
        len(pending),
    )
    return in_hand
