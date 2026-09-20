"""A timed trigger link honours its window: alive before, the uniform 404 after,
gone from the list, and its NAME reusable — the both-keys-EX invariant on real Redis
(the record and its name index expire together, so a same-name re-create succeeds).

The wait is a pure time predicate polled without a sleep (nothing touches the token
during the window), so the post-window 404 can only be expiry."""

from __future__ import annotations

import time
from collections.abc import Callable

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

from ._trigger_support import MISS_MESSAGE, mint_link, no_auth, register_record_hook, wait_records


async def test_timed_link_expires_and_frees_its_name(
    trigger_stack: TaiStack, uniq: Callable[[str], str], exec_key: str
) -> None:
    stack = trigger_stack
    admin = stack.api(port=stack.port_a)
    topic = uniq("topic").replace("_", "-")
    rkey = uniq("rec")
    name = uniq("link")
    ttl_seconds = 5

    await register_record_hook(
        admin, topic, name=uniq("hook"), execution_key=exec_key, tool_kwargs={"key": rkey}, expr="{value: .x}"
    )
    link = await mint_link(admin, topic, name=name, ttl_seconds=ttl_seconds, execution_key=exec_key)
    created = time.monotonic()
    public = no_auth(stack)

    # Alive BEFORE the window closes: a fire records (catches a born-dead / units bug).
    alive = await public.request_raw("GET", f"/trigger/{link['token']}?x=alive")
    assert alive.status_code == 200, alive.text
    assert wait_records(stack, rkey, count=1) == ["alive"]

    # Wait past the window on a pure time predicate — nothing touches the token.
    async def past_ttl() -> bool:
        return time.monotonic() >= created + ttl_seconds + 1.0

    await wait_for_async(past_ttl, deadline=25.0, interval=0.25, message="the link TTL window never elapsed")

    # After expiry: the uniform 404, and gone from the list.
    expired = await public.request_raw("GET", f"/trigger/{link['token']}?x=dead")
    assert expired.status_code == 404
    assert expired.json() == {"error": MISS_MESSAGE}
    listing = await admin.get("/api/hooks/trigger-links")
    assert all(item["name"] != name for item in listing["items"]), "an expired link must be gone from the list"

    # The name key expired WITH the record, so re-creating under the SAME name succeeds.
    reused = await mint_link(admin, topic, name=name, ttl_seconds=None, execution_key=exec_key)
    assert reused["name"] == name, "the freed name must be re-creatable"
    assert reused["token"], "a re-created link must carry a token"
