"""Tests for the plugin-spec contract (``tai42_contract.plugins``).

Pins the 14-kind vocabulary, the kind→manifest binding table (the single
source the installer and the registry consume), the frozen ``PluginSpec``
model family, and every validator's pass/raise paths.
"""

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


def test_kind_enum_has_the_fourteen_kinds():
    from tai42_contract.plugins import PluginItemKind

    assert {k.value for k in PluginItemKind} == {
        "tool",
        "agent",
        "extension",
        "connector",
        "channel",
        "backend",
        "storage",
        "monitoring",
        "webhook-verifier",
        "config",
        "identity",
        "studio-plugin",
        "router",
        "middleware",
    }


def test_bindings_cover_every_kind_exactly():
    from tai42_contract.plugins import KIND_MANIFEST_BINDINGS, PluginItemKind

    # Completeness pin: every kind — including ``router`` and ``middleware`` —
    # has a binding, so a future member added without one fails here rather than
    # at a skeleton install.
    assert set(KIND_MANIFEST_BINDINGS) == set(PluginItemKind)


def test_router_and_middleware_bind_to_module_lists():
    from tai42_contract.plugins import KIND_MANIFEST_BINDINGS, ManifestBinding, PluginItemKind

    assert KIND_MANIFEST_BINDINGS[PluginItemKind.ROUTER] == ManifestBinding(field="routers_modules", mode="module_list")
    assert KIND_MANIFEST_BINDINGS[PluginItemKind.MIDDLEWARE] == ManifestBinding(
        field="middlewares_modules", mode="module_list"
    )


def test_binding_fields_are_real_manifest_fields():
    from tai42_contract.manifest import Manifest
    from tai42_contract.plugins import KIND_MANIFEST_BINDINGS

    # Every manifest-wired binding names an actual Manifest field, so the
    # installer can never patch a field the manifest model would reject.
    for kind, binding in KIND_MANIFEST_BINDINGS.items():
        if binding.field is not None:
            assert binding.field in Manifest.model_fields, f"{kind}: {binding.field}"


def test_binding_mode_matches_manifest_field_cardinality():
    from typing import get_origin

    from tai42_contract.manifest import Manifest
    from tai42_contract.plugins import KIND_MANIFEST_BINDINGS

    # A scalar mode must target a non-list Manifest field (a single-module slot
    # like ``str | None``); every list/row mode must target a list-typed field.
    # This guards the binding table against a mode/field cardinality mismatch
    # that would make an installer append to a scalar or overwrite a list.
    scalar_modes = {"scalar_module"}
    list_modes = {"config_row", "module_list", "package_list"}
    for kind, binding in KIND_MANIFEST_BINDINGS.items():
        if binding.field is None:
            continue
        annotation = Manifest.model_fields[binding.field].annotation
        is_list_field = get_origin(annotation) is list
        if binding.mode in scalar_modes:
            assert not is_list_field, f"{kind}: scalar mode targets list field {binding.field}"
        elif binding.mode in list_modes:
            assert is_list_field, f"{kind}: list mode targets non-list field {binding.field}"
        else:  # pragma: no cover - a new mode must be classified here
            raise AssertionError(f"{kind}: unclassified mode {binding.mode!r}")


def test_binding_field_none_iff_env_selected():
    from tai42_contract.plugins import ManifestBinding

    with pytest.raises(ValueError, match="env_selected"):
        ManifestBinding(field=None, mode="module_list")
    with pytest.raises(ValueError, match="env_selected"):
        ManifestBinding(field="tools", mode="env_selected")
    assert ManifestBinding(field=None, mode="env_selected").field is None


def test_spec_constructs_and_is_frozen():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    spec = PluginSpec(**_spec_kwargs())
    assert spec.package == "tai42-toolbox"
    with pytest.raises(ValidationError):
        spec.name = "changed"


def test_ref_is_namespace_slash_name():
    from tai42_contract.plugins import PluginSpec

    assert PluginSpec(**_spec_kwargs()).ref == "tai42/toolbox"


def test_spec_version_must_be_one():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match="spec_version"):
        PluginSpec(**_spec_kwargs(spec_version=2))


def test_unknown_top_level_key_rejected():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match="pricing"):
        PluginSpec(**_spec_kwargs(pricing="free"))


def test_namespace_and_name_must_be_lowercase_slugs():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    for bad in ("Tai42", "2go", "with space", ""):
        with pytest.raises(ValidationError):
            PluginSpec(**_spec_kwargs(namespace=bad))
        with pytest.raises(ValidationError):
            PluginSpec(**_spec_kwargs(name=bad))


def test_display_name_optional_bounded_and_single_line():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    kwargs = _spec_kwargs()
    del kwargs["display_name"]
    assert PluginSpec(**kwargs).display_name is None
    assert PluginSpec(**_spec_kwargs(display_name=None)).display_name is None
    assert PluginSpec(**_spec_kwargs(display_name="TAI Toolbox")).display_name == "TAI Toolbox"
    with pytest.raises(ValidationError, match="at most 80"):
        PluginSpec(**_spec_kwargs(display_name="x" * 81))
    with pytest.raises(ValidationError, match="display_name must be non-empty"):
        PluginSpec(**_spec_kwargs(display_name="   "))
    with pytest.raises(ValidationError, match="display_name must be a single line"):
        PluginSpec(**_spec_kwargs(display_name="two\nlines"))


def test_icon_accepts_https_or_relative_path_and_rejects_others():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    kwargs = _spec_kwargs()
    del kwargs["icon"]
    assert PluginSpec(**kwargs).icon is None
    assert PluginSpec(**_spec_kwargs(icon=None)).icon is None
    assert PluginSpec(**_spec_kwargs(icon="https://tai42.ai/toolbox.png")).icon == "https://tai42.ai/toolbox.png"
    assert PluginSpec(**_spec_kwargs(icon="assets/icon.svg")).icon == "assets/icon.svg"
    for bad in ("/abs/icon.png", "http://tai42.ai/x.png", "assets\\icon.png"):
        with pytest.raises(ValidationError, match="relative POSIX path"):
            PluginSpec(**_spec_kwargs(icon=bad))
    for bad in ("../escape.png", "assets/../secret.png"):
        with pytest.raises(ValidationError, match=r"'\.\.' segment"):
            PluginSpec(**_spec_kwargs(icon=bad))


def test_package_must_be_a_normalized_dist_name():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    for bad in ("tai42_toolbox", "Tai-Toolbox", "tai-toolbox-", "-tai"):
        with pytest.raises(ValidationError, match="normalized"):
            PluginSpec(**_spec_kwargs(package=bad))


def test_version_must_be_pep440():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    assert PluginSpec(**_spec_kwargs(version="1.0.0rc1")).version == "1.0.0rc1"
    with pytest.raises(ValidationError, match="PEP 440"):
        PluginSpec(**_spec_kwargs(version="not-a-version"))


@pytest.mark.parametrize(
    "value",
    [">=0.1,<0.2", "==0.1.*", "~=1.2", "===0.1.0+anything", ">=0.1", "!=1.0.*"],
)
def test_contract_range_accepts_common_forms(value: str):
    from tai42_contract.plugins import PluginSpec

    assert PluginSpec(**_spec_kwargs(contract=value)).contract == value


@pytest.mark.parametrize(
    "value",
    [
        "0.1",  # no operator
        ">=",  # no version
        ">=0.1,,<0.2",  # empty clause
        "<0.1.*",  # wildcard outside ==/!=
        "==0.1.0rc1.*",  # wildcard stem is not a plain release
        "~=1",  # single release segment
        ">1.0+local",  # local version on an ordered comparison
        ">=zzz",  # unparseable version
        "",  # empty set
    ],
)
def test_contract_range_rejects_malformed(value: str):
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError):
        PluginSpec(**_spec_kwargs(contract=value))


def test_categories_require_one_to_three_unique_slugs():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match=r"1\.\.3"):
        PluginSpec(**_spec_kwargs(categories=[]))
    with pytest.raises(ValidationError, match=r"1\.\.3"):
        PluginSpec(**_spec_kwargs(categories=["a", "b", "c", "d"]))
    with pytest.raises(ValidationError, match="unique"):
        PluginSpec(**_spec_kwargs(categories=["utilities", "utilities"]))
    with pytest.raises(ValidationError, match="category"):
        PluginSpec(**_spec_kwargs(categories=["Utilities"]))


def test_tags_are_bounded_unique_and_lowercase():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match="at most 10"):
        PluginSpec(**_spec_kwargs(tags=[f"t{i}" for i in range(11)]))
    with pytest.raises(ValidationError, match="unique"):
        PluginSpec(**_spec_kwargs(tags=["dup", "dup"]))
    with pytest.raises(ValidationError, match="tag"):
        PluginSpec(**_spec_kwargs(tags=["UPPER"]))


def test_permissions_default_all_false_and_reject_unknown_keys():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginPermissions, PluginSpec

    kwargs = _spec_kwargs()
    del kwargs["permissions"]
    spec = PluginSpec(**kwargs)
    assert spec.permissions == PluginPermissions()
    assert (spec.permissions.network, spec.permissions.subprocess, spec.permissions.filesystem) == (
        False,
        False,
        False,
    )
    with pytest.raises(ValidationError, match="gpu"):
        PluginSpec(**_spec_kwargs(permissions={"gpu": True}))


def test_provides_must_be_non_empty():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match="at least one"):
        PluginSpec(**_spec_kwargs(provides=[]))


def test_provides_rejects_duplicate_kind_name():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    kwargs = _spec_kwargs()
    kwargs["provides"] = [kwargs["provides"][0], dict(kwargs["provides"][0])]
    with pytest.raises(ValidationError, match="duplicate"):
        PluginSpec(**kwargs)


@pytest.mark.parametrize(
    ("kind", "module"), [("router", "tai42_plugin.routers.door"), ("middleware", "tai42_plugin.mw.trace")]
)
def test_provides_accepts_router_and_middleware_items(kind: str, module: str):
    from tai42_contract.plugins import PluginItemKind, PluginSpec

    spec = PluginSpec(
        **_spec_kwargs(
            provides=[
                {
                    "kind": kind,
                    "name": f"{kind}_item",
                    "module": module,
                    "description": f"A plugin {kind}.",
                }
            ]
        )
    )
    item = spec.provides[0]
    assert item.kind is PluginItemKind(kind)
    assert item.module == module


def test_provides_rejects_duplicate_router_items():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    router_item = {
        "kind": "router",
        "name": "door",
        "module": "tai42_plugin.routers.door",
        "description": "A plugin router.",
    }
    with pytest.raises(ValidationError, match="duplicate"):
        PluginSpec(**_spec_kwargs(provides=[router_item, dict(router_item)]))


def test_item_kind_must_be_known():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem

    with pytest.raises(ValidationError):
        PluginItem(kind="gizmo", name="x", module="a.b", description="d")  # pyright: ignore[reportArgumentType]


def test_item_module_must_be_a_dotted_import_path():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem, PluginItemKind

    ok = PluginItem(kind=PluginItemKind.TOOL, name="x", module="pkg.sub.mod", description="d")
    assert ok.module == "pkg.sub.mod"
    for bad in ("pkg..mod", ".pkg", "pkg.", "pkg-name", "1pkg"):
        with pytest.raises(ValidationError, match="import path"):
            PluginItem(kind=PluginItemKind.TOOL, name="x", module=bad, description="d")


def test_item_name_must_be_a_registration_slug():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem, PluginItemKind

    with pytest.raises(ValidationError, match="item name"):
        PluginItem(kind=PluginItemKind.TOOL, name="Bad Name", module="a.b", description="d")


def test_item_description_must_be_a_non_empty_single_line():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem, PluginItemKind

    with pytest.raises(ValidationError, match="single line"):
        PluginItem(kind=PluginItemKind.TOOL, name="x", module="a.b", description="two\nlines")


def test_item_tags_are_bounded():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem, PluginItemKind

    with pytest.raises(ValidationError, match="at most 10"):
        PluginItem(
            kind=PluginItemKind.TOOL,
            name="x",
            module="a.b",
            description="d",
            tags=[f"t{i}" for i in range(11)],
        )


def test_descriptions_must_be_non_empty_single_lines():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match="non-empty"):
        PluginSpec(**_spec_kwargs(description="   "))
    with pytest.raises(ValidationError, match="single line"):
        PluginSpec(**_spec_kwargs(description="two\nlines"))


def test_urls_must_be_http():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    assert PluginSpec(**_spec_kwargs(homepage="https://tai42.ai")).homepage == "https://tai42.ai"
    assert PluginSpec(**_spec_kwargs(homepage=None)).homepage is None
    with pytest.raises(ValidationError, match="http"):
        PluginSpec(**_spec_kwargs(repository="git@github.com:tai42ai/tai42.git"))


def test_license_must_be_an_spdx_id_shape():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match="SPDX"):
        PluginSpec(**_spec_kwargs(license="Apache 2.0"))


def test_plugin_item_rejects_unknown_key():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem, PluginItemKind

    # PluginItem is extra='forbid': a stray key is a loud reject, not ignored.
    with pytest.raises(ValidationError, match="weight"):
        PluginItem(
            kind=PluginItemKind.TOOL,
            name="x",
            module="a.b",
            description="d",
            weight=3,  # pyright: ignore[reportCallIssue]
        )


# (field name, a valid value for that field) for every regex-validated token
# field on PluginSpec. A trailing or embedded newline must be rejected: the
# validators use ``fullmatch``, so ``$``-before-final-newline can no longer
# smuggle a newline past an anchored pattern.
_SPEC_TOKEN_FIELDS: list[tuple[str, str]] = [
    ("namespace", "tai42"),
    ("name", "toolbox"),
    ("package", "tai42-toolbox"),
    ("version", "0.1.0"),
    ("license", "Apache-2.0"),
    ("icon", "assets/toolbox.svg"),
]


@pytest.mark.parametrize(("field", "valid"), _SPEC_TOKEN_FIELDS)
def test_spec_token_fields_reject_newlines(field: str, valid: str):
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    trailing = f"{valid}\n"
    embedded = f"{valid[:1]}\n{valid[1:]}"
    for bad in (trailing, embedded):
        with pytest.raises(ValidationError):
            PluginSpec(**_spec_kwargs(**{field: bad}))


@pytest.mark.parametrize("field", ["categories", "tags"])
def test_spec_list_token_fields_reject_newlines(field: str):
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    for bad in ("utilities\n", "uti\nlities"):
        with pytest.raises(ValidationError):
            PluginSpec(**_spec_kwargs(**{field: [bad]}))


def test_item_name_and_module_reject_newlines():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem, PluginItemKind

    for bad in ("generate_uuid\n", "gen\nerate"):
        with pytest.raises(ValidationError, match="item name"):
            PluginItem(kind=PluginItemKind.TOOL, name=bad, module="a.b", description="d")
    for bad in ("a.b\n", "a\n.b"):
        with pytest.raises(ValidationError, match="import path"):
            PluginItem(kind=PluginItemKind.TOOL, name="x", module=bad, description="d")


def test_item_tags_reject_newlines():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem, PluginItemKind

    for bad in ("uuid\n", "uu\nid"):
        with pytest.raises(ValidationError, match="tag"):
            PluginItem(kind=PluginItemKind.TOOL, name="x", module="a.b", description="d", tags=[bad])


def test_https_icon_rejects_malformed_and_whitespace_urls():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    assert PluginSpec(**_spec_kwargs(icon="https://tai42.ai/toolbox.png")).icon == "https://tai42.ai/toolbox.png"
    # Bare scheme (no host), embedded whitespace/newline, and control chars are
    # all rejected rather than returned unchecked.
    for bad in ("https://", "https:// evil", "https://tai42.ai/x.png\n", "https://tai\t42.ai/x.png"):
        with pytest.raises(ValidationError, match="icon"):
            PluginSpec(**_spec_kwargs(icon=bad))


def test_urls_reject_malformed_and_whitespace():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    for bad in ("https://", "http:// evil", "https://tai42.ai\n", "https://ta\ti42.ai"):
        with pytest.raises(ValidationError):
            PluginSpec(**_spec_kwargs(homepage=bad))


def test_urls_reject_embedded_userinfo_and_hostless_authority():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    # ``tai42.ai@evil.com`` resolves to host evil.com behind a trusted-looking
    # authority; ``user@`` has a non-empty netloc but no host; an unterminated IPv6
    # literal makes urlsplit raise. All are rejected across icon/homepage/repository.
    for bad in ("https://tai42.ai@evil.com", "https://user@", "https://user:pass@", "https://["):
        with pytest.raises(ValidationError, match="icon"):
            PluginSpec(**_spec_kwargs(icon=bad))
        with pytest.raises(ValidationError):
            PluginSpec(**_spec_kwargs(homepage=bad))
        with pytest.raises(ValidationError):
            PluginSpec(**_spec_kwargs(repository=bad))


def test_plugins_module_imports_only_stdlib_or_pydantic():
    import ast
    import sys
    from pathlib import Path

    import tai42_contract.plugins as plugins_module

    # The contract must stay dependency-light: no third-party import (notably
    # ``packaging``) may creep back into the plugin-spec module. Re-adding one
    # would pass every behavioural test, so this AST scan is the guard.
    source_path = Path(plugins_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    allowed_roots = set(sys.stdlib_module_names) | {"pydantic"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative (same-package) import; always allowed.
            roots = [] if node.level else [(node.module or "").split(".")[0]]
        else:
            continue
        for root in roots:
            assert root in allowed_roots, f"disallowed import of {root!r} in {source_path}"


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
    # homepage/repository both route through _check_web_url. A raw control char
    # in a URL is escape-injection bait. U+009F also pins the top of the
    # ``0x7F..0x9F`` range against a narrowing of the shared guard.
    for ctrl in ("\x7f", "\x9b", "\x9f"):
        with pytest.raises(ValidationError, match="icon"):
            PluginSpec(**_spec_kwargs(icon=f"https://tai42.ai/x{ctrl}.png"))
        with pytest.raises(ValidationError):
            PluginSpec(**_spec_kwargs(homepage=f"https://tai42.ai/{ctrl}"))
        with pytest.raises(ValidationError):
            PluginSpec(**_spec_kwargs(repository=f"https://github.com/x/y{ctrl}"))


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

    # URL fields route through _check_web_url, which shares the control-char
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


def test_contract_rejects_surrounding_and_embedded_whitespace():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    for bad in (">=0.1\n", " >=0.1", ">=0.1,\n<2"):
        with pytest.raises(ValidationError, match="whitespace"):
            PluginSpec(**_spec_kwargs(contract=bad))


@pytest.mark.parametrize("value", [">=0.1, <0.2", ">=0.1,<0.2"])
def test_contract_accepts_conventional_inner_whitespace(value: str):
    from tai42_contract.plugins import PluginSpec

    # Conventional whitespace around a comma (PEP 440's ">=0.1, <0.2") stays
    # allowed and is stored verbatim.
    assert PluginSpec(**_spec_kwargs(contract=value)).contract == value


def test_http_urls_are_accepted_for_homepage_and_repository():
    from tai42_contract.plugins import PluginSpec

    # _check_web_url accepts both http and https for homepage/repository; pin
    # that a plain http:// value is accepted so a narrowing to https-only fails.
    assert PluginSpec(**_spec_kwargs(homepage="http://tai42.ai")).homepage == "http://tai42.ai"
    assert (
        PluginSpec(**_spec_kwargs(repository="http://github.com/tai42ai/tai42/tree/main/plugins/toolbox")).repository
        == "http://github.com/tai42ai/tai42/tree/main/plugins/toolbox"
    )
