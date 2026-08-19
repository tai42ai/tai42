"""Per-identity isolation A/B negatives across the three audience-bearing seams —
tool runs, interactions, and notifications. For each: identity A creates/receives a
record (sentinel), identity B does not see it (list absence + direct-id 403 where the
matrix says 403), and the unrestricted operator sees everything. Two structural pins go
beyond simple exclusion: the per-identity index/feed stays COMPLETE under a shared-window
flood, and the interactions add-frame carries NO callback ticket (so a filtered caller
can never obtain another identity's ticket through the stream)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import redis as redis_lib

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

from ._owned_support import mint_owned, mint_owner

_ADD_EVENT = "interaction.add"
_ANSWERED_EVENT = "interaction.answered"
_REMOVED_EVENT = "interaction.removed"
_BACKLOG_DONE = "interaction.backlog_done"


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


async def _find_add(stack: TaiStack, port: int, token: str, question: str, *, deadline: float = 8.0) -> dict:
    """Stream as ``token`` until an add-frame carrying ``question`` appears; return the
    frame's data. Re-opens per probe so a not-yet-registered question simply retries."""
    url = _stream_url(stack, port)
    headers = {"Authorization": f"Bearer {token}"}

    async def probe() -> dict | None:
        async with httpx.AsyncClient(timeout=4.0) as client:
            try:
                async with client.stream("GET", url, headers=headers) as response:
                    buffer = ""
                    async for chunk in response.aiter_text():
                        buffer += chunk
                        while "\n\n" in buffer:
                            frame, buffer = buffer.split("\n\n", 1)
                            event, data = _parse_frame(frame)
                            if event == _ADD_EVENT and question in json.dumps(data):
                                return data
                            if event == _BACKLOG_DONE:
                                return None
            except httpx.HTTPError:
                return None
        return None

    found = await wait_for_async(probe, deadline=deadline, message=f"add-frame for {question!r} never appeared")
    assert found is not None
    return found


async def _backlog_adds(stack: TaiStack, port: int, token: str, *, timeout: float = 8.0) -> list[dict]:
    """Read one stream as ``token`` through to ``backlog_done`` and return every
    add-frame's data — the caller's whole visible pending backlog."""
    url = _stream_url(stack, port)
    headers = {"Authorization": f"Bearer {token}"}
    adds: list[dict] = []
    async with httpx.AsyncClient(timeout=timeout) as client, client.stream("GET", url, headers=headers) as response:
        buffer = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                event, data = _parse_frame(frame)
                if event == _ADD_EVENT:
                    adds.append(data)
                elif event == _BACKLOG_DONE:
                    return adds
    return adds


async def _collect_frames(stack: TaiStack, port: int, token: str, sink: list[tuple[str, dict]]) -> None:
    """Stream as ``token``, appending every (event, data) except the backlog marker to
    ``sink`` until cancelled — a long-lived observer for the answered/removed pins."""
    url = _stream_url(stack, port)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=None) as client, client.stream("GET", url, headers=headers) as response:
        buffer = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                event, data = _parse_frame(frame)
                if event is not None and event != _BACKLOG_DONE:
                    sink.append((event, data))


def _resolve_ticket(stack: TaiStack, interaction_id: str) -> str:
    """Recover the callback ticket for ``interaction_id`` from Redis — proof a ticket
    EXISTS for the interaction even though no stream frame ever carries it."""
    host, port = stack.infra.settings.redis_host_port
    client = redis_lib.Redis(host=host, port=port, db=stack.resources.redis_idx, decode_responses=True)
    try:
        for key in client.scan_iter(match="interactions:ticket:*"):
            if client.get(key) == interaction_id:
                return key.rsplit(":", 1)[-1]
    finally:
        client.close()
    raise AssertionError(f"no callback ticket in Redis for interaction {interaction_id}")


# -- identity setup ----------------------------------------------------------


async def _two_identities(stack: TaiStack, uniq: Callable[[str], str]) -> tuple[str, str, str, str]:
    """Provision two restricted owned keys under two distinct owners. Returns each
    key's OWN id (its isolation identity under the key-keyed model — its ``user_id`` /
    ``get_current_user_id()``, NEVER its owner's): ``(owned_a_id, owned_a_raw,
    owned_b_id, owned_b_raw)``."""
    root = stack.api(port=stack.port_a)
    _owner_a_id, owner_a_raw = await mint_owner(root, uniq)
    owned_a_id, owned_a_raw = await mint_owned(root.with_token(owner_a_raw), uniq)
    _owner_b_id, owner_b_raw = await mint_owner(root, uniq)
    owned_b_id, owned_b_raw = await mint_owned(root.with_token(owner_b_raw), uniq)
    return owned_a_id, owned_a_raw, owned_b_id, owned_b_raw


def _ids(entries: list[dict[str, Any]]) -> list[str]:
    return [entry["run_id"] for entry in entries]


# -- tool runs ---------------------------------------------------------------


async def test_tool_run_isolation_and_completeness(auth_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    _owned_a, owned_a_raw, _owned_b, owned_b_raw = await _two_identities(auth_stack, uniq)
    root = auth_stack.api(port=auth_stack.port_a)
    owned_a = root.with_token(owned_a_raw)
    owned_b = root.with_token(owned_b_raw)

    submitted = await owned_a.post(
        "/api/tool-runs", json={"tool_name": "e2e_echo", "arguments": {"payload": "a"}}, expect=202
    )
    run_id = submitted["run_id"]

    # A sees its own run — by id and in its list (the sentinel).
    got = await owned_a.get(f"/api/tool-runs/{run_id}")
    assert got["run_id"] == run_id
    assert run_id in _ids(await owned_a.get("/api/tool-runs?tool_name=e2e_echo"))

    # B cannot: a NAMED run of another identity is a 403 (never a lying 404), and it is
    # absent from B's own list.
    denied = await owned_b.request_raw("GET", f"/api/tool-runs/{run_id}")
    assert denied.status_code == 403, denied.text
    assert run_id not in _ids(await owned_b.get("/api/tool-runs?tool_name=e2e_echo"))

    # The unrestricted operator sees everything.
    assert (await root.get(f"/api/tool-runs/{run_id}"))["run_id"] == run_id
    assert run_id in _ids(await root.get("/api/tool-runs?tool_name=e2e_echo"))

    # Completeness pin: flood the shared window (recent limit is 3 on this stack) with
    # other-identity runs. A's own list STILL carries its run (per-identity index),
    # while the shared window has evicted it (the sentinel the flood actually overflowed).
    for _ in range(5):
        await root.post("/api/tool-runs", json={"tool_name": "e2e_echo", "arguments": {"payload": "flood"}}, expect=202)
    assert run_id in _ids(await owned_a.get("/api/tool-runs?tool_name=e2e_echo"))
    assert run_id not in _ids(await root.get("/api/tool-runs?tool_name=e2e_echo"))


async def test_key_own_not_owner_tool_runs_two_siblings_under_one_owner(
    auth_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """The key-own-vs-owner pin for the TOOL-RUNS seam. TWO owned keys minted under the
    SAME owner share an owner claim but carry DIFFERENT own ids, so under the key-keyed
    model each is its OWN island: a run's ownership follows the starter's OWN id, never
    the shared owner. Sibling-1 owns its run; sibling-2 — same owner — is a foreign
    identity to it (GET-by-id 403, absent from its list). The owner-keyed model would
    FAIL this: a shared owner would let sibling-2 read sibling-1's run."""
    root = auth_stack.api(port=auth_stack.port_a)
    owner_id, owner_raw = await mint_owner(root, uniq)
    owner = root.with_token(owner_raw)
    # Two siblings under the one owner: same owner claim, distinct own ids.
    owned_1_id, owned_1_raw = await mint_owned(owner, uniq)
    owned_2_id, owned_2_raw = await mint_owned(owner, uniq)
    assert owned_1_id != owner_id
    assert owned_2_id != owner_id
    # Distinct own ids are what makes them siblings rather than one identity.
    assert owned_1_id != owned_2_id
    owned_1 = root.with_token(owned_1_raw)
    owned_2 = root.with_token(owned_2_raw)

    # Sibling-1 starts (and so owns) a background run.
    submitted = await owned_1.post(
        "/api/tool-runs", json={"tool_name": "e2e_echo", "arguments": {"payload": "s1"}}, expect=202
    )
    run_id = submitted["run_id"]

    # Sibling-1 sees its own run — by id and in its list (the sentinel).
    got = await owned_1.get(f"/api/tool-runs/{run_id}")
    assert got["run_id"] == run_id
    assert run_id in _ids(await owned_1.get("/api/tool-runs?tool_name=e2e_echo"))

    # The SIBLING under the SAME owner does NOT: a NAMED run of another identity is a
    # loud 403 (never a lying 404), and it is absent from the sibling's own list —
    # isolation follows the own id, so a shared owner does NOT share a run slice.
    denied = await owned_2.request_raw("GET", f"/api/tool-runs/{run_id}")
    assert denied.status_code == 403, denied.text
    assert "belongs to another identity" in denied.text
    assert run_id not in _ids(await owned_2.get("/api/tool-runs?tool_name=e2e_echo"))


# -- interactions ------------------------------------------------------------


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


async def test_interaction_stream_filter_and_answer_matrix(auth_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    _owned_a, owned_a_raw, _owned_b, owned_b_raw = await _two_identities(auth_stack, uniq)
    port = auth_stack.port_a
    question = uniq("question")

    ask_task = asyncio.create_task(_ask(auth_stack, owned_a_raw, question))
    try:
        add = await _find_add(auth_stack, port, owned_a_raw, question)
        interaction_id = add["interaction_id"]

        # B (a different identity) never sees A's addressed interaction in its backlog.
        b_adds = await _backlog_adds(auth_stack, port, owned_b_raw)
        assert all(question not in json.dumps(entry) for entry in b_adds), "B's stream leaked A's addressed question"

        api_b = auth_stack.api(port=port).with_token(owned_b_raw)
        api_a = auth_stack.api(port=port).with_token(owned_a_raw)
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
    auth_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """The key-own-vs-owner pin for the INTERACTIONS seam. TWO owned keys minted under
    the SAME owner share an owner claim but carry DIFFERENT own ids, so under the
    key-keyed model each is its OWN island: an ask's audience clamps to the asker's OWN
    id, and only that id (or the operator) may see/answer it. Sibling-2 — same owner —
    is a foreign identity: it never sees sibling-1's question and is a loud 403 at the
    answer door. The owner-keyed model would FAIL this: a shared owner would let
    sibling-2 read and answer sibling-1's addressed question."""
    root = auth_stack.api(port=auth_stack.port_a)
    owner_id, owner_raw = await mint_owner(root, uniq)
    owner = root.with_token(owner_raw)
    # Two siblings under the one owner: same owner claim, distinct own ids.
    owned_1_id, owned_1_raw = await mint_owned(owner, uniq)
    owned_2_id, owned_2_raw = await mint_owned(owner, uniq)
    assert owned_1_id != owner_id
    assert owned_2_id != owner_id
    # Distinct own ids are what makes them siblings rather than one identity.
    assert owned_1_id != owned_2_id
    port = auth_stack.port_a

    # Write-side foreign-audience denial: sibling-1 addressing an ask to a FOREIGN identity
    # (including the shared owner and the sibling) is a loud cross-identity refusal at the
    # tool door. Only its OWN id is addressable.
    for foreign_audience in (owner_id, owned_2_id):
        async with auth_stack.mcp(port=port, auth=owned_1_raw) as mcp:
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
    ask_task = asyncio.create_task(_ask(auth_stack, owned_1_raw, question, audience=owned_1_id))
    try:
        add = await _find_add(auth_stack, port, owned_1_raw, question)
        interaction_id = add["interaction_id"]
        # The add frame carries the addressed audience — sibling-1's OWN id, the value
        # the ask clamped to — verbatim, so the attribution rides the stream present-value.
        assert add["audience"] == owned_1_id

        # The SIBLING under the SAME owner never sees sibling-1's addressed question in
        # its backlog — isolation follows the own id, so a shared owner does NOT share a
        # stream (the owner-keyed model would leak it here).
        two_adds = await _backlog_adds(auth_stack, port, owned_2_raw)
        assert all(question not in json.dumps(entry) for entry in two_adds), "sibling leaked A's addressed question"

        api_1 = auth_stack.api(port=port).with_token(owned_1_raw)
        api_2 = auth_stack.api(port=port).with_token(owned_2_raw)
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
    auth_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    root = auth_stack.api(port=auth_stack.port_a)
    _owner_id, owner_raw = await mint_owner(root, uniq)
    _owned_id, owned_raw = await mint_owned(root.with_token(owner_raw), uniq)
    port = auth_stack.port_a
    question = uniq("question")

    ask_task = asyncio.create_task(_ask(auth_stack, owned_raw, question))
    try:
        add = await _find_add(auth_stack, port, owned_raw, question)
        # The operator (unrestricted) can always unblock a question addressed to another
        # identity.
        answered = await root.request_raw(
            "POST", f"/api/interactions/{add['interaction_id']}/answer", json={"answer": "op"}
        )
        assert answered.status_code == 200, answered.text
        answer = await asyncio.wait_for(ask_task, timeout=10.0)
        assert "op" in json.dumps(answer)
    finally:
        if not ask_task.done():
            ask_task.cancel()


# The deliver-only channel the auth stack registers (``tai42_e2e_fixtures.stub_channel``).
# A channel-delivered ask hands the callback ticket to the CHANNEL out-of-band; the in-app
# add-frame carries no callback URL — the genuinely ticket-contained mode.
_STUB_CHANNEL = "stub"


async def test_stream_add_frame_omits_ticket_though_one_exists(
    auth_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    root = auth_stack.api(port=auth_stack.port_a)
    _owner_id, owner_raw = await mint_owner(root, uniq)
    _owned_id, owned_raw = await mint_owned(root.with_token(owner_raw), uniq)
    port = auth_stack.port_a
    question = uniq("question")

    async def ask_over_channel() -> object:
        # A channel-delivered ask mints a callback ticket and hands it to the channel
        # out-of-band; the reply bridges back through the public callback door, so the in-app
        # add-frame must never carry the ticket — its silence is a real containment claim.
        async with auth_stack.mcp(port=port, auth=owned_raw) as mcp:
            result = await mcp.call_tool(
                "ask_user",
                {"question": question, "channel": _STUB_CHANNEL},
            )
        return result.data

    ask_task = asyncio.create_task(ask_over_channel())
    try:
        add = await _find_add(auth_stack, port, owned_raw, question)
        interaction_id = add["interaction_id"]

        # The ticket EXISTS in Redis (the channel carries it out-of-band)...
        ticket = _resolve_ticket(auth_stack, interaction_id)
        # ...yet its exact VALUE appears nowhere in the add-frame nor anywhere in the
        # caller's whole visible backlog — so a filtered caller can never lift another
        # identity's callback capability off the stream. The leak channel is an embedded
        # callback URL, caught only by this value check.
        assert ticket not in json.dumps(add), "add-frame leaked the callback ticket"
        for entry in await _backlog_adds(auth_stack, port, owned_raw):
            assert ticket not in json.dumps(entry), "a backlog add-frame leaked the callback ticket"

        # Unblock through the public callback door so the waiter completes cleanly.
        callback = await root.request_raw("POST", f"/api/interactions/callback/{ticket}", json={"answer": "done"})
        assert callback.status_code == 200, callback.text
        await asyncio.wait_for(ask_task, timeout=10.0)
    finally:
        if not ask_task.done():
            ask_task.cancel()


async def test_answered_and_removed_frames_do_not_leak_cross_identity(
    auth_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    _owned_a, owned_a_raw, _owned_b, owned_b_raw = await _two_identities(auth_stack, uniq)
    port = auth_stack.port_a
    frames_a: list[tuple[str, dict]] = []
    frames_b: list[tuple[str, dict]] = []
    collector_a = asyncio.create_task(_collect_frames(auth_stack, port, owned_a_raw, frames_a))
    collector_b = asyncio.create_task(_collect_frames(auth_stack, port, owned_b_raw, frames_b))
    api_a = auth_stack.api(port=port).with_token(owned_a_raw)
    api_b = auth_stack.api(port=port).with_token(owned_b_raw)

    def _has(sink: list[tuple[str, dict]], event: str, needle: str) -> Callable[[], Awaitable[bool]]:
        async def check() -> bool:
            return any(evt == event and needle in json.dumps(data) for evt, data in sink)

        return check

    def _id_for(sink: list[tuple[str, dict]], question: str) -> str:
        for evt, data in sink:
            if evt == _ADD_EVENT and question in json.dumps(data):
                return data["interaction_id"]
        raise AssertionError(f"no add-frame for {question!r}")

    answered_q = uniq("answered")
    removed_q = uniq("removed")
    # B drives its OWN addressed pair as a liveness sentinel: asserting B receives its own
    # frames proves B's collector connected and is delivering, so B's silence on A's terminal
    # frames is genuine isolation, not a dead stream.
    b_answered_q = uniq("banswered")
    b_removed_q = uniq("bremoved")
    answered_task = asyncio.create_task(_ask(auth_stack, owned_a_raw, answered_q))
    b_answered_task = asyncio.create_task(_ask(auth_stack, owned_b_raw, b_answered_q))
    # A short SUT-side timeout drives a deterministic prune → removed frame (no cancel
    # race); the ask raises the timeout, swallowed via return_exceptions below.
    removed_task = asyncio.create_task(_ask(auth_stack, owned_a_raw, removed_q, timeout=3.0))
    b_removed_task = asyncio.create_task(_ask(auth_stack, owned_b_raw, b_removed_q, timeout=3.0))
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


# -- notifications -----------------------------------------------------------


async def test_notification_audience_isolation_and_completeness(
    auth_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    owned_a_id, owned_a_raw, owned_b_id, owned_b_raw = await _two_identities(auth_stack, uniq)
    root = auth_stack.api(port=auth_stack.port_a)
    owned_a = root.with_token(owned_a_raw)
    owned_b = root.with_token(owned_b_raw)

    msg_a = uniq("na")
    msg_b = uniq("nb")
    msg_broadcast = uniq("bcast")
    # The operator addresses each record by AUDIENCE (the identity's OWN id), NOT its owner.
    # msg_a is addressed to A by audience but its recipient (a delivery ADDRESS) is set to
    # B's identity string, and msg_recip_a is a BROADCAST whose recipient is A's identity
    # string. Crossing the two axes makes the isolation below pass ONLY if it keys on
    # audience (a who), never on recipient (a where).
    await root.post("/api/notifications", json={"message": msg_a, "audience": owned_a_id, "recipient": owned_b_id})
    await root.post("/api/notifications", json={"message": msg_b, "audience": owned_b_id})
    await root.post("/api/notifications", json={"message": msg_broadcast})
    msg_recip_a = uniq("recipa")
    await root.post("/api/notifications", json={"message": msg_recip_a, "recipient": owned_a_id})

    a_feed = (await owned_a.get("/api/notifications"))["notifications"]
    a_messages = [record["message"] for record in a_feed]
    assert msg_a in a_messages
    assert msg_b not in a_messages
    # A broadcast (no audience) is hidden from a restricted identity (default-deny)...
    assert msg_broadcast not in a_messages
    # ...and STILL hidden when its recipient names A's own identity: recipient does not
    # route the in-app feed, so recipient==A alone never pulls a record into A's inbox.
    assert msg_recip_a not in a_messages
    # recipient (a where) is independent of audience (a who): A sees msg_a by audience
    # even though its stored recipient names B, and the recipient is stored untouched.
    record_a = next(record for record in a_feed if record["message"] == msg_a)
    assert record_a["audience"] == owned_a_id
    assert record_a["recipient"] == owned_b_id

    b_messages = [record["message"] for record in (await owned_b.get("/api/notifications"))["notifications"]]
    assert msg_b in b_messages
    # B never sees msg_a even though its recipient names B's identity — the in-app
    # isolation keys on audience (=A), never on recipient.
    assert msg_a not in b_messages
    assert msg_broadcast not in b_messages
    assert msg_recip_a not in b_messages

    # The unrestricted operator sees every record, addressed or broadcast.
    root_messages = [record["message"] for record in (await root.get("/api/notifications"))["notifications"]]
    assert {msg_a, msg_b, msg_broadcast, msg_recip_a} <= set(root_messages)

    # Completeness pin: A's addressed record survives a broadcast flood that overflows
    # the shared feed (feed max is 5 on this stack), proving A reads its OWN per-identity
    # feed — the shared feed has evicted the record (the sentinel).
    msg_a2 = uniq("na2")
    await root.post("/api/notifications", json={"message": msg_a2, "audience": owned_a_id})
    for _ in range(6):
        await root.post("/api/notifications", json={"message": uniq("flood")})

    a_after = [record["message"] for record in (await owned_a.get("/api/notifications"))["notifications"]]
    assert msg_a2 in a_after
    root_after = [record["message"] for record in (await root.get("/api/notifications"))["notifications"]]
    assert msg_a2 not in root_after


async def test_key_own_not_owner_two_siblings_under_one_owner(auth_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    """The key-own-vs-owner pin. TWO owned keys minted under the SAME owner have the
    same owner claim but DIFFERENT own ids, so under the key-keyed model each is its
    OWN island: isolation follows the OWN id, never the shared owner. A restricted
    caller's ``notify_user`` with no audience lands on ITS OWN id (proven by the stored
    ``audience`` and by the sibling — same owner — never seeing it), and addressing ANY
    foreign identity — INCLUDING the shared owner — is a loud 403."""
    root = auth_stack.api(port=auth_stack.port_a)
    owner_id, owner_raw = await mint_owner(root, uniq)
    owner = root.with_token(owner_raw)
    # Two siblings under the one owner: same owner claim, distinct own ids.
    owned_1_id, owned_1_raw = await mint_owned(owner, uniq)
    owned_2_id, owned_2_raw = await mint_owned(owner, uniq)
    assert owned_1_id != owner_id
    assert owned_2_id != owner_id
    owned_1 = root.with_token(owned_1_raw)
    owned_2 = root.with_token(owned_2_raw)

    # A restricted caller's notify with NO audience is clamped to its OWN id (not the
    # owner, not escalated to operators): the record lands on its own feed and stores
    # its own id as the audience.
    msg_self = uniq("self")
    await owned_1.post("/api/notifications", json={"message": msg_self})
    one_feed = (await owned_1.get("/api/notifications"))["notifications"]
    assert msg_self in [record["message"] for record in one_feed]
    record_self = next(record for record in one_feed if record["message"] == msg_self)
    assert record_self["audience"] == owned_1_id, "audience=None must clamp to the key's OWN id, not the owner"

    # The SIBLING under the SAME owner never sees it — isolation follows the own id, so
    # a shared owner does NOT share a feed (the owner-keyed model would leak it here).
    assert msg_self not in [record["message"] for record in (await owned_2.get("/api/notifications"))["notifications"]]

    # Addressing the OWNER as an explicit audience is a foreign-identity 403 (the key is
    # NOT its owner) — the loud cross-identity write denial, not a silent redirect.
    denied_owner = await owned_1.request_raw(
        "POST", "/api/notifications", json={"message": uniq("atowner"), "audience": owner_id}
    )
    assert denied_owner.status_code == 403, denied_owner.text
    # Addressing a SIBLING (also foreign) is a 403 too.
    denied_sibling = await owned_1.request_raw(
        "POST", "/api/notifications", json={"message": uniq("atsibling"), "audience": owned_2_id}
    )
    assert denied_sibling.status_code == 403, denied_sibling.text
    # Addressing its OWN id explicitly passes.
    msg_own_explicit = uniq("ownexplicit")
    allowed = await owned_1.request_raw(
        "POST", "/api/notifications", json={"message": msg_own_explicit, "audience": owned_1_id}
    )
    assert allowed.status_code == 200, allowed.text
    assert msg_own_explicit in [
        record["message"] for record in (await owned_1.get("/api/notifications"))["notifications"]
    ]
