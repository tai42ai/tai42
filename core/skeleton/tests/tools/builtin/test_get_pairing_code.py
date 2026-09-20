"""The ``get_pairing_code`` builtin and its ``mint_pairing_code`` feature body: resolve the
channel route exactly as the accept path does, refuse loudly when multichannel is off or a
required argument is blank (the api door carries none), and otherwise mint a fresh single-use
code — returning ONLY ``{"code", "expires_at"}``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastmcp.utilities.types import get_cached_typeadapter
from pydantic import ValidationError
from tai42_contract.conversations import MultichannelDisabledError, TargetConversationConfig

from tai42_skeleton.conversations import pairing, turn
from tai42_skeleton.conversations.pair_codes import MintingConversation
from tai42_skeleton.tools.builtin import get_pairing_code as builtin_get_pairing_code

_EXPIRES_AT = datetime(2026, 8, 8, 12, 15, 0, tzinfo=UTC)


def _route(**overrides: object) -> SimpleNamespace:
    """A stand-in resolved route carrying exactly the fields the helper reads off it."""
    fields: dict[str, object] = {
        "target_kind": "agent",
        "target_name": "assistant",
        "route_name": "tg-assistant",
        "door": "channel",
        "channel": "telegram",
        "our_identity": "123456",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _StubConfigStore:
    """Callable stub standing in for ``ConversationTargetConfigStore``: constructing it with
    ``settings`` returns the stub itself, whose ``get`` yields the configured row."""

    def __init__(self, config: TargetConversationConfig | None) -> None:
        self.config = config
        self.calls: list[tuple[str, str]] = []

    def __call__(self, settings: object) -> _StubConfigStore:
        return self

    async def get(self, target_kind: str, target_name: str) -> TargetConversationConfig | None:
        self.calls.append((target_kind, target_name))
        return self.config


class _StubCodeStore:
    """Callable stub standing in for ``ConversationPairCodeStore``: records every minting
    conversation and returns a fixed ``(code, expires_at)``."""

    def __init__(self, code: str = "LINK-ABCD1234", expires_at: datetime = _EXPIRES_AT) -> None:
        self.code = code
        self.expires_at = expires_at
        self.minted: list[MintingConversation] = []

    def __call__(self, settings: object) -> _StubCodeStore:
        return self

    async def mint(self, conversation: MintingConversation) -> tuple[str, datetime]:
        self.minted.append(conversation)
        return self.code, self.expires_at


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    """Wire the pairing helper to stubbed seams and return a knob to set the route + config."""

    def _install(*, route: SimpleNamespace, config: TargetConversationConfig | None) -> tuple:
        resolve_calls: list[tuple[str, str]] = []

        async def fake_resolve(channel: str, our_identity_canonical: str) -> SimpleNamespace:
            resolve_calls.append((channel, our_identity_canonical))
            return route

        config_store = _StubConfigStore(config)
        code_store = _StubCodeStore()
        # ``mint_pairing_code`` looks the route-resolver up on ``turn`` (a lazy import that
        # breaks the turn<->pairing module cycle), so the fake is installed there.
        monkeypatch.setattr(turn, "_resolve_channel_route", fake_resolve)
        monkeypatch.setattr(pairing, "ConversationTargetConfigStore", config_store)
        monkeypatch.setattr(pairing, "ConversationPairCodeStore", code_store)
        return resolve_calls, config_store, code_store

    return _install


# -- mint_pairing_code: the feature body -------------------------------------------------


async def test_mints_for_the_resolved_conversation(wired) -> None:
    route = _route()
    config = TargetConversationConfig(target_kind="agent", target_name="assistant", multichannel=True)
    resolve_calls, config_store, code_store = wired(route=route, config=config)

    code, expires_at = await pairing.mint_pairing_code("telegram", "123456", "chat-42")

    assert (code, expires_at) == ("LINK-ABCD1234", _EXPIRES_AT)
    assert resolve_calls == [("telegram", "123456")]
    assert config_store.calls == [("agent", "assistant")]
    assert code_store.minted == [
        MintingConversation(
            target_kind="agent",
            target_name="assistant",
            route_name="tg-assistant",
            door="channel",
            channel="telegram",
            our_identity="123456",
            address="chat-42",
        )
    ]


async def test_our_identity_canonicalized_before_resolution(wired) -> None:
    # A non-canonical (surrounding whitespace) identity resolves by its canonical form, so
    # the resolve seam — which compares canonical forms — matches the route.
    config = TargetConversationConfig(target_kind="agent", target_name="assistant", multichannel=True)
    resolve_calls, _config_store, _code_store = wired(route=_route(), config=config)

    await pairing.mint_pairing_code("telegram", "  123456  ", "chat-42")

    assert resolve_calls == [("telegram", "123456")]


async def test_sender_stored_canonically(wired) -> None:
    config = TargetConversationConfig(target_kind="agent", target_name="assistant", multichannel=True)
    _resolve_calls, _config_store, code_store = wired(route=_route(), config=config)

    await pairing.mint_pairing_code("telegram", "123456", "  chat-42  ")

    assert code_store.minted[0].address == "chat-42"


async def test_multichannel_off_refuses(wired) -> None:
    config = TargetConversationConfig(target_kind="agent", target_name="assistant", multichannel=False)
    _resolve_calls, _config_store, code_store = wired(route=_route(), config=config)

    with pytest.raises(MultichannelDisabledError, match="multichannel turned off"):
        await pairing.mint_pairing_code("telegram", "123456", "chat-42")
    assert code_store.minted == []


async def test_no_config_refuses(wired) -> None:
    # No stored config row is treated identically to multichannel off (default is off).
    _resolve_calls, _config_store, code_store = wired(route=_route(), config=None)

    with pytest.raises(MultichannelDisabledError, match="multichannel turned off"):
        await pairing.mint_pairing_code("telegram", "123456", "chat-42")
    assert code_store.minted == []


@pytest.mark.parametrize(
    ("channel", "our_identity", "sender", "field"),
    [
        ("", "123456", "chat-42", "channel"),
        ("   ", "123456", "chat-42", "channel"),
        (None, "123456", "chat-42", "channel"),
        ("telegram", "", "chat-42", "our_identity"),
        ("telegram", "   ", "chat-42", "our_identity"),
        ("telegram", None, "chat-42", "our_identity"),
        ("telegram", "123456", "", "sender"),
        ("telegram", "123456", "   ", "sender"),
        ("telegram", "123456", None, "sender"),
    ],
)
async def test_blank_or_missing_argument_refuses(wired, channel, our_identity, sender, field) -> None:
    # Called directly, the feature body defends against both a blank string and a bare
    # None (the api door carries None): either is a loud ValueError raised before any route
    # resolution or mint. In real dispatch a null never reaches the body — the tool's typed
    # signature rejects it first (see test_null_argument_refused_at_input_validation).
    resolve_calls, _config_store, code_store = wired(
        route=_route(),
        config=TargetConversationConfig(target_kind="agent", target_name="assistant", multichannel=True),
    )

    with pytest.raises(ValueError, match=f"non-blank {field}"):
        await pairing.mint_pairing_code(channel, our_identity, sender)  # type: ignore[arg-type]
    assert resolve_calls == []
    assert code_store.minted == []


async def test_re_mint_rotates_never_dedupes(wired) -> None:
    # What this pins is the TOOL's own behaviour: it holds no turn-scoped coalescing,
    # so two calls issue two independent mints — never deduped, never reused. The rotation
    # itself (newest code wins, the previous is invalidated) is ConversationPairCodeStore's
    # guarantee, covered by its own tests, not asserted here.
    config = TargetConversationConfig(target_kind="agent", target_name="assistant", multichannel=True)
    _resolve_calls, _config_store, code_store = wired(route=_route(), config=config)

    await pairing.mint_pairing_code("telegram", "123456", "chat-42")
    await pairing.mint_pairing_code("telegram", "123456", "chat-42")

    assert len(code_store.minted) == 2


# -- get_pairing_code: the builtin shim --------------------------------------------------


async def test_builtin_returns_only_code_and_expires_at(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, str]] = []

    async def fake_mint(channel: str, our_identity: str, sender: str) -> tuple[str, datetime]:
        calls.append((channel, our_identity, sender))
        return "LINK-ZZ990011", _EXPIRES_AT

    monkeypatch.setattr(builtin_get_pairing_code, "mint_pairing_code", fake_mint)

    result = await builtin_get_pairing_code.get_pairing_code("telegram", "123456", "chat-42")

    # EXACTLY {code, expires_at} — no links, no wording, no extra keys.
    assert result == {"code": "LINK-ZZ990011", "expires_at": _EXPIRES_AT.isoformat()}
    assert set(result) == {"code", "expires_at"}
    assert calls == [("telegram", "123456", "chat-42")]


async def test_builtin_propagates_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_mint(channel: str, our_identity: str, sender: str) -> tuple[str, datetime]:
        raise MultichannelDisabledError("target agent:assistant has multichannel turned off")

    monkeypatch.setattr(builtin_get_pairing_code, "mint_pairing_code", fake_mint)

    with pytest.raises(MultichannelDisabledError, match="multichannel turned off"):
        await builtin_get_pairing_code.get_pairing_code("telegram", "123456", "chat-42")


def test_builtin_input_schema_is_three_required_strings() -> None:
    # The schema fastmcp derives from the typed signature — three required string params,
    # nothing else.
    schema = get_cached_typeadapter(builtin_get_pairing_code.get_pairing_code).json_schema()
    props = schema["properties"]
    assert set(props) == {"channel", "our_identity", "sender"}
    assert all(props[name]["type"] == "string" for name in props)
    assert set(schema["required"]) == {"channel", "our_identity", "sender"}


@pytest.mark.parametrize(
    "arguments",
    [
        {"channel": None, "our_identity": "123456", "sender": "chat-42"},
        {"our_identity": "123456", "sender": "chat-42"},
    ],
    ids=["null-channel", "absent-channel"],
)
def test_null_or_absent_argument_refused_at_input_validation(arguments: dict[str, object]) -> None:
    # The api door carries no channel/our_identity. Driven through the tool's REAL validated
    # input boundary — the typed signature fastmcp derives — a null or absent argument is
    # refused by pydantic, before the body ever runs. The body's blank-string ValueError
    # never gets the chance to fire on the api door.
    adapter = get_cached_typeadapter(builtin_get_pairing_code.get_pairing_code)
    with pytest.raises(ValidationError):
        adapter.validate_python(arguments)
