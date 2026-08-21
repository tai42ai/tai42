"""Single-line http(s) URL validation shared across the contract.

``check_web_url`` is the one URL check used by both the plugin spec
(``PluginSpec.icon``/``homepage``/``repository``) and the connector
``ProviderDescriptor.icon_url``. It lives below both so neither module owns the
other's rule and there is no import cycle. The injection-character detection it
needs — the terminal-escape / line-overwrite / Trojan-Source spoofing class —
is defined here too so it has a single home.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# Unicode line/paragraph separators that a naive ``"\n" in value`` check misses
# but which drive a line break (or overwrite) in a terminal or UI. (U+0085 NEL
# is itself a C1 control and is caught by the 0x7F..0x9F range below.)
_UNICODE_LINE_SEPARATORS = frozenset("\u2028\u2029\u0085")


# Unicode bidirectional and zero-width format controls. In free text rendered
# untrusted in a marketplace UI these enable Trojan-Source visual spoofing
# (CVE-2021-42574): bidi overrides/isolates reorder displayed characters and
# zero-width spaces/BOM hide or splice tokens. Held as explicit code points (not
# a Unicode-category ban) so that U+200D ZWJ (emoji sequences) and U+200C ZWNJ
# (required for legitimate Persian/Farsi text) — and all ordinary RTL letters —
# stay allowed.
_BIDI_FORMAT_CONTROLS = frozenset(
    {
        "؜",  # ARABIC LETTER MARK
        "‎",  # LEFT-TO-RIGHT MARK
        "‏",  # RIGHT-TO-LEFT MARK
        "‪",  # LEFT-TO-RIGHT EMBEDDING
        "‫",  # RIGHT-TO-LEFT EMBEDDING
        "‬",  # POP DIRECTIONAL FORMATTING
        "‭",  # LEFT-TO-RIGHT OVERRIDE
        "‮",  # RIGHT-TO-LEFT OVERRIDE
        "⁦",  # LEFT-TO-RIGHT ISOLATE
        "⁧",  # RIGHT-TO-LEFT ISOLATE
        "⁨",  # FIRST STRONG ISOLATE
        "⁩",  # POP DIRECTIONAL ISOLATE
        "​",  # ZERO WIDTH SPACE
        "﻿",  # ZERO WIDTH NO-BREAK SPACE / BOM
    }
)


def is_control_char(ch: str) -> bool:
    """True if ``ch`` is a control, line/paragraph separator, or bidirectional /
    zero-width format character: a C0 control (``ord < 0x20``), ``DEL`` or any C1
    control (``0x7F..0x9F``, which covers U+0085 NEL, U+009B CSI, U+009D OSC,
    ...), U+2028/U+2029, or one of the bidi/zero-width format controls in
    :data:`_BIDI_FORMAT_CONTROLS` (LRM/RLM, embeddings/overrides, isolates, ALM,
    ZWSP, BOM). This is the terminal-escape / line-overwrite / Trojan-Source
    injection class. A regular ASCII space (``0x20``), U+200D ZWJ, and U+200C
    ZWNJ are NOT rejected."""
    return ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F or ch in _UNICODE_LINE_SEPARATORS or ch in _BIDI_FORMAT_CONTROLS


def check_web_url(value: str, *, field: str, schemes: tuple[str, ...]) -> str:
    """Validate a single-line http(s) URL.

    Rejects any whitespace or control character (a URL never contains raw
    whitespace, so this also rules out embedded newlines) and requires a
    parseable ``scheme://host`` whose scheme is in ``schemes``, whose host is
    non-empty, and which carries no embedded userinfo (a ``user@host``
    authority-spoofing form). Raises ``ValueError`` naming ``field``.
    """
    if any(ch.isspace() or is_control_char(ch) for ch in value):
        raise ValueError(f"{field} must be a single-line URL with no whitespace or control characters")
    try:
        split = urlsplit(value)
    except ValueError:
        raise ValueError(f"{field} must be a {' or '.join(schemes)} URL") from None
    if split.scheme not in schemes:
        raise ValueError(f"{field} must be a {' or '.join(schemes)} URL")
    # A real host and no embedded userinfo: a bare ``scheme://`` (no host), a
    # ``scheme://user@host`` credential form (the ``trusted.com@evil.com``
    # authority-spoofing vector), or a hostless ``scheme://user@`` is rejected.
    if not split.hostname or "@" in split.netloc:
        raise ValueError(f"{field} must include a host and no embedded credentials")
    return value
