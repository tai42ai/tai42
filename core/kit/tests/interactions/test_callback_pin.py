"""The callback-URL pin accepts exactly the shape the platform mints and refuses every other shape."""

from __future__ import annotations

import pytest
from tai42_contract.errors import ErrorKind, error_kind

from tai42_kit.interactions import CallbackPinReason, CallbackUrlRefusedError, pin_callback_url

PREFIX = "https://host.example/api/interactions/callback/"
TICKET = "AbC-12_xyzAbC-12_xyzAbC-12_xyzAbC-12_xyzAbC"
MINTED = f"{PREFIX}{TICKET}"


@pytest.mark.parametrize(
    "url",
    [
        MINTED,
        f"https://host.example:443/api/interactions/callback/{TICKET}",
        f"https://HOST.example/api/interactions/callback/{TICKET}",
    ],
)
def test_the_minted_shape_is_accepted(url: str) -> None:
    pin_callback_url(url, prefix=PREFIX)


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://host.example/admin", CallbackPinReason.PATH_MISMATCH),
        ("https://host.example/api/interactions/callback/../../admin", CallbackPinReason.PATH_MISMATCH),
        ("https://host.example/api/interactions/callback/%2e%2e/%2e%2e/admin", CallbackPinReason.PATH_MISMATCH),
        ("https://host.example/api/interactions/callback/.", CallbackPinReason.PATH_MISMATCH),
        (f"{MINTED}?x=1", CallbackPinReason.PATH_MISMATCH),
        (f"{MINTED}?", CallbackPinReason.PATH_MISMATCH),
        (f"{MINTED}#f", CallbackPinReason.PATH_MISMATCH),
        (f"https://u:p@host.example/api/interactions/callback/{TICKET}", CallbackPinReason.PATH_MISMATCH),
        (f"https://host.example@evil.tld/api/interactions/callback/{TICKET}", CallbackPinReason.HOST_MISMATCH),
        (f"https://host.example\\@evil.tld/api/interactions/callback/{TICKET}", CallbackPinReason.HOST_MISMATCH),
        (f"https://host.example.evil.tld/api/interactions/callback/{TICKET}", CallbackPinReason.HOST_MISMATCH),
        (f"https://host.example./api/interactions/callback/{TICKET}", CallbackPinReason.HOST_MISMATCH),
        (f"{MINTED}/", CallbackPinReason.PATH_MISMATCH),
        (PREFIX, CallbackPinReason.PATH_MISMATCH),
        (f"{PREFIX}a/b/c", CallbackPinReason.PATH_MISMATCH),
        (f"{MINTED}/react", CallbackPinReason.PATH_MISMATCH),
        (f"{PREFIX}t%2Fk", CallbackPinReason.PATH_MISMATCH),
        (f"https://host.example//api/interactions/callback/{TICKET}", CallbackPinReason.PATH_MISMATCH),
        (f"https://host.example/API/interactions/callback/{TICKET}", CallbackPinReason.PATH_MISMATCH),
        (f"http://host.example/api/interactions/callback/{TICKET}", CallbackPinReason.HOST_MISMATCH),
        (f"https://host.example:8443/api/interactions/callback/{TICKET}", CallbackPinReason.HOST_MISMATCH),
        (f"https://host.example:99999/api/interactions/callback/{TICKET}", CallbackPinReason.MALFORMED_URL),
        (f"https://host.example:abc/api/interactions/callback/{TICKET}", CallbackPinReason.MALFORMED_URL),
        (f"https://host.example/api/interactions/call\nback/{TICKET}", CallbackPinReason.MALFORMED_URL),
        (f" {MINTED}", CallbackPinReason.MALFORMED_URL),
    ],
)
def test_every_other_shape_is_refused_with_its_reason(url: str, reason: CallbackPinReason) -> None:
    with pytest.raises(CallbackUrlRefusedError) as caught:
        pin_callback_url(url, prefix=PREFIX)
    assert caught.value.reason is reason


def test_the_refusal_names_the_host_and_never_the_ticket() -> None:
    with pytest.raises(CallbackUrlRefusedError) as caught:
        pin_callback_url(f"https://evil.tld/api/interactions/callback/{TICKET}", prefix=PREFIX)
    assert caught.value.target_host == "evil.tld"
    assert "evil.tld" in str(caught.value)
    assert TICKET not in str(caught.value)
    assert TICKET not in repr(caught.value.args)


def test_the_refusal_is_a_bad_input_error() -> None:
    with pytest.raises(CallbackUrlRefusedError) as caught:
        pin_callback_url("https://host.example/admin", prefix=PREFIX)
    assert error_kind(caught.value) is ErrorKind.BAD_INPUT
    assert isinstance(caught.value, ValueError)


def test_a_prefix_without_its_trailing_slash_is_a_caller_error() -> None:
    with pytest.raises(ValueError, match="must end with '/'"):
        pin_callback_url(MINTED, prefix=PREFIX.rstrip("/"))


class _SubPathCaller:
    """A neutral synthetic consumer: a component serving the platform under a sub-path."""

    prefix = "https://h.example/base/api/interactions/callback/"

    def post_target(self, stored_url: str) -> str:
        pin_callback_url(stored_url, prefix=self.prefix)
        return stored_url


def test_a_sub_path_prefix_pins_under_its_own_path() -> None:
    caller = _SubPathCaller()
    assert caller.post_target(f"{caller.prefix}tk_1") == f"{caller.prefix}tk_1"
    with pytest.raises(CallbackUrlRefusedError) as caught:
        caller.post_target("https://h.example/api/interactions/callback/tk_1")
    assert caught.value.reason is CallbackPinReason.PATH_MISMATCH
