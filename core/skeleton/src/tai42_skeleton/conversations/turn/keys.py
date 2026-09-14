"""Deterministic thread, address, rate-bucket and throttle-source key derivation.

Pure string functions: every conversation key a door computes is derived here, so the
thread, the rate bucket and the redeem-throttle scope stay one canonical shape.
"""

from __future__ import annotations

import json
from urllib.parse import quote

from tai42_contract.conversations import ConversationDoor

from tai42_skeleton.agent.thread_reservation import BRIDGE_THREAD_PREFIX, PERSON_THREAD_PREFIX


def _thread_id(route_name: str, client_address: str) -> str:
    return f"{BRIDGE_THREAD_PREFIX}{route_name}:{client_address}"


def _person_thread_id(person_id: str) -> str:
    return f"{PERSON_THREAD_PREFIX}{person_id}"


def _api_client_address(caller_principal: str, address: str) -> str:
    """The API door's address slot: its authenticated caller joined to the caller-supplied
    end-user id. The principal is percent-encoded, so it holds no ``/`` and the join is
    unambiguous for any pair; two callers naming one end user get two addresses."""
    return f"{quote(caller_principal, safe='')}/{address}"


def _channel_bucket_key(route_name: str, cap_key: str) -> str:
    """The channel door's rate-bucket key. ``cap_key`` is the party the door named as
    accountable — a provider-attested address, or a self-minting door's network client
    bucket — and the route scopes it so two routes never share a budget."""
    return f"{route_name}|{cap_key}"


def _api_bucket_key(route_name: str, caller_principal: str) -> str:
    """The API door's rate-bucket key. It is the authenticated CALLER, not the composed
    address, whose cardinality the caller still chooses."""
    return f"{route_name}|caller:{caller_principal}"


def _throttle_source_key(door: ConversationDoor, accountable: str) -> str:
    """The redeem-throttle SOURCE scope: the DOOR-QUALIFIED accountable party, never the
    conversation address (whose cardinality the caller freely chooses). Same accountability
    model the rate caps use — the api door keys on its authenticated ``caller_principal``
    (:func:`_api_bucket_key`), the channel door on the provider-attested ``cap_key``
    (:func:`_channel_bucket_key``). A deterministic JSON array (no delimiter joining) whose
    leading door element keeps an api principal string and a channel value from ever
    colliding, and whose accountable part an attacker cannot rotate per attempt — so the
    lock actually arms against a brute-force run."""
    return json.dumps([door, accountable], separators=(",", ":"))
