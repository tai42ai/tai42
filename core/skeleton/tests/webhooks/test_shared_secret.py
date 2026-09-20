"""The builtin ``shared_secret`` webhook verifier.

Verifies a named header equals an env-resolved secret via a constant-time
compare; the secret is NEVER a stored config value, only a ``secret_env`` name.
A missing env var raises loudly (the door maps that to a fail-closed 500).
"""

from __future__ import annotations

import pytest
from tai42_contract.webhooks import WebhookVerificationError

from tai42_skeleton.webhooks.builtin.shared_secret import SharedSecretVerifier

_CONFIG = {"header": "X-Webhook-Token", "secret_env": "WH_SECRET"}


async def test_happy_path_matching_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WH_SECRET", "s3cr3t")
    v = SharedSecretVerifier()
    # Returns None (no raise) on a matching header; case-insensitive header name.
    assert await v.verify(b"", {"x-webhook-token": "s3cr3t"}, _CONFIG) is None


async def test_wrong_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WH_SECRET", "s3cr3t")
    v = SharedSecretVerifier()
    with pytest.raises(WebhookVerificationError):
        await v.verify(b"", {"X-Webhook-Token": "nope"}, _CONFIG)


async def test_missing_header_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WH_SECRET", "s3cr3t")
    v = SharedSecretVerifier()
    with pytest.raises(WebhookVerificationError):
        await v.verify(b"", {"Other": "x"}, _CONFIG)


async def test_missing_secret_env_raises_not_verification_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WH_SECRET", raising=False)
    v = SharedSecretVerifier()
    # A missing env var is an operator misconfiguration: it raises loudly (KeyError,
    # which the door maps to a fail-closed 500), NOT a WebhookVerificationError.
    with pytest.raises(KeyError):
        await v.verify(b"", {"X-Webhook-Token": "s3cr3t"}, _CONFIG)


@pytest.mark.parametrize("config", [{"secret_env": "WH_SECRET"}, {"header": "X-Token"}, {}])
async def test_malformed_config_raises(monkeypatch: pytest.MonkeyPatch, config: dict) -> None:
    monkeypatch.setenv("WH_SECRET", "s3cr3t")
    v = SharedSecretVerifier()
    # A missing/malformed config key is an operator misconfiguration of the binding:
    # it raises a plain exception (mapped to a fail-closed 500), distinct from the
    # WebhookVerificationError (401) a bad request signature gets.
    with pytest.raises(ValueError, match="requires a non-empty"):
        await v.verify(b"", {"X-Token": "s3cr3t"}, config)


def test_shared_secret_is_not_post_only() -> None:
    # Header-based: works over any delivery method.
    assert SharedSecretVerifier().post_only is False


_ID_CONFIG = {**_CONFIG, "id_header": "X-Delivery-Id"}


def test_replay_defense_keys_on_id_header() -> None:
    from tai42_contract.webhooks import SeenSetClaim

    defense = SharedSecretVerifier().replay_defense(b"", {"X-Delivery-Id": "n-1"}, _ID_CONFIG)
    assert isinstance(defense, SeenSetClaim)
    assert defense.key == "shared_secret:x-delivery-id:n-1"
    assert defense.ttl_seconds == 86400


def test_replay_defense_id_header_case_insensitive() -> None:
    from tai42_contract.webhooks import SeenSetClaim

    defense = SharedSecretVerifier().replay_defense(b"", {"x-delivery-id": "n-2"}, _ID_CONFIG)
    assert isinstance(defense, SeenSetClaim)
    assert defense.key == "shared_secret:x-delivery-id:n-2"


def test_replay_defense_custom_window_honored() -> None:
    from tai42_contract.webhooks import SeenSetClaim

    defense = SharedSecretVerifier().replay_defense(
        b"", {"X-Delivery-Id": "n-3"}, {**_ID_CONFIG, "replay_window_seconds": 120}
    )
    assert isinstance(defense, SeenSetClaim)
    assert defense.ttl_seconds == 120


def test_replay_defense_missing_id_header_config_raises() -> None:
    # Without id_header the scheme cannot be replay-safe: an operator misconfiguration
    # (500), never a silent undefended pass.
    with pytest.raises(ValueError, match="non-empty 'id_header'"):
        SharedSecretVerifier().replay_defense(b"", {"X-Delivery-Id": "n"}, _CONFIG)


def test_replay_defense_missing_id_header_value_fails_closed() -> None:
    # id_header configured but the delivery carries no id under it — refused (401), never
    # dispatched undefended.
    with pytest.raises(WebhookVerificationError, match="replay id header missing"):
        SharedSecretVerifier().replay_defense(b"", {"Other": "x"}, _ID_CONFIG)


@pytest.mark.parametrize("window", [0, -1])
def test_replay_defense_non_positive_window_raises(window: int) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        SharedSecretVerifier().replay_defense(
            b"", {"X-Delivery-Id": "n"}, {**_ID_CONFIG, "replay_window_seconds": window}
        )


def test_replay_defense_bool_window_raises() -> None:
    with pytest.raises(ValueError, match="must be an int"):
        SharedSecretVerifier().replay_defense(
            b"", {"X-Delivery-Id": "n"}, {**_ID_CONFIG, "replay_window_seconds": True}
        )


@pytest.mark.parametrize("window", [1.5, "5"])
def test_replay_defense_non_int_window_raises(window: object) -> None:
    # A float or a numeric string is not a sane seconds count: an operator
    # misconfiguration that fails CLOSED, never a coerced window.
    with pytest.raises(ValueError, match="must be an int"):
        SharedSecretVerifier().replay_defense(
            b"", {"X-Delivery-Id": "n"}, {**_ID_CONFIG, "replay_window_seconds": window}
        )
