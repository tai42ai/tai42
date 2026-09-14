"""The ask's budget/deadline and callback-ticket computation: derive the stored
deadline + the monotonic answer-wait deadline + the async-park TTL margin, and mint
the callback ticket + URL when an external/channel ask forces one."""

from __future__ import annotations

import asyncio
import math
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from tai42_skeleton.interactions.settings import InteractionsSettings


@dataclass(frozen=True)
class DeadlineWindow:
    """The ask's time window: the answer ``budget``, the ``created_at`` anchor, the
    STORED ``timeout_at`` (sync budget or async ``expiry_at``), the monotonic
    ``deadline`` bounding the synchronous phase, and the async-park TTL margin."""

    budget: float
    created_at: datetime
    timeout_at: datetime
    deadline: float
    park_ttl_margin_seconds: int


@dataclass(frozen=True)
class CallbackTicket:
    """A minted callback capability: the opaque ticket, its TTL, and the public
    callback URL the human (or channel) acts on."""

    ticket: str
    ticket_ttl: int
    callback_url: str


def resolve_deadline(
    mode: str, timeout: float | None, expiry_at: datetime | None, settings: InteractionsSettings
) -> DeadlineWindow:
    """Compute the ask's ``budget``, ``created_at``, stored ``timeout_at``, the
    monotonic ``deadline`` for the synchronous phase, and the async-park TTL margin.

    A sync question's stored deadline is its answer budget; an async park's is its
    ``expiry_at`` (no caller blocks on it — the expiry reaper resumes work). ONE
    monotonic ``deadline`` bounds the synchronous phase (delivery attempts + backoff +
    the sync answer wait), so delivery time shrinks a sync caller's wait and a sync
    caller can never block past the budget. The park TTL margin ties the state
    ``_key_ttl`` and the callback ticket TTL to the reaper interval so a park's keys
    outlive its ``expiry_at`` by enough reaper passes to fire."""
    budget = settings.answer_timeout_seconds if timeout is None else timeout
    if budget <= 0:
        # Redis BLPOP treats 0 as "block forever" — the opposite of no-wait — so a
        # non-positive budget can never mean anything sane here.
        raise ValueError(f"timeout must be positive, got {budget!r}")
    created_at = datetime.now(UTC)
    if mode == "async":
        assert expiry_at is not None  # async requires expiry_at (validated up front)
        timeout_at = expiry_at
    else:
        timeout_at = created_at + timedelta(seconds=budget)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    # An async park's keys must outlive its ``expiry_at`` by enough for the reaper to
    # fire — at least a couple of its passes. Tie the margin to the reaper interval so
    # it holds under any configured cadence. This is the ONE source for the async-park
    # margin: the state ``_key_ttl`` and the callback ticket TTL both add this value,
    # so ``expiry_at + margin`` is the shared answerable window for the authenticated
    # ``/answer`` door and the channel-delivery callback door alike.
    park_ttl_margin_seconds = 2 * math.ceil(settings.expiry_reaper_interval_seconds)
    return DeadlineWindow(
        budget=budget,
        created_at=created_at,
        timeout_at=timeout_at,
        deadline=deadline,
        park_ttl_margin_seconds=park_ttl_margin_seconds,
    )


def mint_callback_ticket(
    settings: InteractionsSettings, window: DeadlineWindow, mode: str, *, force: bool
) -> CallbackTicket | None:
    """Mint the callback ticket + URL when ``force`` (external format or a channel
    delivers the question, which bridges the reply back through the public callback
    door), else ``None``. The ticket TTL is the seconds to the stored deadline (ceiled,
    floor 1s) plus — for an async park — the same reaper margin the state ``_key_ttl``
    uses, so the callback door and the authenticated ``/answer`` door stay answerable
    through the identical park window."""
    if not force:
        return None
    # The settings validator guarantees a set public_base_url is https:// (or localhost
    # http://); only absence is checked here.
    if settings.public_base_url is None:
        raise RuntimeError(
            "external answer_format (and channel delivery) requires INTERACTIONS_PUBLIC_BASE_URL to be set"
        )
    ticket = secrets.token_urlsafe(32)
    ticket_ttl = max(1, math.ceil((window.timeout_at - window.created_at).total_seconds()))
    if mode == "async":
        ticket_ttl += window.park_ttl_margin_seconds
    callback_url = f"{settings.public_base_url.rstrip('/')}/api/interactions/callback/{ticket}"
    return CallbackTicket(ticket=ticket, ticket_ttl=ticket_ttl, callback_url=callback_url)
