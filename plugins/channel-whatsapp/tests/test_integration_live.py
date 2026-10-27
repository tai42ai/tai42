"""Live integration tests against the real WhatsApp API (outbound only).

Run with ``pytest -m integration``. Reads the operator's
``CHANNEL_WHATSAPP_*`` credentials from the environment and skips cleanly
when any is unset. Only the outbound Graph calls are live; the correlation store is the
in-memory fake, except in the endpoint leg, which writes to the host's store
(``CHANNEL_WHATSAPP_REDIS_URL``) so the host's data endpoint can serve the Flow it sends.
The flow legs (``CHANNEL_WHATSAPP_WABA_ID`` set) create and publish
Flows on that WABA. The lifecycle probes clean up every Flow they create, however the probe
ends: a draft is deleted, and a published one is deprecated (Meta never deletes a published
Flow, so a deprecated probe Flow stays listed on the WABA).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx
import pytest
from tai42_contract.channels import ChannelDeliveryError
from tai42_kit.clients import client_ctx as kit_client_ctx
from tai42_kit.clients import shutdown_all_clients
from tai42_kit.clients.impl.http import HttpxClient
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.settings import reset_all_settings

from tai42_channel_whatsapp import WhatsAppChannel
from tai42_channel_whatsapp.client import send_message
from tai42_channel_whatsapp.flow_assets import FlowCreateResult, create_flow, delete_flow, list_flows, publish_flow
from tai42_channel_whatsapp.flows import build_form_flow
from tai42_channel_whatsapp.graph import send_request
from tai42_channel_whatsapp.settings import whatsapp_settings

from .conftest import FakeRedis, make_delivery

_ENV_KEYS = (
    "CHANNEL_WHATSAPP_ACCESS_TOKEN",
    "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID",
    "CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS",
)


def _missing_creds() -> bool:
    return not all(os.environ.get(key) for key in _ENV_KEYS)


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_missing_creds(), reason="CHANNEL_WHATSAPP_* not set"),
]


class _LiveHttpx:
    """One-shot real ``httpx.AsyncClient`` per call — the live stand-in for the pooled client."""

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(trust_env=False, timeout=30.0) as client:
            return await client.get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(trust_env=False, timeout=30.0) as client:
            return await client.post(url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(trust_env=False, timeout=30.0) as client:
            return await client.delete(url, **kwargs)


@pytest.fixture(autouse=True)
def live_clients(stub_app):
    # The operator's real env must win over any cached test settings, and the
    # send path must reach the real API (the correlation store stays in-memory).
    reset_all_settings()
    stub_app.clients.by_class[HttpxClient] = _LiveHttpx()
    stub_app.clients.by_class[RedisClient] = FakeRedis()
    yield
    reset_all_settings()


async def test_send_message_returns_a_wamid_and_the_recipient_wa_id():
    settings = whatsapp_settings()
    assert settings.default_phone_number_id is not None
    recipient = settings.allowed_recipients[0]

    sent = await send_message(
        phone_number_id=settings.default_phone_number_id,
        to=recipient,
        body="tai42-channel-whatsapp live smoke: send_message",
    )

    assert sent.wamid.startswith("wamid.")
    assert sent.wa_id.isdigit()


async def test_full_deliver_reaches_the_configured_human(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_WHATSAPP_REDIS_URL", "redis://in-memory-fake/0")
    reset_all_settings()

    settings = whatsapp_settings()
    await WhatsAppChannel().deliver(
        make_delivery(
            recipient=settings.allowed_recipients[0],
            question="tai42-channel-whatsapp live smoke: full deliver — reply is not expected.",
        )
    )


@pytest.mark.skipif(
    not os.environ.get("CHANNEL_WHATSAPP_WABA_ID"), reason="CHANNEL_WHATSAPP_WABA_ID not set (form leg needs a WABA)"
)
async def test_stepped_prefilled_form_creates_publishes_and_sends(monkeypatch: pytest.MonkeyPatch):
    # The vendor authority for the publishable Flow JSON shape: a two-page prefilled form
    # carrying a TextInput, a Dropdown with per-send options, AND an OptIn boolean,
    # delivered through the real Graph API. deliver() runs build_form_flow →
    # _resolve_flow_id → a real create + publish, then the send. It SUCCEEDS only if the
    # create returned no validation_errors (else _resolve_flow_id raises and deletes the
    # draft), the publish returned 200, and Meta accepted the send — proof that a screen
    # id with a digit and a control-level init-value inside a Form are both gone, across
    # every control type, the boolean included, and the prefill reaches the recipient.
    from tai42_contract.interactions.models import FormData, FormOption, FormPage

    monkeypatch.setenv("CHANNEL_WHATSAPP_REDIS_URL", "redis://in-memory-fake/0")
    reset_all_settings()
    settings = whatsapp_settings()

    schema = {
        "type": "object",
        "properties": {
            "note": {"type": "string", "title": "Note"},
            "tier": {"type": "string", "title": "Tier"},
            "agree": {"type": "boolean", "title": "Agree"},
        },
        "required": ["note"],
    }
    data = FormData(
        values={"note": "prefilled", "tier": "gold", "agree": True},
        options={"tier": [FormOption(value="gold", label="Gold"), FormOption(value="silver", label="Silver")]},
    )
    pages = [FormPage(title="Details", fields=["note", "tier"]), FormPage(title="Confirm", fields=["agree"])]

    await WhatsAppChannel().deliver(
        make_delivery(
            recipient=settings.allowed_recipients[0],
            answer_format="form",
            schema=schema,
            data=data,
            pages=pages,
            question="tai42-channel-whatsapp live smoke: stepped prefilled form — reply is not expected.",
        )
    )


_needs_waba = pytest.mark.skipif(
    not os.environ.get("CHANNEL_WHATSAPP_WABA_ID"), reason="CHANNEL_WHATSAPP_WABA_ID not set (flow legs need a WABA)"
)


def _probe_flow_json() -> dict[str, Any]:
    """A publishable static Flow for the lifecycle probes, from the plugin's own renderer."""
    flow_json, _ = build_form_flow({"type": "object", "properties": {"note": {"type": "string"}}})
    return flow_json


def _probe_name() -> str:
    return f"tai42-live-probe-{uuid4().hex}"


class _ProbeFlows:
    """The Flows one live probe created on the WABA, each with the state the probe left it in."""

    def __init__(self, waba_id: str, api_base_url: str) -> None:
        self._waba_id = waba_id
        self._api_base_url = api_base_url
        self._states: dict[str, str] = {}

    async def create(
        self, name: str, flow_json: dict[str, Any] | None = None, endpoint_uri: str | None = None
    ) -> FlowCreateResult:
        created = await create_flow(
            waba_id=self._waba_id, name=name, flow_json=flow_json or _probe_flow_json(), endpoint_uri=endpoint_uri
        )
        self._states[created.flow_id] = "DRAFT"
        return created

    async def publish(self, flow_id: str) -> None:
        await publish_flow(flow_id)
        self._states[flow_id] = "PUBLISHED"

    async def deprecate(self, flow_id: str) -> None:
        await send_request(f"{self._api_base_url}/{flow_id}/deprecate", {})
        self._states[flow_id] = "DEPRECATED"

    async def delete(self, flow_id: str) -> None:
        await delete_flow(flow_id)
        del self._states[flow_id]

    async def clean_up(self) -> None:
        """Delete every draft and deprecate every published Flow; raise naming each one that failed."""
        failures: list[str] = []
        for flow_id, state in list(self._states.items()):
            try:
                if state == "DRAFT":
                    await self.delete(flow_id)
                elif state == "PUBLISHED":
                    await self.deprecate(flow_id)
            except ChannelDeliveryError as exc:
                failures.append(f"{flow_id} ({state}): {exc}")
        if failures:
            raise AssertionError("live probe could not clean up its flows: " + "; ".join(failures))


@asynccontextmanager
async def _probe_flows() -> AsyncIterator[_ProbeFlows]:
    """A probe's Flow tracker, cleaned up in a ``finally`` whether the probe passed or failed."""
    settings = whatsapp_settings()
    assert settings.waba_id is not None
    flows = _ProbeFlows(settings.waba_id, settings.api_base_url)
    try:
        yield flows
    finally:
        await flows.clean_up()


@_needs_waba
async def test_flow_list_page_shape_and_after_cursor_paging():
    # Meta's documented list shape: ``data[]`` of ``{id, name, status}`` and ``paging.cursors.after``;
    # ``next`` is present only while another page follows, and that page is reached by ``after``.
    settings = whatsapp_settings()
    assert settings.waba_id is not None
    url = f"{settings.api_base_url}/{settings.waba_id}/flows"

    first = (await send_request(url, method="get")).json()
    assert isinstance(first["data"], list)
    for item in first["data"]:
        assert {"id", "name", "status"} <= set(item)
    if "next" in first.get("paging", {}):
        after = first["paging"]["cursors"]["after"]
        second = (await send_request(url, method="get", params={"after": after})).json()
        assert isinstance(second["data"], list)
        assert {item["id"] for item in second["data"]}.isdisjoint({item["id"] for item in first["data"]})

    flows = await list_flows(settings.waba_id)
    assert {flow.flow_id for flow in flows} >= {item["id"] for item in first["data"]}


@_needs_waba
async def test_create_under_a_published_flow_name_is_refused_as_not_unique():
    # A create under a name a PUBLISHED Flow holds answers HTTP 400, code 100, titled
    # "Flow name is not unique" (an ``error_subcode`` rides along; nothing here depends on it).
    name = _probe_name()
    async with _probe_flows() as flows:
        created = await flows.create(name)
        assert created.validation_errors == []
        await flows.publish(created.flow_id)

        with pytest.raises(ChannelDeliveryError) as excinfo:
            await flows.create(name)

    assert "HTTP 400" in str(excinfo.value)
    assert "code=100" in str(excinfo.value)
    assert "Flow name is not unique" in str(excinfo.value)


async def _recreate_outcome(flows: _ProbeFlows, name: str) -> str:
    """Create again under ``name`` and say whether Meta took the name or refused it as not unique."""
    try:
        again = await flows.create(name)
    except ChannelDeliveryError as exc:
        refusal = str(exc)
    else:
        return f"free: created draft {again.flow_id}"
    assert "Flow name is not unique" in refusal
    return f"reserved: {refusal}"


@_needs_waba
async def test_create_under_a_deprecated_flow_name_outcome(record_property):
    # Whether a DEPRECATED Flow still reserves its name is not in Meta's docs. This probe
    # publishes and deprecates a Flow, re-creates under its name, and records which way
    # Meta answers: a new draft (the name is free) or the name-not-unique refusal.
    name = _probe_name()
    async with _probe_flows() as flows:
        created = await flows.create(name)
        await flows.publish(created.flow_id)
        await flows.deprecate(created.flow_id)
        record_property("deprecated_name_outcome", await _recreate_outcome(flows, name))


@_needs_waba
async def test_create_under_a_deleted_draft_name_outcome(record_property):
    # The resolve deletes a DRAFT found under a Flow's name and then creates under that name,
    # which holds only if deleting the draft frees its name. This probe creates a draft,
    # deletes it, re-creates under the same name, and records which way Meta answers.
    name = _probe_name()
    async with _probe_flows() as flows:
        created = await flows.create(name)
        await flows.delete(created.flow_id)
        record_property("deleted_draft_name_outcome", await _recreate_outcome(flows, name))


# --- the declared shapes on a live Flow -------------------------------------------------

# Every shape family the Flow draws natively (required on a CalendarPicker date, a pattern with its
# helper text, the whole-number pattern on an integer, the platform regex on a time) and one each
# that only the data endpoint checks (an email format, a number bound), with every control binding
# its ``error-message`` and a merged range binding its one error object.
_SHAPED_LIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "title": "Code", "pattern": "^[0-9]{4}$"},
        "count": {"type": "integer", "title": "Count"},
        "at": {"type": "string", "title": "Time", "format": "time"},
        "on": {"type": "string", "title": "Day", "format": "date"},
        "start": {"type": "string", "title": "From", "format": "date"},
        "end": {"type": "string", "title": "To", "format": "date", "rangeStart": "start", "maxDays": 5},
        "email": {"type": "string", "title": "Email address", "format": "email"},
        "qty": {"type": "integer", "title": "Quantity", "minimum": 1},
    },
    "required": ["code", "on", "email"],
}

_needs_flow_endpoint = pytest.mark.skipif(
    not (
        os.environ.get("CHANNEL_WHATSAPP_WABA_ID")
        and os.environ.get("CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY")
        and os.environ.get("CHANNEL_WHATSAPP_FLOW_ENDPOINT_URI")
    ),
    reason="CHANNEL_WHATSAPP_WABA_ID / _FLOW_PRIVATE_KEY / _FLOW_ENDPOINT_URI not set (the endpoint leg needs each)",
)


@_needs_waba
async def test_native_shapes_publish_without_validation_errors():
    # The vendor authority for the native shape attributes: a static Flow carrying a TextInput
    # ``pattern`` with its helper text, an integer's whole-number pattern, a time's regex pattern, a
    # single-mode CalendarPicker with ``required``, and an ``error-message`` bound to an empty
    # ``__err`` key on every control, created as a draft — Meta returns its validation errors for any
    # attribute it refuses.
    native = {
        "type": "object",
        "properties": {key: _SHAPED_LIVE_SCHEMA["properties"][key] for key in ("code", "count", "at", "on")},
        "required": ["code", "on"],
    }
    flow_json, _ = build_form_flow(native)
    assert "data_api_version" not in flow_json
    async with _probe_flows() as flows:
        created = await flows.create(_probe_name(), flow_json)
        assert created.validation_errors == []


@_needs_waba
async def test_a_boolean_review_readback_publishes_without_validation_errors():
    # The vendor authority for the review readback of a boolean: an ``If`` on the boolean's ``__val``
    # carrier at the screen's top level, each branch one literal ``TextBody`` with the caller's word,
    # created as a draft — Meta returns its validation errors for any shape it refuses.
    schema = {
        "type": "object",
        "properties": {"agree": {"type": "boolean", "title": "Agree"}, "note": {"type": "string"}},
    }
    pages = [
        {"title": "Ask", "fields": ["agree", "note"], "kind": "input"},
        {"title": "Check", "fields": [], "kind": "review"},
    ]
    flow_json, _ = build_form_flow(schema, pages, boolean_words={"agree": ("Si", "No")})
    async with _probe_flows() as flows:
        created = await flows.create(_probe_name(), flow_json)
        assert created.validation_errors == []


@_needs_waba
async def test_a_merged_range_publishes_without_validation_errors():
    # The vendor authority for a merged range pair's error text: a static Flow whose one range-mode
    # CalendarPicker binds its ``error-message`` to one screen-data object ``{start-date, end-date}``,
    # created as a draft — Meta returns its validation errors for any shape it refuses.
    pair = {
        "type": "object",
        "properties": {key: _SHAPED_LIVE_SCHEMA["properties"][key] for key in ("start", "end")},
        "required": ["start", "end"],
    }
    flow_json, _ = build_form_flow(pair)
    assert "data_api_version" not in flow_json
    async with _probe_flows() as flows:
        created = await flows.create(_probe_name(), flow_json)
        assert created.validation_errors == []


@_needs_flow_endpoint
async def test_a_multi_value_visible_when_publishes_without_validation_errors():
    # The vendor authority for a field shown for several values of its controller: one Dropdown and
    # one CalendarPicker drawn per case, each case under its own ``If`` on one value, and a field
    # chained under the Dropdown's cases — on one screen (a ``${form.…}`` controller) and with the
    # controller on an earlier screen (a ``${data.…}`` carrier). Each is an endpoint-driven Flow whose
    # payloads carry one plain reference per case, created as a draft against the configured data
    # endpoint — Meta returns its validation errors for anything it refuses.
    schema = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "title": "Kind", "enum": ["a", "b", "c"]},
            "target": {"type": "string", "title": "Target", "visibleWhen": {"field": "kind", "in": ["a", "b"]}},
            "day": {
                "type": "string",
                "title": "Day",
                "format": "date",
                "visibleWhen": {"field": "kind", "in": ["a", "c"]},
            },
            "note": {"type": "string", "title": "Note", "visibleWhen": {"field": "target", "equals": "x"}},
        },
        "required": ["kind", "target", "day"],
    }
    two_pages = [
        {"title": "First", "fields": ["kind"], "kind": "input", "display": []},
        {"title": "Second", "fields": ["target", "day", "note"], "kind": "input", "display": []},
    ]
    layouts = [build_form_flow(schema, pages, option_fields={"target"})[0] for pages in (None, two_pages)]
    endpoint_uri = whatsapp_settings().flow_endpoint_uri
    async with _probe_flows() as flows:
        for flow_json in layouts:
            assert flow_json["data_api_version"] == "3.0"
            created = await flows.create(_probe_name(), flow_json, endpoint_uri=endpoint_uri)
            assert created.validation_errors == []


_needs_host_store = pytest.mark.skipif(
    not os.environ.get("CHANNEL_WHATSAPP_REDIS_URL"),
    reason=(
        "CHANNEL_WHATSAPP_REDIS_URL not set: the endpoint leg must write the form's sidecar to the store the "
        "host's data endpoint reads, through the same setting the host uses"
    ),
)


@pytest.fixture
async def host_store(stub_app, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    # The correlation store this process writes is the host's real one: the kit's pooled Redis client
    # opened from ``CHANNEL_WHATSAPP_REDIS_URL`` (and its sibling connection settings), exactly as the
    # host opens it. Every other client stays the test's stub.
    stubbed = stub_app.clients.client_ctx

    def client_ctx(client_cls: type, settings: Any = None, **kwargs: Any) -> Any:
        if client_cls is RedisClient:
            return kit_client_ctx(RedisClient, settings, **kwargs)
        return stubbed(client_cls, settings, **kwargs)

    monkeypatch.setattr(stub_app.clients, "client_ctx", client_ctx)
    yield
    await shutdown_all_clients()


@_needs_flow_endpoint
@_needs_host_store
async def test_shape_checked_form_publishes_endpoint_driven_and_sends(host_store, record_property):
    # The whole shape-checked form delivered through the real Graph API, published endpoint-driven
    # against the configured data endpoint (served by this plugin on the host), with the form's
    # sidecar written to the host's own store so its data endpoint serves every screen. deliver()
    # SUCCEEDS only if Meta created the Flow with no validation errors (the range control's one
    # ``error-message`` binding to its error object included), published it, and accepted the send.
    # What the person reads on each failure is then observed on the phone and recorded beside the run:
    # the native refusals (an empty required field; a pattern mismatch; whether the email/phone
    # keyboards refuse on their own), that native checks run before a ``data_exchange`` footer, that a
    # same-screen refusal keeps the person on the screen with the bound ``error-message`` showing its
    # sentence and an empty one showing nothing, the longest dynamic error text shown whole, and that
    # every control keeps what the person typed after a refusal (the refusal carries the screen's whole
    # data, each control's ``__init`` set to the value the person left in it). The final submit reaches
    # a host that holds no parked ask for this probe's interaction, so what follows the submit is not
    # part of this leg.
    settings = whatsapp_settings()
    delivery = make_delivery(
        recipient=settings.allowed_recipients[0],
        answer_format="form",
        schema=_SHAPED_LIVE_SCHEMA,
        question="tai42-channel-whatsapp live probe: shape-checked form — fill it with bad values first.",
    )
    await WhatsAppChannel().deliver(delivery)
    record_property("shape_probe_interaction", delivery.interaction_id)
    record_property(
        "shape_probe_observe",
        "empty required text; pattern mismatch on Code; dhdhdh in the email keyboard; 0 in Quantity; "
        "a same-screen refusal; an empty error-message on every clean field; every typed value kept after it",
    )
