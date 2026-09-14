"""Redis value coercion and state-hash deserialization: the bytes/str normalizer
and the state-hash-to-model reader shared by every read path."""

from __future__ import annotations

from typing import Literal, cast, overload

from tai42_contract.interactions import InteractionRequest, InteractionResponse, InteractionState


@overload
def as_str(value: None) -> None: ...
@overload
def as_str(value: str | bytes | bytearray) -> str: ...
def as_str(value: str | bytes | bytearray | None) -> str | None:
    """Normalize a redis value to ``str`` whether the client decodes or not."""
    if value is None:
        return None
    return value.decode() if isinstance(value, (bytes, bytearray)) else value


def state_from_raw(raw: dict[str | bytes, str | bytes]) -> InteractionState | None:
    """Build an ``InteractionState`` from a raw state-hash mapping (as returned
    by ``HGETALL``), or ``None`` when the hash is empty (missing/expired)."""
    if not raw:
        return None
    fields = {as_str(k): as_str(v) for k, v in raw.items()}
    request = InteractionRequest.model_validate_json(fields["request"])
    response_json = fields.get("response")
    response = InteractionResponse.model_validate_json(response_json) if response_json else None
    return InteractionState(
        # Pydantic validates the stored status against the Literal at runtime.
        status=cast("Literal['pending', 'answered']", fields["status"]),
        group_id=fields["group_id"],
        request=request,
        response=response,
    )
