"""A conversation route to a channel must name a channel a plugin registered, and the channel
listing carries each channel's hint for the route's identity.

A channel route naming a channel no plugin registered is a 400 that names it and stores no
route, whether it is created over HTTP or with ``tai conversations create``. ``GET
/api/channels`` lists every registered channel sorted by name with its declared
``identity_hint``: the channel plugins' own sentences, ``null`` for the stub channels, which
declare none."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from tai42_channel_twilio import TwilioChannel
from tai42_channel_web import WebChannel
from tai42_channel_whatsapp import WhatsAppChannel

from tai42_e2e.binaries import tai_bin
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs(
    "kind:identity",
    "probe-tools",
    "setting:conversations:redis",
    "setting:seeded-access-control",
    "setting:tai42_e2e_fixtures.stub_channel",
)

# The stub channels the working-signal stack loads. Their module registers on import, so the
# names are written here rather than imported from it.
_STUB_CHANNELS = ("stub", "stub_working", "stub_working_off")

# A tool the stack serves, so the only reference a refused create misses is the channel.
_TARGET_TOOL = "e2e_overlap_probe"


async def _mint_key(stack: TaiStack, user_id: str) -> None:
    await stack.api(stack.port_b).post(
        "/api/auth/api-keys",
        json={"user_id": user_id, "description": "e2e channel-refusal key", "scopes": ["e2e-all"]},
    )


def _run_tai(stack: TaiStack, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the real ``tai`` console script against the stack, authenticated by the root token."""
    assert stack.auth_token is not None, "the bridge stack is seeded with a root token"
    env = {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/local/bin", "/usr/bin", "/bin"]),
        "HOME": os.environ.get("HOME", str(cwd)),
        "TAI_API_KEY": stack.auth_token,
    }
    argv = [tai_bin(), "--server", stack.origin(stack.port_a), *args]
    return subprocess.run(argv, env=env, cwd=str(cwd), capture_output=True, text=True, timeout=90, check=False)


async def test_a_route_to_an_unregistered_channel_is_refused_over_http(
    working_signal_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, _ = working_signal_stack
    exec_key = uniq("chan-ref-exec")
    await _mint_key(stack, exec_key)
    route = uniq("chan-ref-route").replace("_", "-")
    ghost = uniq("ghost")

    refused = await stack.api(stack.port_a).request_raw(
        "POST",
        f"/api/conversations/{route}",
        json={
            "door": "channel",
            "target_kind": "tool",
            "target_name": _TARGET_TOOL,
            "execution_key": exec_key,
            "channel": ghost,
            "our_identity": "acme-desk",
        },
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"] == f"route channel {ghost!r} is not a registered channel"
    await stack.api(stack.port_b).get(f"/api/conversations/{route}", expect=404)


@pytest.mark.needs("cli")
async def test_a_route_to_an_unregistered_channel_is_refused_by_the_cli(
    working_signal_stack: tuple[TaiStack, str], uniq: Callable[[str], str], tmp_path: Path
) -> None:
    stack, _ = working_signal_stack
    exec_key = uniq("chan-ref-cli-exec")
    await _mint_key(stack, exec_key)
    route = uniq("chan-ref-cli-route").replace("_", "-")
    ghost = uniq("ghost")

    created = _run_tai(
        stack,
        tmp_path,
        "conversations",
        "create",
        route,
        "--door",
        "channel",
        "--target-kind",
        "tool",
        "--target-name",
        _TARGET_TOOL,
        "--execution-key",
        exec_key,
        "--channel",
        ghost,
        "--identity",
        "acme-desk",
    )
    assert created.returncode == 1, f"exit {created.returncode}; stdout:\n{created.stdout}"
    assert created.stdout == ""
    assert created.stderr == f"Error (HTTP 400): route channel {ghost!r} is not a registered channel\n"
    await stack.api(stack.port_b).get(f"/api/conversations/{route}", expect=404)


async def test_the_listing_carries_each_channels_identity_hint(working_signal_stack: tuple[TaiStack, str]) -> None:
    stack, _ = working_signal_stack

    listing = await stack.api(stack.port_a).get("/api/channels")

    assert listing == {
        "channels": [
            *({"name": name, "identity_hint": None} for name in _STUB_CHANNELS),
            {"name": "twilio", "identity_hint": TwilioChannel.identity_hint},
            {"name": "web", "identity_hint": WebChannel.identity_hint},
            {"name": "whatsapp", "identity_hint": WhatsAppChannel.identity_hint},
        ]
    }
