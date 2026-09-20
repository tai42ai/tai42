"""The combined secret-env + manifest-marker write door and its pointer/key helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from tai42_contract.app.responses import ApplyResponse

from tai42_skeleton.app.boot_rules import BackendNeedsBusError
from tai42_skeleton.config.boundary import registered_env_var_names, x_band_env_keys
from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.operations import BadRequestError, operation
from tai42_skeleton.operations._broadcast import apply_response, translate_orphan_env_write

from .models import _ENV_KEY_RE, _ENV_KEY_START, _NON_ENV_KEY_CHAR, _SECRET_MARKS_VAR, SetMcpSecretEnv


def _parse_manifest_pointer(pointer: str) -> list[str]:
    """Split a slash-delimited, no-leading-slash manifest pointer into segments, enforcing mcp-only authority.

    The HEAD segment MUST be ``mcp`` (a loud 400 like
    ``set_mcp_config``'s), and the pointer must address a leaf UNDER ``mcp`` (at least
    one segment past the head). A leading slash, an empty segment, or a wrong head is a
    loud 400.
    """
    segments = pointer.split("/")
    if not segments or segments[0] != "mcp":
        raise BadRequestError(
            f"manifest_pointer head segment must be 'mcp' (slash-delimited, no leading slash); got {pointer!r}"
        )
    if len(segments) < 2 or any(seg == "" for seg in segments):
        raise BadRequestError(
            f"manifest_pointer must address a leaf under 'mcp' with no empty segments; got {pointer!r}"
        )
    return segments


def _derive_secret_env_key(key_hint: str, taken: frozenset[str]) -> str:
    """Derive a valid, unique env KEY from ``key_hint`` (a key_hint-based name).

    The hint is uppercased and reduced to the shell-identifier charset; an empty or
    non-identifier result falls back to ``SECRET``. The name is then made unique against
    ``taken`` (stored env keys and the X band) by appending ``_2``, ``_3``, … — so a
    generated key never clobbers an existing key nor collides with a deployment X-band
    name. The value is NEVER derived from the secret and is never returned to the caller.
    """
    base = _NON_ENV_KEY_CHAR.sub("_", key_hint).strip("_").upper()
    if not base or not _ENV_KEY_START.match(base):
        base = f"SECRET_{base}".rstrip("_") if base else "SECRET"
    candidate = base
    suffix = 2
    while candidate in taken:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _resolve_secret_env_key(explicit: str | None, hint: str | None, value: str, stored: dict[str, str]) -> str:
    """Resolve the env KEY a secret is stored under: exactly one of an EXPLICIT ``explicit`` key or a ``hint``.

    ``hint`` is a base to generate from (``key | key_hint``).
    An explicit key is validated to the shell-identifier charset (an odd value is a loud
    ``ValueError`` → 400) and REFUSED if it collides with an existing stored key holding a
    DIFFERENT value (never a silent overwrite of a live secret; an identical value is an
    idempotent re-send, no collision). A generated key is made unique against the stored keys,
    the X band, AND every registered settings ``env_var``, so it never clobbers a stored key
    nor SHADOWS a registered var. Raises ``ValueError`` (the op maps it to a 400).
    """
    if (explicit is None) == (hint is None):
        raise ValueError("provide exactly one of 'key' (explicit) or 'key_hint' (to generate from)")
    if explicit is not None:
        if not _ENV_KEY_RE.fullmatch(explicit):
            raise ValueError(f"invalid env key {explicit!r}: must match {_ENV_KEY_RE.pattern!r}")
        if explicit in stored and stored[explicit] != value:
            raise ValueError(
                f"explicit key {explicit!r} collides with an existing stored env key holding a "
                "different value — refusing to silently overwrite a live secret"
            )
        return explicit
    taken = frozenset(stored) | x_band_env_keys() | registered_env_var_names()
    return _derive_secret_env_key(cast("str", hint), taken)


def _set_marker_at_pointer(document: dict[str, Any], segments: list[str], marker: str) -> None:
    """Set ``document`` at the location named by ``segments`` (head ``mcp``) to ``marker``.

    A numeric segment indexes a list (in range, else a loud ``ValueError``); a non-numeric
    segment keys a mapping — a missing mapping key is created (as a list when the next
    segment is numeric, else a mapping) so a NEW leaf under an existing MCP entry can be
    written. Pure / re-runnable: it only edits ``document`` (the external-store 409-replay contract).
    A path that traverses a non-container, or a numeric segment out of range, is a
    ``ValueError`` the door maps to a 400.
    """
    node: Any = document
    for depth, seg in enumerate(segments[:-1]):
        nxt = segments[depth + 1]
        if isinstance(node, list):
            node = node[_pointer_index(node, seg, segments)]
        elif isinstance(node, dict):
            if seg not in node:
                node[seg] = [] if nxt.isdigit() else {}
            node = node[seg]
        else:
            raise ValueError(f"manifest_pointer traverses a non-container at {seg!r} in {'/'.join(segments)!r}")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
    last = segments[-1]
    if isinstance(node, list):
        node[_pointer_index(node, last, segments)] = marker
    elif isinstance(node, dict):
        node[last] = marker
    else:
        raise ValueError(f"manifest_pointer traverses a non-container at {last!r} in {'/'.join(segments)!r}")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour


def _pointer_index(node: list[Any], segment: str, segments: list[str]) -> int:
    """A numeric list-index segment resolved against ``node``.

    A non-numeric or out-of-range segment is a loud ``ValueError`` (→ 400).
    """
    if not segment.isdigit():
        raise ValueError(f"manifest_pointer segment {segment!r} must be a list index in {'/'.join(segments)!r}")
    index = int(segment)
    if index >= len(node):
        raise ValueError(f"manifest_pointer list index {index} out of range in {'/'.join(segments)!r}")
    return index


@operation(
    summary="Write a secret env value and its manifest !ENV marker together, then reload",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError],
    request_model=SetMcpSecretEnv,
    response_model=ApplyResponse,
)
async def set_mcp_secret_env(
    value: str, manifest_pointer: str, key: str | None = None, key_hint: str | None = None
) -> dict:
    """Store a pasted secret as an env value and reference it from the manifest by a marker.

    The env KEY is EITHER an explicit ``key`` OR generated from ``key_hint`` — exactly one
    (``key | key_hint``). Writes the secret ``value`` to the env store under that key AND
    marks the key secret (adding it to ``TAI_ENV_SECRET_KEYS``), and writes an ``!ENV ${KEY}``
    MARKER at ``manifest_pointer`` — all through the combined
    ``ConfigService.apply_env_and_change`` pipeline so the env write and the manifest mutate
    stay consistent. Partial-failure: a manifest-persist failure after the env write does
    NO rollback — the env write stands as an inert, re-runnable orphan and the op raises
    loudly. An explicit ``key`` colliding with an existing stored key holding a DIFFERENT value
    is a loud 400 naming the key; a generated key never shadows a registered settings
    ``env_var``. The pointer's HEAD segment MUST be ``mcp`` (loud 400 otherwise). The response
    is the ``reloadConfigResult`` shape; the resolved key is NEVER returned. A dangling
    ``!ENV`` / X-band refusal surfaces as a loud 400 naming the key (the shared boundary
    validator, same as ``POST /api/mcp-config``).
    """
    segments = _parse_manifest_pointer(manifest_pointer)  # loud 400 on a non-mcp head

    service = ConfigService.from_app()

    async def prepare(stored: dict[str, str]) -> tuple[dict[str, str], Callable[[dict[str, Any]], None]]:
        # Runs INSIDE the ConfigService env-write lock, against the stored env read once
        # under that lock — so the key resolution and the secret-marks append are read-then-
        # write against a snapshot no concurrent combined op can clobber.
        #
        # Resolve the key: an explicit key (collision-refused) or generated from the hint
        # (unique against stored keys, the X band, and every registered env_var). A bad key is
        # a ValueError the op maps to a 400 below (raised before any write).
        key_resolved = _resolve_secret_env_key(key, key_hint, value, stored)

        # Mark the new key secret: APPEND to the current marks read from the STORED env —
        # never the settings cache, which is stale until a reload and would clobber a mark a
        # prior op just added. Read→append→fold into the same write_env merge, order-stable.
        existing_marks = [m.strip() for m in stored.get(_SECRET_MARKS_VAR, "").split(",") if m.strip()]
        marks = list(dict.fromkeys([*existing_marks, key_resolved]))
        changes = {key_resolved: value, _SECRET_MARKS_VAR: ",".join(marks)}
        marker = f"!ENV ${{{key_resolved}}}"

        def mutator(document: dict[str, Any]) -> None:
            _set_marker_at_pointer(document, segments, marker)

        return changes, mutator

    with translate_orphan_env_write():
        try:
            result = await service.apply_env_and_change(prepare, manifest_pointer=manifest_pointer)
        except BackendNeedsBusError as exc:
            raise BadRequestError(str(exc)) from exc
        except ValueError as exc:
            # A bad/colliding key from _resolve_secret_env_key (raised inside prepare, before
            # any write) or a boundary refusal (X-band / dangling marker) — a loud 400 either
            # way. A manifest-persist failure after the env write is the OrphanEnvWriteError
            # the surrounding context manager maps to a loud 500.
            raise BadRequestError(str(exc)) from exc
        return apply_response(result)
