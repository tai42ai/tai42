"""Plain record and key-listing helpers the trigger-link test modules share: a
valid restore body and the record/name/tombstone key prefixes present in a store's
fake redis."""

from __future__ import annotations

from typing import Any


def rec_keys(store) -> list[str]:
    return [k for k in store.redis._strings if k.startswith(store.settings.trigger_record_key_prefix)]


def name_keys(store) -> list[str]:
    return [k for k in store.redis._strings if k.startswith(store.settings.trigger_name_key_prefix)]


def tomb_keys(store) -> list[str]:
    return [k for k in store.redis._strings if k.startswith(store.settings.trigger_tomb_key_prefix)]


def valid_record(name: str = "rn", topic: str = "t") -> dict[str, Any]:
    return {
        "name": name,
        "topic": topic,
        "execution_key": "k-fire",
        "execution_key_fingerprint": "fp-fire",
        "require_api_key": False,
        "tool_kwargs": None,
        "created_by": None,
        "created_at": "2026-07-21T00:00:00+00:00",
        "expires_at": None,
    }
