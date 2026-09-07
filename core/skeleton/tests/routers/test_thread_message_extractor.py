"""The operator-send door's body extractor, pinned to carry the full rich-send vocabulary.

The op ``send_conversation_thread_message`` forwards ``location``/``sections``/``header``/
``footer`` into the conversation send; this asserts the HTTP door parses and forwards them
rather than dropping them, so an HTTP client's rich fields reach the op.
"""

from __future__ import annotations

import json

from starlette.requests import Request

from tai42_skeleton.routers.conversations import _extract_thread_message


def _make_post_request(body: dict) -> Request:
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/conversations/relay/thread/messages",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
        "client": ("1.2.3.4", 1),
        "path_params": {"route_name": "relay"},
    }
    return Request(scope, receive)


async def test_extractor_forwards_location_sections_header_footer():
    body = {
        "thread_id": "bridge:relay:alice",
        "text": "hi",
        "location": {"latitude": 1.0, "longitude": 2.0},
        "sections": [{"title": "group", "options": [{"kind": "reply", "text": "yes"}]}],
        "header": {"kind": "image", "url": "https://example.test/h.png"},
        "footer": "a trailing line",
    }

    extracted = await _extract_thread_message(_make_post_request(body))

    assert extracted["location"] == body["location"]
    assert extracted["sections"] == body["sections"]
    assert extracted["header"] == body["header"]
    assert extracted["footer"] == body["footer"]


async def test_extractor_defaults_rich_fields_to_none_when_absent():
    extracted = await _extract_thread_message(_make_post_request({"thread_id": "bridge:relay:alice", "text": "hi"}))

    assert extracted["location"] is None
    assert extracted["sections"] is None
    assert extracted["header"] is None
    assert extracted["footer"] is None
