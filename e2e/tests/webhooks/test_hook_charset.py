r"""PLAN_10 §C (R5) — a hook's ``topic`` and ``name`` are each dispatched as ONE
lowercase URL path segment, so the register door refuses any value outside that charset
with a 400 — never a stored record no webhook delivery could route to and no door could
address. The end anchor is ``\Z`` (not ``$``), so a trailing newline cannot slip a
phantom segment past the check.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.stack import TaiStack


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("topic", "bad/topic"),  # a path separator would split the URL segment
        ("topic", "BadTopic"),  # uppercase is out of charset
        ("topic", "e2e-topic\n"),  # trailing newline — the \Z-anchor regression
        ("topic", ""),  # empty addresses nothing
        ("name", "bad/name"),  # a path separator
        ("name", "e2e-name\n"),  # trailing newline
    ],
)
async def test_hook_register_rejects_out_of_charset_segment(
    replicas_stack: TaiStack, uniq: Callable[[str], str], field: str, value: str
) -> None:
    api = replicas_stack.api(port=replicas_stack.port_a)
    # Every identity field is well-formed except the one under test, so the 400 is
    # unambiguously the charset rejection of that field.
    body = {
        "name": uniq("hook").replace("_", "-"),
        "topic": uniq("topic").replace("_", "-"),
        "tool": "e2e_record",
        "execution_key": uniq("exec"),
        field: value,
    }
    resp = await api.request_raw("POST", "/api/hooks", json=body)
    assert resp.status_code == 400, f"{field}={value!r} was not rejected: {resp.text}"
    assert field in resp.json()["error"], resp.text
