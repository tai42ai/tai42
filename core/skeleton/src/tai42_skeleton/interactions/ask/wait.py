"""The synchronous answer wait for an interaction question.

Block for the reply on a fresh connection within the remaining budget, prune on cancel/timeout, and
return the typed answer (a sensitive answer wrapped) or raise the timeout.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tai42_contract.secrets import SecretValue
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.interactions.store import InteractionStore
from tai42_skeleton.tools.turn_budget import mark_parked_question

from .delivery import prune
from .errors import InteractionTimeoutError
from .timing import DeadlineWindow


async def await_answer(
    store: InteractionStore,
    settings: InteractionsSettings,
    window: DeadlineWindow,
    *,
    interaction_id: str,
    group: str,
    reply_to: str,
    question: str,
    sensitive: bool,
) -> Any:
    """Block for the answer within what is LEFT of the budget after delivery, returning the typed answer.

    Uses the same monotonic ``deadline`` the delivery attempts ran against, wrapping a ``sensitive`` answer
    in ``SecretValue``. On timeout or cancel the question is pruned (else it inflates the group count and
    stays claimable by a late callback); a timeout then raises ``InteractionTimeoutError`` naming which of
    the three end states the question reached.
    """
    loop = asyncio.get_running_loop()
    # Redis BLPOP reads its timeout at 1ms resolution and a 0/negative timeout as
    # "block forever", so a sub-millisecond remainder degrades to 0 = block-forever too
    # — anything below 1ms skips the wait entirely and takes the timeout path directly.
    remaining = window.deadline - loop.time()
    if remaining < 0.001:
        response = None
    else:
        try:
            # Strip the socket read timeout on this connection only: the BLPOP blocks
            # legitimately for the remaining wait, so a blanket read timeout would kill
            # it. The store wraps the BLPOP in an outer wait_for (the passed timeout +
            # grace) instead, so a black-holed redis still fails loudly. Block on a
            # dedicated connection: pinning one from the shared pool would starve other
            # concurrent ask_user calls once the pool is drained.
            from tai42_skeleton.interactions import helper

            reply_redis = settings.redis.model_copy(update={"socket_timeout": None})
            async with helper.client_ctx(RedisClient, reply_redis, fresh=True) as reply_conn:
                response = await store.wait_for_reply(reply_conn, reply_to, remaining, settings.blocking_grace_seconds)
        except asyncio.CancelledError as exc:
            # Prune on cancel so an abandoned question does not inflate the group count /
            # open index. The status gate makes the cancelled-after-answer race a no-op.
            # A cleanup failure propagates (chained on the CancelledError context).
            mark_parked_question(exc, interaction_id, question, sensitive)
            await prune(settings, store, interaction_id, group)
            raise
    if response is None:
        # Timeout: prune first, else the abandoned question inflates the group count
        # until the idle TTL and stays claimable by a late callback. The prune result
        # names which of the three end states the question reached.
        result = await prune(settings, store, interaction_id, group)
        if result == "pruned":
            raise InteractionTimeoutError(
                f"ask_user timed out after {window.budget}s with no answer (interaction {interaction_id})"
            )
        if result == "answered":
            raise InteractionTimeoutError(
                f"ask_user timed out after {window.budget}s; an answer was recorded after the budget "
                f"and was not returned (interaction {interaction_id})"
            )
        raise InteractionTimeoutError(
            f"ask_user timed out after {window.budget}s; the question record was already gone "
            f"(expired or pruned elsewhere) and no answer was returned (interaction {interaction_id})"
        )
    # A sensitive answer is handed back wrapped so it cannot leak through a repr, a log
    # line, or a JSON dump — the caller reveals it deliberately.
    if sensitive:
        return SecretValue(response.answer)
    return response.answer
