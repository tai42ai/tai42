"""Per-identity isolation A/B negatives on the interactions seam. For an addressed
``ask_user``: identity A sees it (pending list + stream), identity B does not (list
absence + a 403 at the answer door), and the unrestricted operator can always answer.
Two structural pins go beyond simple exclusion: no interactions read surface (the paged
list or the stream) carries a callback ticket (so a filtered caller can never obtain
another identity's ticket), and the terminal answered/removed frames never leak
cross-identity. A further pin proves isolation follows the key's OWN id, not its owner:
two sibling keys under ONE service owner are foreign identities to each other."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import redis as redis_lib

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

from ._owned_support import create_service_owner, mint_key_for, provision_operator, two_service_identities

_ADD_EVENT = "interaction.add"
_ANSWERED_EVENT = "interaction.answered"
_REMOVED_EVENT = "interaction.removed"

# The deliver-only channel the owned-keys stack registers (``tai42_e2e_fixtures.stub_channel``).
# A channel-delivered ask hands the callback ticket to the CHANNEL out-of-band; the in-app
# add-frame carries no callback URL — the genuinely ticket-contained mode.
_STUB_CHANNEL = "stub"


# -- SSE stream helpers ------------------------------------------------------


def _parse_frame(frame: str) -> tuple[str | None, dict]:
    """Split one SSE frame into its ``event`` name and parsed ``data`` object."""
    event: str | None = None
    data: dict = {}
    for line in frame.splitlines():
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            try:
                data = json.loads(line[len("data:") :].strip())
            except json.JSONDecodeError:
                data = {}
    return event, data


def _stream_url(stack: TaiStack, port: int) -> str:
    return f"http://{stack.host}:{port}/api/interactions/stream"


async def _visible_pending(stack: TaiStack, port: int, token: str, *, timeout: float = 8.0) -> list[dict]:
    """Read the paged pending-list door as ``token`` and return every item — the
    caller's whole visible pending set (the door is audience-filtered to the caller,
    so a restricted caller sees ONLY its own addressed questions)."""
    url = f"http://{stack.host}:{port}/api/interactions"
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, params={"page": 1, "pageSize": 200}, headers=headers)
        resp.raise_for_status()
        return list(resp.json()["data"]["items"])


async def _find_add(stack: TaiStack, port: int, token: str, question: str, *, deadline: float = 8.0) -> dict:
    """Poll the paged list door as ``token`` until an item carrying ``question`` appears
    (the door is audience-filtered to the caller); return the item."""

    async def probe() -> dict | None:
        try:
            items = await _visible_pending(stack, port, token, timeout=4.0)
        except httpx.HTTPError:
            return None
        for item in items:
            if question in json.dumps(item):
                return item
        return None

    found = await wait_for_async(probe, deadline=deadline, message=f"list item for {question!r} never appeared")
    assert found is not None
    return found


async def _collect_frames(
    stack: TaiStack, port: int, token: str, sink: list[tuple[str, dict]], ready: asyncio.Event | None = None
) -> None:
    """Stream as ``token``, appending every (event, data) to ``sink`` until cancelled —
    a long-lived observer of the tail-only add/answered/removed events. ``ready`` is set
    once the response headers are in: entering ``client.stream`` has received them, so the
    server has captured the tail cursor and every later add/answered/removed is guaranteed
    — the caller waits on it before publishing so no frame races the connect."""
    url = _stream_url(stack, port)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=None) as client, client.stream("GET", url, headers=headers) as response:
        if ready is not None:
            ready.set()
        buffer = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                event, data = _parse_frame(frame)
                if event is not None:
                    sink.append((event, data))


def _resolve_ticket(stack: TaiStack, interaction_id: str) -> str:
    """Recover the callback ticket for ``interaction_id`` from Redis — proof a ticket
    EXISTS for the interaction even though no stream frame ever carries it."""
    host, port = stack.infra.settings.redis_host_port
    client = redis_lib.Redis(host=host, port=port, db=stack.resources.redis_idx, decode_responses=True)
    try:
        for key in client.scan_iter(match=f"{stack.resources.bus_namespace}:interactions:ticket:*"):
            if client.get(key) == interaction_id:
                return key.rsplit(":", 1)[-1]
    finally:
        client.close()
    raise AssertionError(f"no callback ticket in Redis for interaction {interaction_id}")


async def _ask(
    stack: TaiStack, token: str, question: str, *, timeout: float | None = None, audience: str | None = None
) -> object:
    kwargs: dict[str, Any] = {"question": question}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if audience is not None:
        kwargs["audience"] = audience
    async with stack.mcp(port=stack.port_a, auth=token) as mcp:
        result = await mcp.call_tool("ask_user", kwargs)
    return result.data


# -- interactions ------------------------------------------------------------


async def test_interaction_stream_filter_and_answer_matrix(
    owned_keys_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    _owned_a, owned_a_raw, _owned_b, owned_b_raw = await two_service_identities(owned_keys_stack, uniq)
    port = owned_keys_stack.port_a
    question = uniq("question")

    ask_task = asyncio.create_task(_ask(owned_keys_stack, owned_a_raw, question))
    try:
        add = await _find_add(owned_keys_stack, port, owned_a_raw, question)
        interaction_id = add["interaction_id"]

        # B (a different identity) never sees A's addressed interaction in its pending list.
        b_adds = await _visible_pending(owned_keys_stack, port, owned_b_raw)
        assert all(question not in json.dumps(entry) for entry in b_adds), "B's stream leaked A's addressed question"

        api_b = owned_keys_stack.api(port=port).with_token(owned_b_raw)
        api_a = owned_keys_stack.api(port=port).with_token(owned_a_raw)
        # Answer-door matrix: another restricted identity is denied 403; the addressed
        # identity answers and unblocks the waiter.
        denied = await api_b.request_raw("POST", f"/api/interactions/{interaction_id}/answer", json={"answer": "no"})
        assert denied.status_code == 403, denied.text
        allowed = await api_a.request_raw(
            "POST", f"/api/interactions/{interaction_id}/answer", json={"answer": "yes-a"}
        )
        assert allowed.status_code == 200, allowed.text

        answer = await asyncio.wait_for(ask_task, timeout=10.0)
        assert "yes-a" in json.dumps(answer)
    finally:
        if not ask_task.done():
            ask_task.cancel()


async def test_key_own_not_owner_interactions_two_siblings_under_one_owner(
    owned_keys_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """The key-own-vs-owner pin for the INTERACTIONS seam. TWO keys minted under the SAME
    service owner share an owner claim but carry DIFFERENT own ids, so under the key-keyed
    model each is its OWN island: an ask's audience clamps to the asker's OWN id, and only
    that id (or the operator) may see/answer it. Sibling-2 — same owner — is a foreign
    identity: it never sees sibling-1's question and is a loud 403 at the answer door. The
    owner-keyed model would FAIL this: a shared owner would let sibling-2 read and answer
    sibling-1's addressed question."""
    root = owned_keys_stack.api(port=owned_keys_stack.port_a)
    owner_id = await create_service_owner(root, uniq)
    # Two siblings under the one owner: same owner claim, distinct own ids.
    owned_1_id, owned_1_raw = await mint_key_for(root, uniq, owner_id)
    owned_2_id, owned_2_raw = await mint_key_for(root, uniq, owner_id)
    assert owned_1_id != owner_id
    assert owned_2_id != owner_id
    # Distinct own ids are what makes them siblings rather than one identity.
    assert owned_1_id != owned_2_id
    port = owned_keys_stack.port_a

    # Write-side foreign-audience denial: sibling-1 addressing an ask to a FOREIGN identity
    # (including the shared owner and the sibling) is a loud cross-identity refusal at the
    # tool door. Only its OWN id is addressable.
    for foreign_audience in (owner_id, owned_2_id):
        async with owned_keys_stack.mcp(port=port, auth=owned_1_raw) as mcp:
            refused = await mcp.call_tool(
                "ask_user",
                {"question": uniq("foreign"), "audience": foreign_audience},
                raise_on_error=False,
            )
        assert refused.is_error, "a restricted caller must not address a foreign audience"
        refused_text = json.dumps([block.model_dump(mode="json") for block in (refused.content or [])])
        assert "own identity" in refused_text, refused_text

    question = uniq("question")
    # Positive acceptance: sibling-1 addresses this ask to its OWN id EXPLICITLY. The whole
    # found-in-own-stream + answered-by-own-id flow below runs on it, proving an explicit
    # own-id audience is accepted end-to-end.
    ask_task = asyncio.create_task(_ask(owned_keys_stack, owned_1_raw, question, audience=owned_1_id))
    try:
        add = await _find_add(owned_keys_stack, port, owned_1_raw, question)
        interaction_id = add["interaction_id"]
        # The add frame carries the addressed audience — sibling-1's OWN id, the value
        # the ask clamped to — verbatim, so the attribution rides the stream present-value.
        assert add["audience"] == owned_1_id

        # The SIBLING under the SAME owner never sees sibling-1's addressed question in
        # its pending list — isolation follows the own id, so a shared owner does NOT share a
        # stream (the owner-keyed model would leak it here).
        two_adds = await _visible_pending(owned_keys_stack, port, owned_2_raw)
        assert all(question not in json.dumps(entry) for entry in two_adds), "sibling leaked A's addressed question"

        api_1 = owned_keys_stack.api(port=port).with_token(owned_1_raw)
        api_2 = owned_keys_stack.api(port=port).with_token(owned_2_raw)
        # Answer-door matrix: the sibling (foreign identity, same owner) is denied 403;
        # sibling-1 — the addressed own id — answers and unblocks the waiter.
        denied = await api_2.request_raw("POST", f"/api/interactions/{interaction_id}/answer", json={"answer": "no"})
        assert denied.status_code == 403, denied.text
        allowed = await api_1.request_raw(
            "POST", f"/api/interactions/{interaction_id}/answer", json={"answer": "yes-1"}
        )
        assert allowed.status_code == 200, allowed.text

        answer = await asyncio.wait_for(ask_task, timeout=10.0)
        assert "yes-1" in json.dumps(answer)
    finally:
        if not ask_task.done():
            ask_task.cancel()


async def test_unrestricted_operator_can_answer_addressed_interaction(
    owned_keys_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    root = owned_keys_stack.api(port=owned_keys_stack.port_a)
    svc_id = await create_service_owner(root, uniq)
    _owned_id, owned_raw = await mint_key_for(root, uniq, svc_id)
    # The unrestricted operator: an admin session is a top-level principal with no owner
    # claim, so the isolation gates give it the full cross-identity view. The seeded admin
    # root key is unrestricted too — admin is never confined to a slice, owned or not.
    operator = root.with_token(await provision_operator(owned_keys_stack, root, uniq))
    port = owned_keys_stack.port_a
    question = uniq("question")

    ask_task = asyncio.create_task(_ask(owned_keys_stack, owned_raw, question))
    try:
        add = await _find_add(owned_keys_stack, port, owned_raw, question)
        # The operator (unrestricted) can always unblock a question addressed to another
        # identity.
        answered = await operator.request_raw(
            "POST", f"/api/interactions/{add['interaction_id']}/answer", json={"answer": "op"}
        )
        assert answered.status_code == 200, answered.text
        answer = await asyncio.wait_for(ask_task, timeout=10.0)
        assert "op" in json.dumps(answer)
    finally:
        if not ask_task.done():
            ask_task.cancel()


async def test_stream_add_frame_omits_ticket_though_one_exists(
    owned_keys_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    root = owned_keys_stack.api(port=owned_keys_stack.port_a)
    svc_id = await create_service_owner(root, uniq)
    _owned_id, owned_raw = await mint_key_for(root, uniq, svc_id)
    port = owned_keys_stack.port_a
    question = uniq("question")

    async def ask_over_channel() -> object:
        # A channel-delivered ask mints a callback ticket and hands it to the channel
        # out-of-band; the reply bridges back through the public callback door, so the in-app
        # add-frame must never carry the ticket — its silence is a real containment claim.
        async with owned_keys_stack.mcp(port=port, auth=owned_raw) as mcp:
            result = await mcp.call_tool(
                "ask_user",
                {"question": question, "channel": _STUB_CHANNEL},
            )
        return result.data

    ask_task = asyncio.create_task(ask_over_channel())
    try:
        add = await _find_add(owned_keys_stack, port, owned_raw, question)
        interaction_id = add["interaction_id"]

        # The ticket EXISTS in Redis (the channel carries it out-of-band)...
        ticket = _resolve_ticket(owned_keys_stack, interaction_id)
        # ...yet its exact VALUE appears nowhere in the add-frame nor anywhere in the
        # caller's whole visible pending list — so a filtered caller can never lift another
        # identity's callback capability off the stream. The leak channel is an embedded
        # callback URL, caught only by this value check.
        assert ticket not in json.dumps(add), "add-frame leaked the callback ticket"
        for entry in await _visible_pending(owned_keys_stack, port, owned_raw):
            assert ticket not in json.dumps(entry), "a pending-list item leaked the callback ticket"

        # Unblock through the public callback door so the waiter completes cleanly.
        callback = await root.request_raw("POST", f"/api/interactions/callback/{ticket}", json={"answer": "done"})
        assert callback.status_code == 200, callback.text
        await asyncio.wait_for(ask_task, timeout=10.0)
    finally:
        if not ask_task.done():
            ask_task.cancel()


def _has(sink: list[tuple[str, dict]], event: str, needle: str) -> Callable[[], Awaitable[bool]]:
    """A poll predicate: whether ``sink`` holds a frame of ``event`` whose data carries ``needle``."""

    async def check() -> bool:
        return any(evt == event and needle in json.dumps(data) for evt, data in sink)

    return check


def _id_for(sink: list[tuple[str, dict]], question: str) -> str:
    """The interaction id of the add-frame carrying ``question`` in ``sink``."""
    for evt, data in sink:
        if evt == _ADD_EVENT and question in json.dumps(data):
            return data["interaction_id"]
    raise AssertionError(f"no add-frame for {question!r}")


async def _assert_terminal_frames_isolated(
    frames_a: list[tuple[str, dict]],
    frames_b: list[tuple[str, dict]],
    *,
    answered_id: str,
    removed_id: str,
    b_answered_id: str,
    b_removed_id: str,
) -> None:
    """A receives BOTH its own terminal frames; B receives its OWN (the liveness sentinel) and
    NONE of A's — the cross-identity isolation of the answered/removed tail."""
    # A's own stream receives BOTH terminal frames.
    await wait_for_async(
        _has(frames_a, _ANSWERED_EVENT, answered_id), deadline=8.0, message="A never saw its own answered frame"
    )
    await wait_for_async(
        _has(frames_a, _REMOVED_EVENT, removed_id), deadline=8.0, message="A never saw its own removed frame"
    )

    # Liveness sentinel: B's stream receives ITS OWN terminal frames — so the
    # collector is provably live and delivering answered/removed events.
    await wait_for_async(
        _has(frames_b, _ANSWERED_EVENT, b_answered_id),
        deadline=8.0,
        message="B never saw its own answered frame (collector not live)",
    )
    await wait_for_async(
        _has(frames_b, _REMOVED_EVENT, b_removed_id),
        deadline=8.0,
        message="B never saw its own removed frame (collector not live)",
    )

    # ...yet B never saw ANY frame — add, answered, or removed — for A's interactions.
    leaked = [(evt, data) for evt, data in frames_b if data.get("interaction_id") in (answered_id, removed_id)]
    assert leaked == [], f"B leaked cross-identity frames: {leaked}"


async def test_answered_and_removed_frames_do_not_leak_cross_identity(
    owned_keys_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    _owned_a, owned_a_raw, _owned_b, owned_b_raw = await two_service_identities(owned_keys_stack, uniq)
    port = owned_keys_stack.port_a
    frames_a: list[tuple[str, dict]] = []
    frames_b: list[tuple[str, dict]] = []
    ready_a = asyncio.Event()
    ready_b = asyncio.Event()
    collector_a = asyncio.create_task(_collect_frames(owned_keys_stack, port, owned_a_raw, frames_a, ready_a))
    collector_b = asyncio.create_task(_collect_frames(owned_keys_stack, port, owned_b_raw, frames_b, ready_b))
    api_a = owned_keys_stack.api(port=port).with_token(owned_a_raw)
    api_b = owned_keys_stack.api(port=port).with_token(owned_b_raw)

    answered_q = uniq("answered")
    removed_q = uniq("removed")
    # B drives its OWN addressed pair as a liveness sentinel: asserting B receives its own
    # frames proves B's collector connected and is delivering, so B's silence on A's terminal
    # frames is genuine isolation, not a dead stream.
    b_answered_q = uniq("banswered")
    b_removed_q = uniq("bremoved")
    # Both observers must have their response headers (cursor captured) before any ask
    # publishes, so no add/answered/removed frame races the connect.
    await asyncio.wait_for(asyncio.gather(ready_a.wait(), ready_b.wait()), timeout=8.0)
    answered_task = asyncio.create_task(_ask(owned_keys_stack, owned_a_raw, answered_q))
    b_answered_task = asyncio.create_task(_ask(owned_keys_stack, owned_b_raw, b_answered_q))
    # A short SUT-side timeout drives a deterministic prune → removed frame (no cancel
    # race); the ask raises the timeout, swallowed via return_exceptions below.
    removed_task = asyncio.create_task(_ask(owned_keys_stack, owned_a_raw, removed_q, timeout=3.0))
    b_removed_task = asyncio.create_task(_ask(owned_keys_stack, owned_b_raw, b_removed_q, timeout=3.0))
    try:
        await wait_for_async(
            _has(frames_a, _ADD_EVENT, answered_q), deadline=8.0, message="A never saw its answered-q add"
        )
        await wait_for_async(
            _has(frames_a, _ADD_EVENT, removed_q), deadline=8.0, message="A never saw its removed-q add"
        )
        await wait_for_async(
            _has(frames_b, _ADD_EVENT, b_answered_q), deadline=8.0, message="B never saw its own answered-q add"
        )
        await wait_for_async(
            _has(frames_b, _ADD_EVENT, b_removed_q), deadline=8.0, message="B never saw its own removed-q add"
        )
        answered_id = _id_for(frames_a, answered_q)
        removed_id = _id_for(frames_a, removed_q)
        b_answered_id = _id_for(frames_b, b_answered_q)
        b_removed_id = _id_for(frames_b, b_removed_q)

        # Answer one addressed interaction per identity; let the others time out and prune.
        ok = await api_a.request_raw("POST", f"/api/interactions/{answered_id}/answer", json={"answer": "x"})
        assert ok.status_code == 200, ok.text
        ok_b = await api_b.request_raw("POST", f"/api/interactions/{b_answered_id}/answer", json={"answer": "xb"})
        assert ok_b.status_code == 200, ok_b.text
        await asyncio.gather(answered_task, removed_task, b_answered_task, b_removed_task, return_exceptions=True)

        await _assert_terminal_frames_isolated(
            frames_a,
            frames_b,
            answered_id=answered_id,
            removed_id=removed_id,
            b_answered_id=b_answered_id,
            b_removed_id=b_removed_id,
        )
    finally:
        for task in (answered_task, removed_task, b_answered_task, b_removed_task, collector_a, collector_b):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            answered_task,
            removed_task,
            b_answered_task,
            b_removed_task,
            collector_a,
            collector_b,
            return_exceptions=True,
        )
