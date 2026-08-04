"""The Stripe payment flow end to end on the payments stack.

Two tests share one module-scoped payments stack (access control genuinely ON, the stripe
verifier + the three stripe tools + the ask_external extension). A money-pinned PRESET over
``create_stripe_checkout_ask_external`` opens the external ask; the payer "pays" at the
in-process FakeStripe stub; the answer returns to the blocked ask two ways:

* Test A — the webhook loop: a locally-signed ``checkout.session.completed`` reaches the
  topic, the hook fires the bridge, the ask wakes. Plus idempotency (exactly one terminal
  SSE frame), the preset's injection resistance, the forged-cheap-session rejection (the
  executable proof the const pin rejects), and the verifier-bound GET-404 in both states.
* Test B — reconciliation: no webhook is delivered; a direct authed
  ``reconcile_stripe_payments`` run recovers the paid session, idempotently, with real
  cursor paging.

No run-time call reaches a real Stripe host: the tools' ``STRIPE_API_BASE`` points at the
stub and every delivery is HMAC-signed here with the topic's own secret.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from tai42_e2e.netfixtures import FakeStripe
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

# Every delivery here is HMAC-signed locally and answered by the in-process FakeStripe stub
# (session mint, complete_payment, list-cursor introspection), so this is the stripe MOCK leg.
# A real stripe selection points the tools at the live Stripe host and the webhook arrives from
# Stripe's servers; that real leg is exercised on the dedicated e2e creds host (PLAN_2 §F), not
# in CI, so the stub-bound module steps aside for it. Inert in the default mock run —
# is_real("stripe") is False, so collection is byte-for-byte today's.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("stripe"),
    reason="locally-signed FakeStripe flow is the stripe mock leg; the real leg runs on the creds host (PLAN_2 §F)",
)


async def _until(fn: Callable[[], Any], *, deadline: float, interval: float = 0.1, message: str) -> Any:
    """Poll a SYNC condition on the event loop — the blocking ``wait_for`` would starve the
    concurrent ask/stream tasks these tests hold open, so every wait goes through the async
    twin. The condition itself reads in-process state (the stub, the SSE frame list, a log
    file); it just cannot block the loop while the SUT-facing tasks make progress."""

    async def pred() -> Any:
        return fn()

    return await wait_for_async(pred, deadline=deadline, interval=interval, message=message)


# The pinned charge, written ONCE and reused for both the baked ``amount`` and the answer
# schema's ``amount_total`` const — the single-source rule the preset encodes.
_AMOUNT = 50000
_CURRENCY = "usd"

# The canonical expr/condition the payments hook is registered with. The expr projects the
# Stripe event into the bridge's ``event`` param; the condition gates the hook on the
# completed event type. Keep this copy byte-identical to the canonical projection the bridge
# consumes, so the event the reconciler and the webhook path deliver has the same shape.
_CANONICAL_EXPR = (
    "{event: {type: .type, id: .id, session: (.data.object | "
    "{id, payment_intent, payment_status, amount_total, currency, livemode, "
    "customer_email: (.customer_details.email? // null), metadata})}}"
)
_CANONICAL_CONDITION = '.type == "checkout.session.completed"'

# The whitelisted answer key set both the bridge and the reconciler build — ``metadata`` is
# deliberately absent so the agent never receives its own callback ticket back.
_ANSWER_KEYS = {"status", "session_id", "payment_intent", "amount_total", "currency", "customer_email"}


def _preset_body(name: str) -> dict[str, Any]:
    """The canonical money-pinning preset body: six baked keys, ``description`` present, NO
    ``extensions`` key, the schema's consts written from the SAME two literals as the
    charge (lowercase currency)."""
    return {
        "name": name,
        "base_tool": "create_stripe_checkout_ask_external",
        "description": "Ask the customer to pay for a Pro licence.",
        "fixed_kwargs": {
            "amount": _AMOUNT,
            "currency": _CURRENCY,
            "product_name": "Pro licence",
            "success_url": "https://acme.example/thanks",
            "cancel_url": "https://acme.example/cancelled",
            "answer_schema": {
                "type": "object",
                "required": ["amount_total", "currency"],
                "properties": {"amount_total": {"const": _AMOUNT}, "currency": {"const": _CURRENCY}},
            },
        },
    }


@pytest.fixture(autouse=True)
def _reset_stripe(fake_stripe: FakeStripe) -> None:
    """The two tests share one stack; reset the stub before each so the second never
    inherits the first's sessions."""
    fake_stripe.reset()


def _sign(secret: bytes, body: bytes, *, timestamp: int | None = None) -> dict[str, str]:
    """A Stripe-Signature header over ``"<ts>.<raw body>"``, as the verifier recomputes it."""
    ts = timestamp if timestamp is not None else int(time.time())
    digest = hmac.new(secret, f"{ts}".encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    return {"Stripe-Signature": f"t={ts},v1={digest}"}


def _completed_event(session: dict[str, Any]) -> dict[str, Any]:
    """A Stripe ``checkout.session.completed`` envelope carrying ``session`` on
    ``.data.object`` — the ENVELOPE shape the canonical expr projects (a flat payload
    would null ``.data.object`` and fail inside jq)."""
    return {"type": "checkout.session.completed", "id": f"evt_{session['id']}", "data": {"object": session}}


async def _deliver(stack: TaiStack, topic: str, secret: bytes, event: dict[str, Any]) -> httpx.Response:
    """Sign and POST an event to the topic ingress on port_a (the delivery, the bridge fire
    and the rejection all pin to one replica). No platform bearer: Stripe carries none and
    the door is public."""
    body = json.dumps(event).encode()
    # Content-Type application/json is load-bearing: the ingress parses the event only under
    # it (as Stripe sends), and the hook condition reads ``.type`` off the parsed dict. The
    # signature is over the raw bytes, unaffected by the header.
    headers = {**_sign(secret, body), "Content-Type": "application/json"}
    url = f"http://{stack.host}:{stack.port_a}/universal_webhook/{topic}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.post(url, content=body, headers=headers)


async def _setup_flow(stack: TaiStack, api: Any, uniq: Callable[[str], str]) -> tuple[str, str]:
    """Bind the stripe verifier FIRST, mint an owned execution key and register the bridge
    hook, then bake the money-pinning preset. Returns ``(topic, preset_name)``."""
    topic = uniq("payments").replace("_", "-")
    # The hook's execution key must be a MINTED key's user_id (every mint stamps the
    # fingerprint the bind resolves); the seeded root has no such anchor. The admin root
    # binds any existing key.
    exec_user_id = uniq("exec")
    await api.post(
        "/api/auth/api-keys",
        json={"user_id": exec_user_id, "description": "e2e stripe hook key", "scopes": ["e2e-all"]},
    )
    # Bind BEFORE the hook: an unbound topic is an open unauthenticated door into the bridge.
    await api.put(
        f"/api/hooks/topics/{topic}/verifier",
        json={"verifier": "stripe", "config": {"secret_env": "E2E_STRIPE_WEBHOOK_SECRET"}},
    )
    await api.post(
        "/api/hooks",
        json={
            "name": uniq("hook"),
            "topic": topic,
            "tool": "confirm_stripe_payment",
            "execution_key": exec_user_id,
            "condition": _CANONICAL_CONDITION,
            "expr": _CANONICAL_EXPR,
        },
    )
    preset_name = uniq("buypro")
    await api.post("/api/presets", json=_preset_body(preset_name), retry_on_reloading=True)
    return topic, preset_name


def _open_ask(stack: TaiStack, root_token: str, preset_name: str, question: str) -> asyncio.Task[Any]:
    """Launch the composed PRESET ask as a task — the call blocks on port_a until the ask is
    answered, so it is awaited off the task afterwards."""

    async def run() -> Any:
        async with stack.mcp(port=stack.port_a, auth=root_token) as mcp:
            result = await mcp.call_tool(preset_name, {"question": question}, retry_on_reloading=True)
        return result.data if result.data is not None else result.structured_content

    return asyncio.create_task(run())


def _new_session_id(fake_stripe: FakeStripe, before: set[str]) -> str | None:
    return next((sid for sid in fake_stripe.session_ids() if sid not in before), None)


def _as_answer(value: Any) -> dict[str, Any]:
    """The woken value as a dict (the composed tool may hand back the answer as a dict or a
    JSON string)."""
    if isinstance(value, str):
        value = json.loads(value)
    assert isinstance(value, dict), f"unexpected answer shape: {value!r}"
    return value


async def _get_callback(url: str) -> httpx.Response:
    """A browser-style GET on the callback URL (no bearer) — server-to-server only, so a
    verifier-bound question 404s it in every state."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.get(url)


# ---- SSE frame helpers (item 3: exactly-one terminal event) --------------


def _parse_frame(frame: str) -> tuple[str | None, dict[str, Any] | None]:
    event: str | None = None
    data: dict[str, Any] | None = None
    for line in frame.splitlines():
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            try:
                parsed = json.loads(line[len("data:") :].strip())
            except json.JSONDecodeError:
                parsed = None
            data = parsed if isinstance(parsed, dict) else None
    return event, data


async def _iter_frames(response: httpx.Response) -> AsyncIterator[str]:
    buffer = ""
    async for chunk in response.aiter_text():
        buffer += chunk
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            yield frame


class _StreamCollector:
    """Reads an interactions SSE stream held open across both deliveries, appending every
    frame to a list the test scans. Keying the terminal count on the ``event:`` line (not the
    payload) is required: both terminal types render the same ``{interaction_id, group_id}``."""

    def __init__(self) -> None:
        self.frames: list[str] = []
        self._task: asyncio.Task[None] | None = None

    async def _run(self, url: str, token: str) -> None:
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=None) as client, client.stream("GET", url, headers=headers) as response:
            async for frame in _iter_frames(response):
                self.frames.append(frame)

    def start(self, url: str, token: str) -> None:
        self._task = asyncio.create_task(self._run(url, token))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
                await self._task

    def add_interaction_id(self, question: str) -> str | None:
        for frame in list(self.frames):
            event, data = _parse_frame(frame)
            if event == "interaction.add" and data is not None and data.get("question") == question:
                return data.get("interaction_id")
        return None

    def backlog_done(self) -> bool:
        return any(_parse_frame(frame)[0] == "interaction.backlog_done" for frame in list(self.frames))

    def answered_count(self, interaction_id: str) -> int:
        count = 0
        for frame in list(self.frames):
            event, data = _parse_frame(frame)
            if event == "interaction.answered" and data is not None and data.get("interaction_id") == interaction_id:
                count += 1
        return count


def _log_has(stack: TaiStack, needle: str) -> str:
    text = stack.process("serve-a").log_path.read_text(encoding="utf-8", errors="replace")
    return text if needle in text else ""


@pytest.mark.backendless
async def test_stripe_payment_webhook_loop(
    payments_stack: tuple[TaiStack, str], fake_stripe: FakeStripe, uniq: Callable[[str], str]
) -> None:
    stack, root_token = payments_stack
    secret = stack.config.env["E2E_STRIPE_WEBHOOK_SECRET"].encode()
    api = stack.api(port=stack.port_a)
    topic, preset_name = await _setup_flow(stack, api, uniq)

    # Open the SSE stream BEFORE the first delivery and hold it across both, so the add frame
    # is a live-tail frame and no terminal frame fires before the connection.
    stream = _StreamCollector()
    stream.start(f"http://{stack.host}:{stack.port_a}/api/interactions/stream", root_token)
    try:
        await _until(lambda: stream.backlog_done() or None, deadline=10.0, message="stream never drained its backlog")

        # --- Item 1: the loop closes ---------------------------------------
        question = uniq("q")
        before = set(fake_stripe.session_ids())
        ask_task = _open_ask(stack, root_token, preset_name, question)
        try:
            sid = await _until(
                lambda: _new_session_id(fake_stripe, before), deadline=15.0, message="no session was minted"
            )
            interaction_id = await _until(
                lambda: stream.add_interaction_id(question), deadline=15.0, message="no add frame for the ask"
            )
            callback_url = fake_stripe.session(sid)["metadata"]["tai_callback_url"]

            # --- Item 6 (live half): GET the callback URL while the ask is LIVE -> 404 ---
            live_get = await _get_callback(callback_url)
            assert live_get.status_code == 404, live_get.text

            # The payer pays at the stub, then the signed completed event reaches the topic.
            fake_stripe.complete_payment(sid)
            completed = _completed_event(fake_stripe.session(sid))
            ingress = await _deliver(stack, topic, secret, completed)
            assert ingress.status_code == 200, ingress.text

            answer = _as_answer(await asyncio.wait_for(ask_task, timeout=20.0))
        finally:
            if not ask_task.done():
                ask_task.cancel()

        # --- Item 1 + 2: the woken payload -----------------------------------
        assert answer["status"] == "paid", answer
        assert answer["amount_total"] == _AMOUNT
        assert answer["currency"] == _CURRENCY
        assert set(answer.keys()) == _ANSWER_KEYS, f"answer key set is not the whitelist: {sorted(answer)}"

        # --- Item 6: GET after ANSWERED -> still 404 (bound-question oracle closed) ---
        answered_get = await _get_callback(callback_url)
        assert answered_get.status_code == 404, answered_get.text

        # --- Item 3: idempotency — a duplicate delivery adds no second terminal frame ---
        # The terminal frame is XADD'd during the claim but reaches the stream collector over a
        # separate async hop, so poll for it (via the sanctioned wait primitive) rather than
        # reading a point value that could race ahead of the frame.
        def _answered_once() -> bool | None:
            return True if stream.answered_count(interaction_id) == 1 else None

        await _until(_answered_once, deadline=6.0, interval=0.2, message="answered frame never arrived")
        dup = await _deliver(stack, topic, secret, completed)
        assert dup.status_code == 200, dup.text
        # Hold past the duplicate's dispatch: a successful duplicate writes no completion
        # signal, so poll a NAMED deadline (via the sanctioned wait primitive) and require the
        # count to stay at one the whole time — a second frame at any tick is a double-wake.
        drain_until = time.monotonic() + 3.0

        def _drain_stayed_one() -> bool | None:
            assert stream.answered_count(interaction_id) == 1, "a duplicate delivery double-woke the ask"
            return True if time.monotonic() >= drain_until else None

        await _until(_drain_stayed_one, deadline=6.0, interval=0.2, message="drain window never elapsed")

        # --- Item 4: the pin cannot be displaced (injection) -----------------
        # A baked key is hidden from the transformed schema (additionalProperties: False), so
        # passing it is rejected BEFORE any Stripe call — no new session is minted.
        for injected in ({"question": uniq("q"), "amount": 1}, {"question": uniq("q"), "answer_schema": {}}):
            count_before = fake_stripe.session_count
            async with stack.mcp(port=stack.port_a, auth=root_token) as mcp:
                result = await mcp.call_tool(preset_name, injected, raise_on_error=False, retry_on_reloading=True)
            assert result.is_error, f"injecting a baked key must fail: {injected}"
            assert fake_stripe.session_count == count_before, "a rejected create still reached the stub"

        # --- Item 5: the forged cheap session is rejected AND the ask survives ---
        forged_question = uniq("q")
        before5 = set(fake_stripe.session_ids())
        ask5 = _open_ask(stack, root_token, preset_name, forged_question)
        try:
            sid5 = await _until(
                lambda: _new_session_id(fake_stripe, before5), deadline=15.0, message="no session for the forged leg"
            )
            callback5 = fake_stripe.session(sid5)["metadata"]["tai_callback_url"]
            # A self-consistent CHEAP event, valid in every respect except the pinned amount:
            # stamps agree with each other, currency agrees, type/paid/livemode all correct.
            forged_session = {
                "id": f"cs_forged_{uniq('f')}",
                "object": "checkout.session",
                "status": "complete",
                "payment_status": "paid",
                "payment_intent": f"pi_forged_{uniq('p')}",
                "amount_total": 100,
                "currency": _CURRENCY,
                "livemode": False,
                "customer_details": {"email": None},
                "metadata": {"tai_callback_url": callback5, "tai_amount": "100", "tai_currency": _CURRENCY},
            }
            forged_ingress = await _deliver(stack, topic, secret, _completed_event(forged_session))
            assert forged_ingress.status_code == 200, forged_ingress.text

            # FIRST: the door's 400 reaches the hook-failure log, matched by the pinned
            # status-bearing literal (never a bare "400", which a ValueError line could carry).
            logged = await _until(
                lambda: _log_has(stack, "callback door refused the answer: HTTP 400"),
                deadline=20.0,
                message="the forged delivery's 400 never reached the hook-failure log",
            )
            assert "callback door refused the answer: HTTP 400" in logged
            # THEN: the ask survived the rejection, its ticket unconsumed.
            assert not ask5.done(), "the forged delivery answered the ask instead of being rejected"
            # THEN: the CORRECT event answers it — proof the ticket really survived.
            fake_stripe.complete_payment(sid5)
            correct_ingress = await _deliver(stack, topic, secret, _completed_event(fake_stripe.session(sid5)))
            assert correct_ingress.status_code == 200, correct_ingress.text
            answer5 = _as_answer(await asyncio.wait_for(ask5, timeout=20.0))
            assert answer5["status"] == "paid"
            assert answer5["amount_total"] == _AMOUNT
        finally:
            if not ask5.done():
                ask5.cancel()
    finally:
        await stream.stop()


async def _mint_filler(fake_stripe: FakeStripe) -> str:
    """Mint one UNSELECTABLE filler through the stub's REAL create endpoint (no
    ``tai_callback_url`` metadata), then complete it so it lists — it drives the cursor
    without adding an answer."""
    form = {
        "mode": "payment",
        "success_url": "https://acme.example/thanks",
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": "777",
        "line_items[0][price_data][product_data][name]": "Filler",
        "line_items[0][quantity]": "1",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(f"{fake_stripe.api_base_url}/v1/checkout/sessions", data=form)
    sid = response.json()["id"]
    fake_stripe.complete_payment(sid)
    return sid


@pytest.mark.backendless
async def test_stripe_reconciliation_recovers_a_lost_payment(
    payments_stack: tuple[TaiStack, str], fake_stripe: FakeStripe, uniq: Callable[[str], str]
) -> None:
    stack, root_token = payments_stack
    api = stack.api(port=stack.port_a)
    _topic, preset_name = await _setup_flow(stack, api, uniq)

    # --- Item 1: money moved at Stripe, the platform never heard about it ----
    question = uniq("q")
    before = set(fake_stripe.session_ids())
    ask_task = _open_ask(stack, root_token, preset_name, question)
    try:
        sid = await _until(lambda: _new_session_id(fake_stripe, before), deadline=15.0, message="no session was minted")
        # Complete at the stub ONLY; deliver no webhook. The ask stays blocked.
        fake_stripe.complete_payment(sid)
        assert not ask_task.done(), "the ask answered without any delivery"

        # --- Item 2 + 3: a direct authed reconcile wakes it, with the whitelist payload ---
        async with stack.mcp(port=stack.port_a, auth=root_token) as mcp:
            first = await mcp.call_tool("reconcile_stripe_payments", {"lookback_hours": 26}, retry_on_reloading=True)
        summary = _as_answer(first.data if first.data is not None else first.structured_content)
        assert summary["selected"] == 1, summary
        assert summary["answered"] == 1, summary
        assert summary["already_answered"] == 0, summary
        assert summary["expired"] == 0, summary
        assert summary["rejected"] == 0, summary
        assert summary["failed"] == [], summary
        # The totality identity holds.
        assert summary["selected"] == (
            summary["answered"]
            + summary["already_answered"]
            + summary["expired"]
            + summary["rejected"]
            + len(summary["failed"])
        )

        answer = _as_answer(await asyncio.wait_for(ask_task, timeout=20.0))
    finally:
        if not ask_task.done():
            ask_task.cancel()
    assert answer["status"] == "paid"
    assert answer["amount_total"] == _AMOUNT
    assert answer["currency"] == _CURRENCY
    assert set(answer.keys()) == _ANSWER_KEYS, f"reconcile answer key set is not the whitelist: {sorted(answer)}"

    # --- Item 4: a SECOND run reports the same session already_answered, no raise ---
    async with stack.mcp(port=stack.port_a, auth=root_token) as mcp:
        second = await mcp.call_tool("reconcile_stripe_payments", {"lookback_hours": 26}, retry_on_reloading=True)
    summary2 = _as_answer(second.data if second.data is not None else second.structured_content)
    assert summary2["selected"] == 1, summary2
    assert summary2["already_answered"] == 1, summary2
    assert summary2["answered"] == 0, summary2
    assert summary2["failed"] == [], summary2

    # --- Item 5: the cursor-paging path really ran ---------------------------
    # 100 unselectable fillers + the 1 real (already-answered) session = 101 completed, one
    # more than a page (limit 100), so the run's second list request carries starting_after.
    for _ in range(100):
        await _mint_filler(fake_stripe)
    fake_stripe.list_requests.clear()
    async with stack.mcp(port=stack.port_a, auth=root_token) as mcp:
        paged = await mcp.call_tool("reconcile_stripe_payments", {"lookback_hours": 26}, retry_on_reloading=True)
    paged_summary = _as_answer(paged.data if paged.data is not None else paged.structured_content)
    # Only the genuinely selectable session counts; the fillers carry no tai_callback_url.
    assert paged_summary["selected"] == 1, paged_summary
    assert paged_summary["already_answered"] == 1, paged_summary
    assert paged_summary["failed"] == [], paged_summary
    # The cursor loop was exercised: a second list request in the run carried starting_after.
    assert any("starting_after" in params for params in fake_stripe.list_requests), fake_stripe.list_requests
