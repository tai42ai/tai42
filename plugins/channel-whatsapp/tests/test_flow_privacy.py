"""The published WhatsApp Flow artefact carries no personal data, and one Flow is
reused across guests.

A form send creates and publishes a Flow on Meta (the create's ``{name, flow_json,
endpoint_uri?}`` body is the artefact Meta stores). These guards assert that artefact
depends only on the form's schema/pages/options/reactions — never on the guest it is
sent to:

* REUSE — two sends of the SAME form to DIFFERENT guests resolve one cached Flow id,
  so ``create_flow`` runs ONCE, not per send (covers the plain form ask, the ask-less
  notification, and the reacting/endpoint-driven form).
* NO-PII — the published name and ``flow_json`` contain no guest phone number
  (the recipient ``wa_id``) and no correlation token (the ``interaction_id`` /
  ``flow_token``); the correlation token rides the send's runtime parameters only.
"""

from __future__ import annotations

import json

import httpx
import pytest
from tai42_contract.interactions.models import FormData, FormOption, FormPage, FormReactions

from tai42_channel_whatsapp.channel import WhatsAppChannel
from tai42_channel_whatsapp.flows import build_form_flow

from .conftest import (
    ALLOWED_A,
    ALLOWED_B,
    PHONE_NUMBER_ID,
    FakeHttpx,
    FakeRedis,
    _accepted,
    make_delivery,
    response,
)

pytestmark = pytest.mark.usefixtures("whatsapp_env")

_WABA_ID = "WABA-100"
_FLOWS_URL = f"https://graph.facebook.com/v23.0/{_WABA_ID}/flows"
_MESSAGES_URL = f"https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages"

# A form schema with a text field (a label/title), a choice field and a date field, so the
# emitted ``flow_json`` carries component names, human-readable labels, a data-source and a
# date control — the places a guest identifier could leak if any were derived from the send.
_SCHEMA = {
    "type": "object",
    "properties": {
        "note": {"type": "string", "title": "Your note"},
        "tier": {"type": "string", "enum": ["g", "s"]},
        "when": {"type": "string", "format": "date"},
    },
    "required": ["note"],
}

_REACT_SCHEMA = {
    "type": "object",
    "properties": {"tier": {"type": "string", "enum": ["g", "s"]}, "note": {"type": "string"}},
    "required": ["tier"],
}


@pytest.fixture
def waba_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WHATSAPP_WABA_ID", _WABA_ID)
    reset_all_settings()


def _flow_created(flow_id: str = "flow-1") -> httpx.Response:
    return response(200, json={"id": flow_id})


def _published() -> httpx.Response:
    return response(200, json={"success": True})


def _flow_cache_key(schema_hash: str) -> str:
    return f"channel:whatsapp:flow:{_WABA_ID}:{schema_hash}"


def _form_delivery(**overrides):
    fields = {"answer_format": "form", "schema": _SCHEMA, "question": "Please fill this in."}
    fields.update(overrides)
    return make_delivery(**fields)


# --- REUSE: one published Flow across guests, create runs once ----------------------


async def test_same_form_to_different_guests_publishes_one_flow(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # Two form asks of the SAME form to DIFFERENT guests (different recipient wa_id and
    # interaction id). The first is a cache miss (create + publish + send); the second a
    # cache hit (send only). create_flow must run exactly ONCE — never a new Flow per send.
    fake_httpx.responses.append(_flow_created("flow-1"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.ONE"))
    fake_httpx.responses.append(_accepted("wamid.TWO"))

    await WhatsAppChannel().deliver(_form_delivery(recipient=ALLOWED_A, interaction_id="int-1"))
    await WhatsAppChannel().deliver(_form_delivery(recipient=ALLOWED_B, interaction_id="int-2"))

    urls = [call["url"] for call in fake_httpx.calls]
    assert urls.count(_FLOWS_URL) == 1  # exactly one create across the two sends
    assert urls == [_FLOWS_URL, "https://graph.facebook.com/v23.0/flow-1/publish", _MESSAGES_URL, _MESSAGES_URL]


async def test_notify_same_form_to_different_guests_publishes_one_flow(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # The ask-less notification resolves the Flow through the same machinery: two notifies of
    # the same form to different guests publish one Flow, then a send each.
    from tai42_contract.channels import ChannelNotification

    fake_httpx.responses.append(_flow_created("flow-nf"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.ONE"))
    fake_httpx.responses.append(_accepted("wamid.TWO"))

    await WhatsAppChannel().notify(ChannelNotification(message="Fill this.", recipient=ALLOWED_A, schema=_SCHEMA))
    await WhatsAppChannel().notify(ChannelNotification(message="Fill this.", recipient=ALLOWED_B, schema=_SCHEMA))

    urls = [call["url"] for call in fake_httpx.calls]
    assert urls.count(_FLOWS_URL) == 1
    assert urls == [_FLOWS_URL, "https://graph.facebook.com/v23.0/flow-nf/publish", _MESSAGES_URL, _MESSAGES_URL]


# --- NO-PII: no guest phone / correlation token in the published artefact -----------


def _published_artefact(fake_httpx: FakeHttpx) -> str:
    # The create_flow body is the artefact Meta persists: its name, its flow_json, and (for a
    # reacting Flow) its endpoint_uri. Serialize the whole body back to a string so a scan sees
    # every component name, label, reference, literal and navigate-payload key inside flow_json.
    create_calls = [call for call in fake_httpx.calls if call["url"] == _FLOWS_URL]
    assert len(create_calls) == 1
    return json.dumps(create_calls[0]["json"])


async def test_published_flow_carries_no_guest_identifier(waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # Send a form ask to a known guest: the recipient wa_id (the guest's phone number) and the
    # interaction id (the flow token). Neither may appear in the published Flow name or flow_json.
    guest_phone = ALLOWED_A
    interaction_id = "int-1"
    fake_httpx.responses.append(_flow_created("flow-1"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.FORM"))

    await WhatsAppChannel().deliver(_form_delivery(recipient=guest_phone, interaction_id=interaction_id))

    artefact = _published_artefact(fake_httpx)
    assert guest_phone not in artefact  # the guest's phone number never reaches the published Flow
    assert interaction_id not in artefact  # the correlation token is not baked into the artefact
    assert "wamid" not in artefact  # no message id of any kind

    # The published name is the schema-derived label only; the flow_json is byte-identical to the
    # pure builder output, which the guest never touches.
    flow_json, schema_hash = build_form_flow(_SCHEMA)
    create_body = json.loads(artefact)
    assert create_body["name"] == f"tai42-form-{schema_hash}"
    assert guest_phone not in create_body["name"]
    assert json.loads(create_body["flow_json"]) == flow_json

    # The correlation token rides the SEND's runtime parameters only — proving it is sent at
    # runtime, not persisted in the published Flow.
    send = fake_httpx.calls[-1]["json"]
    assert send["to"] == guest_phone
    assert send["interactive"]["action"]["parameters"]["flow_token"] == interaction_id


async def test_published_reacting_flow_carries_no_guest_identifier(
    waba_env, monkeypatch: pytest.MonkeyPatch, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # The reacting (endpoint-driven) Flow is published the same way, plus an endpoint_uri. The
    # guest's phone number and the interaction id must be absent from that artefact too.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from tai42_kit.settings import reset_all_settings

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    monkeypatch.setenv("CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY", pem)
    monkeypatch.setenv("CHANNEL_WHATSAPP_FLOW_ENDPOINT_URI", "https://app.example/api/channels/whatsapp/flow-data")
    reset_all_settings()

    guest_phone = ALLOWED_A
    interaction_id = "int-1"
    reactions = FormReactions(field_changed=["tier"], submitted=True, choices=["tier"])
    fake_httpx.responses.append(_flow_created("flow-react"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.R"))

    await WhatsAppChannel().deliver(
        make_delivery(
            answer_format="form",
            schema=_REACT_SCHEMA,
            question="Pick a tier.",
            reactions=reactions,
            recipient=guest_phone,
            interaction_id=interaction_id,
        )
    )

    artefact = _published_artefact(fake_httpx)
    assert guest_phone not in artefact
    assert interaction_id not in artefact
    assert "wamid" not in artefact
    # The artefact carries the data endpoint URI (not guest data) and the data-exchange version.
    create_body = json.loads(artefact)
    assert create_body["endpoint_uri"] == "https://app.example/api/channels/whatsapp/flow-data"
    assert json.loads(create_body["flow_json"])["data_api_version"] == "3.0"


async def test_published_flow_with_prefill_and_options_carries_no_guest_identifier(
    waba_env, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # Per-send prefill values and option lists ride the SEND's flow_action_payload.data, never
    # the published Flow — so even a form carrying per-send data publishes a guest-free artefact.
    schema = {
        "type": "object",
        "properties": {"tier": {"type": "string", "enum": ["g", "s"]}, "note": {"type": "string"}},
    }
    data = FormData(values={"note": "prefill-xyzzy"}, options={"tier": [FormOption(value="g", label="Gold")]})
    pages = [FormPage(title="Plan", fields=["tier"]), FormPage(title="Say", fields=["note"])]
    guest_phone = ALLOWED_A
    interaction_id = "int-1"
    fake_httpx.responses.append(_flow_created("flow-multi"))
    fake_httpx.responses.append(_published())
    fake_httpx.responses.append(_accepted("wamid.ONE"))

    await WhatsAppChannel().deliver(
        _form_delivery(schema=schema, data=data, pages=pages, recipient=guest_phone, interaction_id=interaction_id)
    )

    artefact = _published_artefact(fake_httpx)
    assert guest_phone not in artefact
    assert interaction_id not in artefact
    # The prefill value rides the send's data payload, not the published Flow.
    assert "prefill-xyzzy" not in artefact
    send_data = fake_httpx.calls[-1]["json"]["interactive"]["action"]["parameters"]["flow_action_payload"]["data"]
    assert send_data["note__init"] == "prefill-xyzzy"
