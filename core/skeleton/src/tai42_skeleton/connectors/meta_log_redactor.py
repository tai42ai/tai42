"""The connector-secret log rule, registered with the kit's record redaction.

Managed calls put secrets into the request struct: the OAuth access token via
the JSON-RPC ``_meta`` field (stdio) or an ``Authorization`` header (http), and a
no-auth connection's client config into the ``headers`` (http) or ``env`` (stdio)
of the transport config. No local logger surfaces these under normal config, but
a DEBUG bump of ``mcp.shared.session``, a future fastmcp release, or a wrapper
layer could.

:func:`register_connector_log_redaction` registers this module's rule — mask the
``_meta`` token value and every value inside a logged ``headers``/``env`` object —
with the kit's record factory (``tai42_kit.logging.register_record_redactor``); the
app and the CLI entrypoints install that factory at their scope
(``tai42_kit.logging.install_record_redaction``).
"""

from __future__ import annotations

import re

from tai42_kit.logging import register_record_redactor

from tai42_skeleton.connectors.settings import connector_adapter_settings

_REDACTION = "**********"

# Start of a ``"headers"``/``"env"`` object in the logged config (JSON or python
# repr). The matching close brace is found by a quote-aware balanced scan, NOT a
# brace-free char class — a client secret value can legitimately contain ``{`` or
# ``}``, which would otherwise truncate the object body and leak the rest.
_HEADERS_ENV_START_RE = re.compile(r'["\'](?:headers|env)["\']\s*:\s*\{')

_OPEN_TO_CLOSE = {"[": "]", "{": "}", "(": ")"}


def _build_redactor_regex(meta_key: str) -> re.Pattern[str]:
    # Match the key, then its quoted value in the JSON the emitter logs
    # (``"<key>": "<value>"``). The meta token is a base64url string (no quotes),
    # so a simple value class is safe here.
    quoted_key = re.escape(meta_key)
    return re.compile(
        rf'["\']?{quoted_key}["\']?\s*:\s*(?P<quote>["\'])(?P<value>[^"\']*)(?P=quote)',
    )


def _read_string(text: str, i: int) -> int:
    r"""Index just past the closing quote of the string starting at ``text[i]`` (a quote char).

    Honours ``\\`` escapes; ``len(text)`` if unterminated.
    """
    quote = text[i]
    j = i + 1
    while j < len(text):
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch == quote:
            return j + 1
        j += 1
    return len(text)


def _find_object_end(text: str, open_brace: int) -> int:
    """Index of the ``}`` matching the ``{`` at ``open_brace``, skipping braces inside quoted strings.

    Returns ``len(text)`` if unterminated.
    """
    depth = 0
    i = open_brace
    while i < len(text):
        ch = text[i]
        if ch in "\"'":
            i = _read_string(text, i)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(text)


def _skip_value(text: str, i: int) -> int:
    """Index just past the value starting at ``text[i]``.

    The value is a quoted string, a bracketed collection (``[]``/``{}``/``()``, quote- and nesting-aware),
    or a bare scalar up to the next top-level ``,``/``}``. Consuming the whole value (not stopping at the
    first comma/brace) is what stops a collection or a brace-bearing secret from leaking its tail.
    """
    n = len(text)
    if i >= n:
        return i
    ch = text[i]
    if ch in "\"'":
        return _read_string(text, i)
    if ch in _OPEN_TO_CLOSE:
        depth = 0
        j = i
        while j < n:
            c = text[j]
            if c in "\"'":
                j = _read_string(text, j)
                continue
            if c in "[{(":
                depth += 1
            elif c in "]})":
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        return n
    j = i
    while j < n and text[j] not in ",}":
        j += 1
    return j


def _mask_object_body(body: str) -> str:
    """Mask every value in a flat/nested ``key: value`` object body.

    Keys (always quoted) are kept; each value (any shape) is replaced with the redaction.
    """
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch in " \t\r\n,":
            out.append(ch)
            i += 1
            continue
        if ch in "\"'":
            key_end = _read_string(body, i)
            out.append(body[i:key_end])
            i = key_end
            while i < n and body[i] in " \t":
                out.append(body[i])
                i += 1
            if i < n and body[i] == ":":
                out.append(":")
                i += 1
                while i < n and body[i] in " \t":
                    out.append(body[i])
                    i += 1
                value_end = _skip_value(body, i)
                out.append(f'"{_REDACTION}"')
                i = value_end
            continue
        # A bare/unquoted run where a key was expected (a well-formed dict[str,str]
        # never produces this). Never echo it through — consume to the next
        # top-level delimiter and mask, so no unrecognized content passes in clear.
        value_end = _skip_value(body, i)
        out.append(f'"{_REDACTION}"')
        i = value_end
    return "".join(out)


def _mask_headers_env(msg: str) -> str:
    """Mask every value inside any ``headers``/``env`` object in ``msg``."""
    pos = 0
    while True:
        start = _HEADERS_ENV_START_RE.search(msg, pos)
        if start is None:
            return msg
        open_brace = start.end() - 1
        end = _find_object_end(msg, open_brace)
        masked = _mask_object_body(msg[open_brace + 1 : end])
        msg = msg[: open_brace + 1] + masked + msg[end:]
        pos = open_brace + 1 + len(masked) + 1


# Substring markers of a logged ``headers``/``env`` object, in JSON or python repr.
_HEADERS_ENV_MARKERS = ('"headers"', "'headers'", '"env"', "'env'")


def _redact_meta_match(m: re.Match[str]) -> str:
    """Replace the matched token value with the redaction.

    An empty value has nothing to hide and would garble the text via ``str.replace("", …)`` (which
    injects the redaction between every character), so it is left as-is.
    """
    value = m.group("value")
    if not value:
        return m.group(0)
    return m.group(0).replace(value, _REDACTION)


def _redact_text(text: str, meta_key: str, pattern: re.Pattern[str]) -> str:
    """Mask the meta token value and every headers/env value present in ``text``."""
    if meta_key in text:
        text = pattern.sub(_redact_meta_match, text)
    if any(marker in text for marker in _HEADERS_ENV_MARKERS):
        text = _mask_headers_env(text)
    return text


def register_connector_log_redaction(meta_key: str | None = None) -> None:
    """Register the connector-secret rule with the kit's record redaction (replaces by name).

    Args:
        meta_key: The connector-meta token key to redact; defaults to the configured
            key. A malformed key fails loudly here, at registration, never by
            silently passing tokens at log time.
    """
    key = meta_key if meta_key is not None else connector_adapter_settings().meta_token_key
    pattern = _build_redactor_regex(key)
    register_record_redactor(
        "connector-secrets",
        markers=(key, *_HEADERS_ENV_MARKERS),
        redact=lambda text: _redact_text(text, key, pattern),
    )
