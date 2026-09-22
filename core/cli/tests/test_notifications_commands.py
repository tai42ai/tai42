"""``tai notifications`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, error_response, run_cli


def test_notifications_list_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/notifications"
        assert request.headers["x-api-key"] == "test-key"
        return data_response(
            {
                "notifications": [
                    {"id": "b", "message": "deploy done", "recipient": "ops", "created_at": "2026-07-11T00:00:01Z"},
                    {"id": "a", "message": "deploy started", "recipient": None, "created_at": "2026-07-11T00:00:00Z"},
                ]
            }
        )

    result = run_cli(monkeypatch, handler, ["notifications", "list"])
    assert result.exit_code == 0, result.output
    # Newest-first, as the feed returns it.
    assert result.output.index("deploy done") < result.output.index("deploy started")
    assert "ops" in result.output


def test_notifications_list_empty_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return data_response({"notifications": []})

    result = run_cli(monkeypatch, handler, ["notifications", "list"])
    assert result.exit_code == 0, result.output


def test_notifications_list_json_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return data_response({"notifications": [{"message": "hi", "recipient": None, "created_at": "t"}]})

    result = run_cli(monkeypatch, handler, ["notifications", "list"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"notifications": [{"message": "hi", "recipient": None, "created_at": "t"}]}


def test_notifications_list_error_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("boom", 500)

    result = run_cli(monkeypatch, handler, ["notifications", "list"])
    assert result.exit_code != 0
    assert "boom" in result.output


def test_notifications_notify_posts_message_and_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/notifications"
        assert json.loads(request.content) == {"message": "Deploy finished", "channel": "telegram"}
        return data_response("notification sent via 'telegram'")

    result = run_cli(monkeypatch, handler, ["notifications", "notify", "Deploy finished", "--channel", "telegram"])
    assert result.exit_code == 0, result.output


def test_notifications_notify_media_and_template_ride_validated_body(monkeypatch: pytest.MonkeyPatch) -> None:
    # The nested --media / --template JSON is validated into the contract models and
    # re-serialized onto the request body.
    def media_handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "message": "photo",
            "channel": "whatsapp",
            "media": [{"kind": "image", "url": "https://example.com/a.png", "caption": None, "filename": None}],
        }
        return data_response("notification sent via 'whatsapp'")

    result = run_cli(
        monkeypatch,
        media_handler,
        [
            "notifications",
            "notify",
            "photo",
            "--channel",
            "whatsapp",
            "--media",
            '[{"kind": "image", "url": "https://example.com/a.png"}]',
        ],
    )
    assert result.exit_code == 0, result.output

    def template_handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "message": "shipped",
            "channel": "whatsapp",
            "template": {
                "name": "status_update",
                "language": "en_US",
                "header_media": None,
                "body_parameters": ["A-42"],
                "buttons": [],
            },
        }
        return data_response("notification sent via 'whatsapp'")

    result = run_cli(
        monkeypatch,
        template_handler,
        [
            "notifications",
            "notify",
            "shipped",
            "--channel",
            "whatsapp",
            "--template",
            '{"name": "status_update", "language": "en_US", "body_parameters": ["A-42"]}',
        ],
    )
    assert result.exit_code == 0, result.output


def test_notifications_notify_malformed_media_json_raises_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # Malformed --media never reaches the server: it is a loud usage error.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request must be made for malformed --media")

    result = run_cli(
        monkeypatch, handler, ["notifications", "notify", "hi", "--channel", "whatsapp", "--media", "{not json"]
    )
    assert result.exit_code != 0
    assert "media" in result.output.lower()


def test_notifications_notify_invalid_media_shape_raises_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # Well-formed JSON of the wrong shape (an http image url) is rejected by the
    # contract model before any request leaves.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request must be made for an invalid media item")

    result = run_cli(
        monkeypatch,
        handler,
        [
            "notifications",
            "notify",
            "hi",
            "--channel",
            "whatsapp",
            "--media",
            '[{"kind": "image", "url": "http://insecure.example/a.png"}]',
        ],
    )
    assert result.exit_code != 0
    assert "media" in result.output.lower()


def test_notifications_notify_schema_rides_validated_body(monkeypatch: pytest.MonkeyPatch) -> None:
    # The --schema JSON object (an ask-less form's answer schema) is shape-checked and
    # posted on the body; the server owns the subset walk and the capability gate.
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "message": "fill this in",
            "channel": "whatsapp",
            "schema": {"type": "object", "properties": {"name": {"type": "string"}}},
        }
        return data_response("notification sent via 'whatsapp'")

    result = run_cli(
        monkeypatch,
        handler,
        [
            "notifications",
            "notify",
            "fill this in",
            "--channel",
            "whatsapp",
            "--schema",
            '{"type": "object", "properties": {"name": {"type": "string"}}}',
        ],
    )
    assert result.exit_code == 0, result.output


def test_notifications_notify_malformed_schema_json_raises_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # Malformed --schema never reaches the server: it is a loud usage error.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request must be made for malformed --schema")

    result = run_cli(
        monkeypatch, handler, ["notifications", "notify", "hi", "--channel", "whatsapp", "--schema", "{not json"]
    )
    assert result.exit_code != 0
    assert "schema" in result.output.lower()


def test_notifications_notify_non_object_schema_raises_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # Well-formed JSON that is not an object is rejected before any request leaves —
    # a form's answer schema is a JSON object by contract.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request must be made for a non-object --schema")

    result = run_cli(
        monkeypatch, handler, ["notifications", "notify", "hi", "--channel", "whatsapp", "--schema", '["nope"]']
    )
    assert result.exit_code != 0
    assert "schema" in result.output.lower()


def test_notifications_notify_data_and_pages_ride_validated_body(monkeypatch: pytest.MonkeyPatch) -> None:
    # --data (a FormData) and --pages (a FormPage list) over --schema are validated into their
    # contract models and posted on the body; the server owns the against-schema cross-check.
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "message": "fill this in",
            "channel": "whatsapp",
            "schema": {"type": "object", "properties": {"name": {"type": "string"}}},
            "data": {"values": {"name": "Ada"}, "options": {}},
            "pages": [{"title": "You", "fields": ["name"]}],
        }
        return data_response("notification sent via 'whatsapp'")

    result = run_cli(
        monkeypatch,
        handler,
        [
            "notifications",
            "notify",
            "fill this in",
            "--channel",
            "whatsapp",
            "--schema",
            '{"type": "object", "properties": {"name": {"type": "string"}}}',
            "--data",
            '{"values": {"name": "Ada"}}',
            "--pages",
            '[{"title": "You", "fields": ["name"]}]',
        ],
    )
    assert result.exit_code == 0, result.output


def test_notifications_notify_invalid_data_shape_raises_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # A --data JSON object carrying a key FormData does not declare is refused loudly before
    # any request leaves — the CLI guards its own seam against a silently dropped key.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request must be made for an invalid --data")

    result = run_cli(
        monkeypatch,
        handler,
        [
            "notifications",
            "notify",
            "hi",
            "--channel",
            "whatsapp",
            "--schema",
            '{"type": "object"}',
            "--data",
            '{"nope": 1}',
        ],
    )
    assert result.exit_code != 0
    assert "data" in result.output.lower()


def test_notifications_notify_options_ride_validated_body(monkeypatch: pytest.MonkeyPatch) -> None:
    # The --options JSON array is validated into the contract's discriminated Option union
    # (reply/link) and posted on the body with every field of each variant.
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "message": "pick one",
            "channel": "whatsapp",
            "options": [
                {"kind": "reply", "text": "Item A", "description": None, "id": None},
                {"kind": "link", "label": "Docs", "url": "https://x.example/d"},
            ],
        }
        return data_response("notification sent via 'whatsapp'")

    result = run_cli(
        monkeypatch,
        handler,
        [
            "notifications",
            "notify",
            "pick one",
            "--channel",
            "whatsapp",
            "--options",
            '[{"kind": "reply", "text": "Item A"}, {"kind": "link", "label": "Docs", "url": "https://x.example/d"}]',
        ],
    )
    assert result.exit_code == 0, result.output


def test_notifications_notify_options_and_media_ride_together(monkeypatch: pytest.MonkeyPatch) -> None:
    # --options combines with --media on one notification.
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "message": "photo",
            "channel": "whatsapp",
            "media": [{"kind": "image", "url": "https://example.com/a.png", "caption": None, "filename": None}],
            "options": [
                {"kind": "reply", "text": "Yes", "description": None, "id": None},
                {"kind": "reply", "text": "No", "description": None, "id": None},
            ],
        }
        return data_response("notification sent via 'whatsapp'")

    result = run_cli(
        monkeypatch,
        handler,
        [
            "notifications",
            "notify",
            "photo",
            "--channel",
            "whatsapp",
            "--media",
            '[{"kind": "image", "url": "https://example.com/a.png"}]',
            "--options",
            '[{"kind": "reply", "text": "Yes"}, {"kind": "reply", "text": "No"}]',
        ],
    )
    assert result.exit_code == 0, result.output


def test_notifications_notify_options_and_template_still_post(monkeypatch: pytest.MonkeyPatch) -> None:
    # The CLI validates each field independently and posts both; the server enforces
    # options/template exclusivity.
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "message": "shipped",
            "channel": "whatsapp",
            "template": {
                "name": "status_update",
                "language": "en_US",
                "header_media": None,
                "body_parameters": [],
                "buttons": [],
            },
            "options": [{"kind": "reply", "text": "Yes", "description": None, "id": None}],
        }
        return data_response("notification sent via 'whatsapp'")

    result = run_cli(
        monkeypatch,
        handler,
        [
            "notifications",
            "notify",
            "shipped",
            "--channel",
            "whatsapp",
            "--template",
            '{"name": "status_update", "language": "en_US"}',
            "--options",
            '[{"kind": "reply", "text": "Yes"}]',
        ],
    )
    assert result.exit_code == 0, result.output


def test_notifications_notify_malformed_options_json_raises_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # Malformed --options never reaches the server: it is a loud usage error.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request must be made for malformed --options")

    result = run_cli(
        monkeypatch, handler, ["notifications", "notify", "hi", "--channel", "whatsapp", "--options", "{not json"]
    )
    assert result.exit_code != 0
    assert "options" in result.output.lower()


def test_notifications_notify_non_array_options_raises_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # Well-formed JSON that is not an array is rejected before any request leaves.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request must be made for non-array --options")

    result = run_cli(
        monkeypatch, handler, ["notifications", "notify", "hi", "--channel", "whatsapp", "--options", '"Item A"']
    )
    assert result.exit_code != 0
    assert "options" in result.output.lower()


def test_notifications_notify_non_string_option_entry_raises_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # An array with a non-string entry is rejected by the list[str] shape before any request.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request must be made for a non-string option entry")

    result = run_cli(
        monkeypatch, handler, ["notifications", "notify", "hi", "--channel", "whatsapp", "--options", '["Item A", 3]']
    )
    assert result.exit_code != 0
    assert "options" in result.output.lower()


def test_notifications_notify_sections_ride_validated_body(monkeypatch: pytest.MonkeyPatch) -> None:
    # The --sections JSON array is validated into the contract's OptionSection list and
    # posted with each row expanded to its full ReplyOption shape.
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "message": "pick one",
            "channel": "whatsapp",
            "sections": [
                {
                    "title": "Fruit",
                    "rows": [
                        {"kind": "reply", "text": "Apple", "description": None, "id": None},
                        {"kind": "reply", "text": "Pear", "description": "green", "id": None},
                    ],
                }
            ],
        }
        return data_response("notification sent via 'whatsapp'")

    result = run_cli(
        monkeypatch,
        handler,
        [
            "notifications",
            "notify",
            "pick one",
            "--channel",
            "whatsapp",
            "--sections",
            '[{"title": "Fruit", "rows": [{"kind": "reply", "text": "Apple"}, '
            '{"kind": "reply", "text": "Pear", "description": "green"}]}]',
        ],
    )
    assert result.exit_code == 0, result.output


def test_notifications_notify_location_rides_validated_body(monkeypatch: pytest.MonkeyPatch) -> None:
    # The --location JSON object is validated into a LocationElement and posted verbatim.
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "message": "meet here",
            "channel": "whatsapp",
            "location": {"latitude": 51.5, "longitude": -0.12, "name": "HQ", "address": None},
        }
        return data_response("notification sent via 'whatsapp'")

    result = run_cli(
        monkeypatch,
        handler,
        [
            "notifications",
            "notify",
            "meet here",
            "--channel",
            "whatsapp",
            "--location",
            '{"latitude": 51.5, "longitude": -0.12, "name": "HQ"}',
        ],
    )
    assert result.exit_code == 0, result.output


def test_notifications_notify_header_and_footer_ride_with_options(monkeypatch: pytest.MonkeyPatch) -> None:
    # --header (a display MediaItem) and --footer (a trailing line) compose an interactive
    # message alongside --options; each rides the body in its serialized shape.
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "message": "choose",
            "channel": "whatsapp",
            "options": [{"kind": "reply", "text": "Yes", "description": None, "id": None}],
            "header": {"kind": "image", "url": "https://example.com/h.png", "caption": None, "filename": None},
            "footer": "powered by tai42",
        }
        return data_response("notification sent via 'whatsapp'")

    result = run_cli(
        monkeypatch,
        handler,
        [
            "notifications",
            "notify",
            "choose",
            "--channel",
            "whatsapp",
            "--options",
            '[{"kind": "reply", "text": "Yes"}]',
            "--header",
            '{"kind": "image", "url": "https://example.com/h.png"}',
            "--footer",
            "powered by tai42",
        ],
    )
    assert result.exit_code == 0, result.output


def test_notifications_notify_unknown_template_key_raises_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # The pre-7 ``parameters`` key (now ``body_parameters``) is not a ChannelTemplate field.
    # The CLI rejects it LOUDLY rather than letting model_validate silently drop it — no request
    # leaves and the accepted keys are named.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request must be made for an unknown template key")

    result = run_cli(
        monkeypatch,
        handler,
        [
            "notifications",
            "notify",
            "shipped",
            "--channel",
            "whatsapp",
            "--template",
            '{"name": "status_update", "language": "en_US", "parameters": ["A-42"]}',
        ],
    )
    assert result.exit_code != 0
    assert "parameters" in result.output.lower()
    assert "body_parameters" in result.output.lower()


def test_notifications_notify_unknown_location_key_raises_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # An unknown --location key is rejected before any request — LocationElement has no
    # extra="forbid", so the CLI guards its own seam.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request must be made for an unknown location key")

    result = run_cli(
        monkeypatch,
        handler,
        [
            "notifications",
            "notify",
            "here",
            "--channel",
            "whatsapp",
            "--location",
            '{"latitude": 1, "longitude": 2, "altitude": 30}',
        ],
    )
    assert result.exit_code != 0
    assert "location" in result.output.lower()
    assert "altitude" in result.output.lower()


def test_notifications_notify_invalid_sections_shape_raises_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # A section with empty rows is refused by the contract model before any request leaves.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request must be made for an invalid section")

    result = run_cli(
        monkeypatch,
        handler,
        [
            "notifications",
            "notify",
            "hi",
            "--channel",
            "whatsapp",
            "--sections",
            '[{"title": "Fruit", "rows": []}]',
        ],
    )
    assert result.exit_code != 0
    assert "sections" in result.output.lower()


def test_notifications_notify_invalid_header_shape_raises_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # A malformed header MediaItem (an insecure http image url) is refused by the contract
    # model before any request leaves. The display-item composition rule (header requires a
    # non-link kind and a choice surface) is the server's; the CLI checks only the item shape.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request must be made for an invalid header")

    result = run_cli(
        monkeypatch,
        handler,
        [
            "notifications",
            "notify",
            "hi",
            "--channel",
            "whatsapp",
            "--header",
            '{"kind": "image", "url": "http://insecure.example/h.png"}',
        ],
    )
    assert result.exit_code != 0
    assert "header" in result.output.lower()
