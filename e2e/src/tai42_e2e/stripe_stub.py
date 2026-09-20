"""A recording loopback stub of Stripe's Checkout Session REST API, thread-hosted
in the pytest process. Every session it mints and field it echoes is what a real
Stripe integration would produce, so a leg passing against it could pass against
Stripe; no run-time SUT call reaches a real Stripe host."""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tai42_e2e._threaded import ThreadedServer
from tai42_e2e.ports import allocate_port
from tai42_e2e.provider_stub import _install_catch_all


class FakeStripe:
    """A recording loopback stub of Stripe's Checkout Session REST API.

    Serves ``POST /v1/checkout/sessions`` (mint a session ``status=open`` /
    ``payment_status=unpaid``, echoing the create's amount, currency and decoded metadata)
    and ``GET /v1/checkout/sessions`` (list ``status=complete`` sessions created at or after
    ``created[gte]``, honouring the ``starting_after`` cursor and ``limit`` server-side). Any
    other path answers a loud 500. A session is completed only through
    :meth:`complete_payment`, which flips BOTH ``status`` and ``payment_status`` and mints the
    ``payment_intent`` — the two fields decide different things (whether the session lists,
    and whether it is answered), so no test reaches into a session dict and flips one."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = allocate_port()
        # Sessions by id, in creation order (the cursor's stable ordering).
        self._sessions: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        # Deterministic Stripe idempotency: a retried create for the same Idempotency-Key
        # returns the stored session instead of minting a second, as Stripe does.
        self._by_idempotency: dict[str, str] = {}
        # Recorded list-request query params (each call), so the paging leg can assert the
        # second request of a run carried ``starting_after``.
        self.list_requests: list[dict[str, str]] = []
        self._server = ThreadedServer(self._build_app(), host, self.port)

    @property
    def api_base_url(self) -> str:
        """The value ``STRIPE_API_BASE`` points at (the tools address
        ``{api_base_url}/v1/checkout/sessions``)."""
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()

    def reset(self) -> None:
        """Clear recorded requests AND stored sessions. Called between the two payment
        tests, which share one module-scoped stack, so the second never inherits the
        first's sessions."""
        self._sessions.clear()
        self._order.clear()
        self._by_idempotency.clear()
        self.list_requests.clear()

    @property
    def session_count(self) -> int:
        """How many sessions the stub has minted — the injection leg asserts a rejected
        create reached no Stripe call, so this never moved."""
        return len(self._sessions)

    def complete_payment(self, session_id: str) -> dict[str, Any]:
        """Complete a session the way a real payment does: ``status=complete`` AND
        ``payment_status=paid``, minting the ``payment_intent``. Both flips are required —
        ``status`` decides whether the session comes back from the list at all,
        ``payment_status`` decides whether it is answered."""
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"FakeStripe has no session {session_id!r} to complete")
        session["status"] = "complete"
        session["payment_status"] = "paid"
        session["payment_intent"] = f"pi_test_{uuid.uuid4().hex}"
        return session

    def session(self, session_id: str) -> dict[str, Any]:
        """The stored session dict (the test reads its captured fields to forge or sign a
        delivery from the real session)."""
        return self._sessions[session_id]

    def session_ids(self) -> list[str]:
        """The minted session ids in creation order — the test diffs this before/after
        opening an ask to learn which session the composed tool created for it."""
        return list(self._order)

    def _mint_session(self, *, amount_total: int, currency: str, metadata: dict[str, str]) -> dict[str, Any]:
        session_id = f"cs_test_{uuid.uuid4().hex}"
        session = {
            "id": session_id,
            "object": "checkout.session",
            "status": "open",
            "payment_status": "unpaid",
            "payment_intent": None,
            "amount_total": amount_total,
            "currency": currency,
            "livemode": False,
            # Raw (unflattened) shape: the reconciler reads ``customer_details.email`` and
            # build_answer_payload's fallback branch runs; ``customer_email`` stays absent.
            "customer_details": {"email": None},
            "metadata": metadata,
            "created": int(time.time()),
            "url": f"http://{self.host}:{self.port}/pay/{session_id}",
        }
        self._sessions[session_id] = session
        self._order.append(session_id)
        return session

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/v1/checkout/sessions")
        async def create_session(request: Request) -> JSONResponse:
            form = await request.form()
            pairs = list(form.multi_items())
            values = dict(pairs)
            idempotency_key = request.headers.get("idempotency-key")
            if idempotency_key is not None and idempotency_key in self._by_idempotency:
                return JSONResponse(self._sessions[self._by_idempotency[idempotency_key]])
            # amount + currency come off the single line item; a create with neither is a
            # malformed request and 400s (never a silent default that would hide a bad body).
            try:
                amount_total = int(str(values["line_items[0][price_data][unit_amount]"]))
                currency = str(values["line_items[0][price_data][currency]"])
            except (KeyError, ValueError):
                return JSONResponse({"error": {"message": "fake_stripe: missing line-item amount/currency"}}, 400)
            # Metadata is OPTIONAL: a create with no ``metadata[<key>]`` params (the page
            # fillers) stores an empty map rather than being rejected.
            metadata = {
                key[len("metadata[") : -1]: str(value)
                for key, value in pairs
                if key.startswith("metadata[") and key.endswith("]")
            }
            session = self._mint_session(amount_total=amount_total, currency=currency, metadata=metadata)
            if idempotency_key is not None:
                self._by_idempotency[idempotency_key] = session["id"]
            return JSONResponse(session)

        @app.get("/v1/checkout/sessions")
        async def list_sessions(request: Request) -> JSONResponse:
            params = dict(request.query_params)
            self.list_requests.append(params)
            created_gte = int(params.get("created[gte]", "0"))
            status = params.get("status")
            limit = int(params.get("limit", "10"))
            starting_after = params.get("starting_after")
            # Server-side narrowing: honour the status filter and the created[gte] floor, in
            # the stable creation order the cursor pages through.
            candidates = [
                self._sessions[sid]
                for sid in self._order
                if (status is None or self._sessions[sid]["status"] == status)
                and self._sessions[sid]["created"] >= created_gte
            ]
            if starting_after is not None:
                ids = [s["id"] for s in candidates]
                candidates = candidates[ids.index(starting_after) + 1 :] if starting_after in ids else []
            page = candidates[:limit]
            has_more = len(candidates) > limit
            return JSONResponse({"object": "list", "url": "/v1/checkout/sessions", "has_more": has_more, "data": page})

        _install_catch_all(app, "stripe")
        return app
