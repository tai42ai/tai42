"""Harness self-tests for the channel-provider stubs and the ``channel_modules``
manifest key: each stub records the plugin's outbound call and mints the id the real
provider would, answers a loud 500 on any unscripted path, and resets; the channel
profile carries its four medium plugins, the auth profile carries the deliver-only
stub_channel, and a bare profile omits the key entirely."""

from __future__ import annotations

import httpx

from tai42_e2e.manifests import build_auth_stack, build_bare_stack, build_channel_stack
from tai42_e2e.netfixtures import FakeSlack, FakeTelegram, FakeTwilio
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import StackResources
from tai42_e2e.variants import resolve_variants


async def test_fake_telegram_records_mints_and_fails_loud() -> None:
    stub = FakeTelegram()
    stub.start()
    try:
        async with httpx.AsyncClient(base_url=stub.api_base_url, timeout=5.0) as client:
            first = await client.post("/botTKN/sendMessage", json={"chat_id": "111", "text": "hi", "reply_markup": {}})
            second = await client.post("/botTKN/sendMessage", json={"chat_id": "111", "text": "yo"})
            assert first.status_code == 200
            id1 = first.json()["result"]["message_id"]
            id2 = second.json()["result"]["message_id"]
            assert id2 > id1  # monotonic minted ids
            # A real Message object carries a NUMERIC ``chat.id`` (never the raw string the
            # send addressed) — the writer requires an int to scope its correlation anchor.
            assert first.json()["result"]["chat"]["id"] == 111
            assert [r["text"] for r in stub.sent] == ["hi", "yo"]
            assert stub.sent[0]["message_id"] == id1

            # sendPhoto returns the same Message shape (numeric ``chat.id`` + minted id) and
            # records the caption as the searchable text — a media notification's send path.
            photo = await client.post(
                "/botTKN/sendPhoto", json={"chat_id": "111", "photo": "https://x/p.jpg", "caption": "cap"}
            )
            assert photo.status_code == 200
            assert photo.json()["result"]["chat"]["id"] == 111
            assert photo.json()["result"]["message_id"] > id2
            assert stub.sent[-1]["text"] == "cap"
            assert stub.sent[-1]["method"] == "sendPhoto"

            hook = await client.post("/botTKN/setWebhook", json={"url": "http://x/inbound", "secret_token": "s"})
            assert hook.json() == {"ok": True, "result": True}
            assert stub.webhooks[0]["secret_token"] == "s"

            # Any unforeseen Bot API call is a loud 500 (the LlmStub unscripted rule).
            assert (await client.post("/botTKN/getUpdates", json={})).status_code == 500

        stub.reset()
        assert stub.sent == []
        assert stub.webhooks == []
    finally:
        stub.stop()


async def test_fake_slack_records_mints_and_fails_loud() -> None:
    stub = FakeSlack()
    stub.start()
    try:
        async with httpx.AsyncClient(base_url=stub.api_base_url, timeout=5.0) as client:
            first = await client.post("/chat.postMessage", json={"channel": "C1", "text": "hi"})
            second = await client.post("/chat.postMessage", json={"channel": "C1", "text": "yo"})
            assert first.status_code == 200
            assert first.json()["ok"] is True
            assert first.json()["ts"] != second.json()["ts"]  # distinct minted thread anchors
            assert [r["text"] for r in stub.posts] == ["hi", "yo"]
            # Any unforeseen Slack Web API call is a loud 500.
            unscripted = await client.post(f"http://{stub.host}:{stub.port}/api/conversations.list")
            assert unscripted.status_code == 500
        stub.reset()
        assert stub.posts == []
    finally:
        stub.stop()


async def test_fake_twilio_records_mints_and_fails_loud() -> None:
    stub = FakeTwilio()
    stub.start()
    try:
        async with httpx.AsyncClient(base_url=stub.api_base_url, timeout=5.0) as client:
            first = await client.post("/Accounts/AC1/Messages.json", data={"To": "+1", "From": "+2", "Body": "hi"})
            # An MMS send attaches each image as a repeated ``MediaUrl`` form field; the stub
            # keeps every attachment (not just the last) so a media notify can be asserted.
            second = await client.post(
                "/Accounts/AC1/Messages.json",
                data={"To": "+1", "From": "+2", "Body": "yo", "MediaUrl": ["https://x/a.jpg", "https://x/b.jpg"]},
            )
            assert first.status_code == 201
            assert first.json()["sid"] != second.json()["sid"]  # distinct minted MessageSids
            assert [r["body"] for r in stub.messages] == ["hi", "yo"]
            assert stub.messages[0]["media_urls"] == []
            assert stub.messages[1]["media_urls"] == ["https://x/a.jpg", "https://x/b.jpg"]
            assert (await client.get("/Accounts/AC1/Messages.json")).status_code == 500
        stub.reset()
        assert stub.messages == []
    finally:
        stub.stop()


def _sentinel() -> StackResources:
    return StackResources(
        redis_idx=1,
        redis_url="redis://127.0.0.1:6379/1",
        probe_redis_url="redis://127.0.0.1:6379/0",
        pg_host="127.0.0.1",
        pg_port=5432,
        pg_user="postgres",
        pg_password="postgres",
        pg_db="tai42_e2e_probe",
        storage_root="/tmp/e2e-storage",
        telegram_api_base_url="http://127.0.0.1:1",
        slack_api_base_url="http://127.0.0.1:1/api",
        twilio_api_base_url="http://127.0.0.1:1",
        # A fully-allocated stack carries a per-stack broker_url; the celery
        # backend variant's feature_env reads it. This sentinel renders the
        # manifest only (it never connects), so a placeholder AMQP vhost URL
        # completes it across every backend variant.
        broker_url="amqp://guest:guest@127.0.0.1:5672/tai42_e2e_probe",
    )


def test_channel_modules_render_per_profile() -> None:
    variants = resolve_variants(HarnessSettings())
    # The channel profile carries its four medium plugins.
    channel = build_channel_stack(_sentinel(), variants)
    assert channel.manifest["channel_modules"] == [
        "tai42_channel_telegram.register",
        "tai42_channel_slack.register",
        "tai42_channel_twilio.register",
        "tai42_channel_web.register",
    ]
    # The auth profile carries the deliver-only stub channel: its channel-delivered
    # ask_user pin needs a registered channel, but no real medium plugin.
    auth = build_auth_stack(_sentinel(), variants)
    assert auth.manifest["channel_modules"] == ["tai42_e2e_fixtures.stub_channel"]
    # A bare profile omits the key entirely (the SUT's contract manifest defaults it
    # to []), so a non-interactions stack loads no channel plugins at all.
    assert "channel_modules" not in build_bare_stack(_sentinel(), variants).manifest
