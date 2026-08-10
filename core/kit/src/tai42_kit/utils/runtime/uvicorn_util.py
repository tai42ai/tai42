import json
import logging
from typing import Any, get_args, get_origin, get_type_hints

logger = logging.getLogger(__name__)


def _is_value_token(token: str) -> bool:
    """Whether a token following a ``--key`` flag is that flag's value.

    ``--flag`` tokens and single-dash flags (``-q``) are not values; a
    single-dash token that parses as a negative number (``-5``, ``-5.5``) is a
    value; every other token is a value.
    """
    if token.startswith("--"):
        return False
    if token.startswith("-"):
        try:
            float(token)
        except ValueError:
            return False
        return True
    return True


def _get_cli_pairs(items: tuple[str, ...]) -> dict[str, str]:
    """
    Step 1: Structural Parse.
    Extracts --key value or --key=value into a raw string dictionary.
    """
    pairs = {}
    i = 0
    while i < len(items):
        item = items[i]
        if not item.startswith("--"):
            i += 1
            continue

        raw_key = item[2:]
        if "=" in raw_key:
            key_part, value_str = raw_key.split("=", 1)
            key = key_part.replace("-", "_")
            pairs[key] = value_str
            i += 1
        else:
            key = raw_key.replace("-", "_")
            if i + 1 < len(items):
                next_item = items[i + 1]
                if _is_value_token(next_item):
                    pairs[key] = next_item
                    i += 2
                    continue

            # If no value follows, treat as a flag
            pairs[key] = "true"
            i += 1
    return pairs


def _infer_and_cast(value_str: str, target_type: Any) -> Any:
    """
    Step 2: Smart Casting.
    Handles JSON inference and Uvicorn-specific boolean logic.
    """
    # A parameterized generic (``list[tuple[str, str]]``, ``dict[str, int]``)
    # cannot be used with ``isinstance`` or called as a constructor; reduce it to
    # its origin class for the type check.
    check_type = get_origin(target_type) or target_type

    # 1. Attempt JSON/Primitive load first
    try:
        val = json.loads(value_str)
        # If the type is correct (e.g. we got an int and need an int), return it
        if isinstance(check_type, type) and isinstance(val, check_type):
            return val
    except (json.JSONDecodeError, TypeError):
        pass

    # 2. Uvicorn-style Boolean Support (handles 'yes', 'no', '0', '1', 'on', 'off')
    if target_type is bool:
        normalized = str(value_str).lower().strip()
        if normalized in ("true", "1", "yes", "on", "y"):
            return True
        if normalized in ("false", "0", "no", "off", "n"):
            return False
        # An unrecognized token for a bool-typed arg must fail loudly: casting it
        # via ``bool(value_str)`` would make any non-empty string truthy
        # (``--reload maybe`` -> True), silently accepting garbage.
        raise ValueError(
            f"uvicorn boolean arg value {value_str!r} is not a recognized boolean token "
            "(true/false, 1/0, yes/no, on/off, y/n)"
        )

    # 3. Standard Casts — only for a concrete, non-generic type. Calling a
    # parameterized generic as a constructor (``list[str]("[...]")``) would split
    # the raw JSON string into characters and return silent garbage, so pass the
    # raw string through for uvicorn's own validation to surface instead.
    if isinstance(target_type, type):
        try:
            return target_type(value_str)
        except (ValueError, TypeError):
            # Best-effort cast failed; pass the raw string through so uvicorn's own
            # validation surfaces the error. Logged so the degrade is visible.
            logger.debug("uvicorn arg value %r not castable to %s; passing raw", value_str, target_type)
            return value_str

    logger.debug("uvicorn arg value %r has non-constructible type %s; passing raw", value_str, target_type)
    return value_str


def parse_and_validate_uvicorn_args(items: tuple[str, ...]) -> dict[str, Any]:
    """Main entry point for parsing and schema-matching."""
    # Opt-in dependency (the "uvicorn" extra) — imported at call time so the
    # module (and the utils.runtime namespace) stays importable without it.
    import uvicorn

    raw_pairs = _get_cli_pairs(items)
    hints = get_type_hints(uvicorn.Config.__init__)
    final_kwargs = {}

    for key, value_str in raw_pairs.items():
        if key in hints:
            target_type = hints[key]
            # Unwrap Optional[T] or Union[T, None]
            origin = get_origin(target_type)
            if origin is not None:
                args = [a for a in get_args(target_type) if a is not type(None)]
                target_type = args[0] if args else str

            final_kwargs[key] = _infer_and_cast(value_str, target_type)
        else:
            # Fallback for keys not in __init__ (e.g. extra settings)
            try:
                final_kwargs[key] = json.loads(value_str)
            except (ValueError, TypeError):
                # Not JSON; keep the raw string. Logged so the degrade is visible.
                logger.debug("uvicorn extra arg %s=%r not JSON; keeping raw string", key, value_str)
                final_kwargs[key] = value_str

    return final_kwargs
