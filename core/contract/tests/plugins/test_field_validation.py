"""Tests for the shared field-shape validators: single-line control/separator rejection,
bidi/zero-width format-control rejection, and the legitimate-text allowances."""

from __future__ import annotations

from typing import Any

import pytest


def _spec_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "spec_version": 1,
        "namespace": "tai42",
        "name": "toolbox",
        "display_name": "TAI Toolbox",
        "package": "tai42-toolbox",
        "version": "0.1.0",
        "description": "Generic tools and tool extensions.",
        "icon": "assets/toolbox.svg",
        "license": "Apache-2.0",
        "repository": "https://github.com/tai42ai/tai42/tree/main/plugins/toolbox",
        "contract": ">=0.1,<0.2",
        "categories": ["utilities"],
        "tags": ["uuid", "http"],
        "permissions": {"network": True},
        "provides": [
            {
                "kind": "tool",
                "name": "generate_uuid",
                "module": "tai42_toolbox.tools.generate_uuid",
                "description": "Generate a random UUID.",
                "tags": ["uuid"],
            }
        ],
    }
    base.update(overrides)
    return base


# Control / separator characters a single-line field must reject beyond a bare
# ``\n``: any C0 control char, DEL, and the Unicode line/paragraph separators.
# Each would enable terminal-escape / line-overwrite injection from untrusted
# plugin metadata.
_ONE_LINE_REJECTED_CHARS: list[tuple[str, str]] = [
    ("CR", "\r"),
    ("TAB", "\t"),
    ("VT", "\x0b"),
    ("FF", "\x0c"),
    ("NUL", "\x00"),
    ("ESC", "\x1b"),
    # C0 upper-edge chars: pin the top of the ``ord < 0x20`` range so a
    # narrowing to e.g. ``< 0x1c`` fails a test.
    ("U+001C_FS", "\x1c"),
    ("U+001D_GS", "\x1d"),
    ("U+001E_RS", "\x1e"),
    ("U+001F_US", "\x1f"),
    ("DEL", "\x7f"),
    ("U+0080", "\x80"),
    ("U+009B_CSI", "\x9b"),
    ("U+009D_OSC", "\x9d"),
    # C1 upper-edge chars: pin the top of the ``0x7F..0x9F`` range so a
    # narrowing to e.g. ``<= 0x9d`` fails a test.
    ("U+009E", "\x9e"),
    ("U+009F", "\x9f"),
    ("U+2028", "\u2028"),
    ("U+2029", "\u2029"),
    ("U+0085", "\x85"),
]

# Unicode bidirectional and zero-width format controls that free-text fields
# (display_name/description) and URL fields must reject: rendered untrusted in a
# marketplace UI they enable Trojan-Source visual spoofing (CVE-2021-42574).
# ZWJ (U+200D) and ZWNJ (U+200C) are deliberately absent — they are required for
# emoji sequences and legitimate Persian/Farsi text.
_BIDI_FORMAT_REJECTED_CHARS: list[tuple[str, str]] = [
    ("U+061C_ALM", "؜"),
    ("U+200E_LRM", "‎"),
    ("U+200F_RLM", "‏"),
    ("U+202A_LRE", "‪"),
    ("U+202B_RLE", "‫"),
    ("U+202C_PDF", "‬"),
    ("U+202D_LRO", "‭"),
    ("U+202E_RLO", "‮"),
    ("U+2066_LRI", "⁦"),
    ("U+2067_RLI", "⁧"),
    ("U+2068_FSI", "⁨"),
    ("U+2069_PDI", "⁩"),
    ("U+200B_ZWSP", "​"),
    ("U+FEFF_BOM", "﻿"),
]


@pytest.mark.parametrize(("label", "char"), _ONE_LINE_REJECTED_CHARS, ids=[c[0] for c in _ONE_LINE_REJECTED_CHARS])
def test_description_rejects_control_and_separator_chars(label: str, char: str):
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match="single line"):
        PluginSpec(**_spec_kwargs(description=f"abc{char}def"))


@pytest.mark.parametrize(("label", "char"), _ONE_LINE_REJECTED_CHARS, ids=[c[0] for c in _ONE_LINE_REJECTED_CHARS])
def test_display_name_rejects_control_and_separator_chars(label: str, char: str):
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match="single line"):
        PluginSpec(**_spec_kwargs(display_name=f"abc{char}def"))


def test_spaced_description_still_accepted():
    from tai42_contract.plugins import PluginSpec

    # A regular ASCII space (0x20) plus unicode letters and an emoji are all
    # legitimate in prose and must pass — the guard rejects only control /
    # separator characters, not printable non-ASCII text.
    text = "Générateur d'outils \U0001f9f0 for développeurs."
    spec = PluginSpec(**_spec_kwargs(description=text))
    assert spec.description == text


def test_urls_reject_del_and_c1_control_chars():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    # DEL (0x7F) and any C1 control (e.g. U+009B CSI, and the U+009F upper edge)
    # must be rejected in URL fields — the https icon branch and
    # homepage/repository both route through check_web_url. A raw control char
    # in a URL is escape-injection bait. U+009F also pins the top of the
    # ``0x7F..0x9F`` range against a narrowing of the shared guard.
    for ctrl in ("\x7f", "\x9b", "\x9f"):
        with pytest.raises(ValidationError, match="icon"):
            PluginSpec(**_spec_kwargs(icon=f"https://tai42.ai/x{ctrl}.png"))
        with pytest.raises(ValidationError):
            PluginSpec(**_spec_kwargs(homepage=f"https://tai42.ai/{ctrl}"))
        with pytest.raises(ValidationError):
            PluginSpec(**_spec_kwargs(repository=f"https://github.com/x/y{ctrl}"))


@pytest.mark.parametrize(
    ("label", "char"), _BIDI_FORMAT_REJECTED_CHARS, ids=[c[0] for c in _BIDI_FORMAT_REJECTED_CHARS]
)
def test_description_rejects_bidi_and_format_controls(label: str, char: str):
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match="single line"):
        PluginSpec(**_spec_kwargs(description=f"abc{char}def"))


@pytest.mark.parametrize(
    ("label", "char"), _BIDI_FORMAT_REJECTED_CHARS, ids=[c[0] for c in _BIDI_FORMAT_REJECTED_CHARS]
)
def test_display_name_rejects_bidi_and_format_controls(label: str, char: str):
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match="single line"):
        PluginSpec(**_spec_kwargs(display_name=f"abc{char}def"))


@pytest.mark.parametrize(
    ("label", "char"), _BIDI_FORMAT_REJECTED_CHARS, ids=[c[0] for c in _BIDI_FORMAT_REJECTED_CHARS]
)
def test_url_fields_reject_bidi_and_format_controls(label: str, char: str):
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    # URL fields route through check_web_url, which shares the control-char
    # predicate; a bidi/zero-width control smuggled into a URL is spoofing bait.
    with pytest.raises(ValidationError, match="icon"):
        PluginSpec(**_spec_kwargs(icon=f"https://tai42.ai/x{char}.png"))
    with pytest.raises(ValidationError):
        PluginSpec(**_spec_kwargs(homepage=f"https://tai42.ai/{char}"))


def test_free_text_allows_zwj_zwnj_and_rtl_letters():
    from tai42_contract.plugins import PluginSpec

    # The bidi/format guard must NOT over-reject legitimate text:
    #   - U+200D ZWJ joins a ZWJ emoji sequence (family emoji here).
    #   - U+200C ZWNJ is required inside a real Persian/Farsi word ("می‌رود").
    #   - Ordinary Arabic and Hebrew RTL letters carry no format controls.
    zwj_family = "\U0001f468‍\U0001f469‍\U0001f467"  # family: man, woman, girl
    persian_zwnj = "می‌رود"  # "می‌رود" (he/she goes)
    arabic = "الأدوات"  # "الأدوات" (the tools)
    hebrew = "כלים"  # "כלים" (tools)
    for text in (
        f"Family {zwj_family} pack",
        f"Persian {persian_zwnj} verb",
        f"Arabic {arabic} listing",
        f"Hebrew {hebrew} listing",
    ):
        assert PluginSpec(**_spec_kwargs(description=text)).description == text
        assert PluginSpec(**_spec_kwargs(display_name=text)).display_name == text
