"""The store-key check every storage and template operation runs on a caller's key."""

from __future__ import annotations

from tai42_skeleton.operations import BadRequestError
from tai42_skeleton.template.path_guard import UnsafeTemplatePathError, safe_template_path


def checked_store_key(key: object) -> str:
    """Return ``key`` when it is a canonical store key; otherwise raise ``BadRequestError`` with the guard's message."""
    try:
        return safe_template_path(key)
    except UnsafeTemplatePathError as exc:
        raise BadRequestError(str(exc)) from exc
