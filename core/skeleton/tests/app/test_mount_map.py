"""The boot/reload mount map: binding sources, the reserved-prefix boot-fail, the
import-time contextvar, and the declared-but-unregistered completeness check."""

from __future__ import annotations

import pytest
from tai42_contract.plugins import (
    PluginItem,
    PluginItemKind,
    PluginSpec,
    RouteDecl,
    RoutesDecl,
)

from tai42_skeleton.app.mount_map import (
    MountBinding,
    MountMapError,
    MountRegistrationError,
    _reject_reserved_public_routes,
    bind_module,
    build_mount_map,
    current_mount_binding,
    note_registered,
)

_RESERVED = ("/api/auth",)


def _router_spec(
    *,
    namespace: str = "acme",
    name: str = "one",
    module: str = "acme_one.router",
    item_name: str = "web",
    base: str = "acme/one",
    path: str = "/ping",
    public: bool = True,
) -> PluginSpec:
    return PluginSpec(
        spec_version=1,
        namespace=namespace,
        name=name,
        package=f"{namespace}-{name}",
        version="1.0.0",
        description="a router plugin",
        license="MIT",
        contract=">=2,<3",
        categories=["utilities"],
        provides=[
            PluginItem(
                kind=PluginItemKind.ROUTER,
                name=item_name,
                module=module,
                description="the router item",
                routes=RoutesDecl(base=base, paths=[RouteDecl(path=path, methods=["GET"], public=public)]),
            )
        ],
    )


def _channel_no_routes_spec(module: str = "acme_chan.channel") -> PluginSpec:
    return PluginSpec(
        spec_version=1,
        namespace="acme",
        name="chan",
        package="acme-chan",
        version="1.0.0",
        description="a channel with no routes",
        license="MIT",
        contract=">=2,<3",
        categories=["utilities"],
        provides=[
            PluginItem(
                kind=PluginItemKind.CHANNEL,
                name="relay",
                module=module,
                description="a route-less channel",
            )
        ],
    )


def _packaged(specs: dict[str, PluginSpec], monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the packaged-spec lookup: module → its PluginSpec, else None."""
    monkeypatch.setattr(
        "tai42_skeleton.app.mount_map._packaged_spec_for_module",
        lambda module: specs.get(module),
    )


def test_packaged_binding_uses_declaration_base_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _router_spec()
    _packaged({"acme_one.router": spec}, monkeypatch)
    mapping = build_mount_map(["acme_one.router"], _RESERVED, dict)
    binding = mapping["acme_one.router"]
    assert binding.owner_ref == "acme/one"
    assert binding.item_name == "web"
    assert binding.base == "acme/one"
    assert binding.resolved_path("/ping") == "/api/acme/one/ping"
    assert binding.forbidden is False


def test_route_mounts_override_the_base(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _router_spec()
    _packaged({"acme_one.router": spec}, monkeypatch)
    mapping = build_mount_map(["acme_one.router"], _RESERVED, lambda: {"acme/one": {"web": "relays/web"}})
    binding = mapping["acme_one.router"]
    assert binding.base == "relays/web"
    assert binding.resolved_path("/ping") == "/api/relays/web/ping"


def test_route_less_item_binds_a_forbidden_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _channel_no_routes_spec()
    _packaged({"acme_chan.channel": spec}, monkeypatch)
    mapping = build_mount_map(["acme_chan.channel"], _RESERVED, dict)
    binding = mapping["acme_chan.channel"]
    assert binding.forbidden is True
    assert binding.declared_routes == ()


def test_operator_module_with_no_packaged_spec_gets_no_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    # A manifest module whose top-level package ships no tai-plugin.yml (an
    # operator-authored deployment module) keeps core semantics: no binding.
    _packaged({}, monkeypatch)
    mapping = build_mount_map(["some.operator.module"], _RESERVED, dict)
    assert mapping == {}


def test_overrides_are_not_read_when_no_plugin_route_module_is_present(monkeypatch: pytest.MonkeyPatch) -> None:
    # No packaged plugin route module → the override loader is never invoked (no
    # install-store round-trip on a deployment with none).
    _packaged({}, monkeypatch)

    def _fail() -> dict:
        raise AssertionError("the override loader must not be called with no plugin route module")

    assert build_mount_map(["some.operator.module"], _RESERVED, _fail) == {}


def test_public_route_under_reserved_prefix_fails_the_build(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _router_spec(base="auth", path="/keys", public=True)
    _packaged({"acme_one.router": spec}, monkeypatch)
    with pytest.raises(MountMapError):
        build_mount_map(["acme_one.router"], _RESERVED, dict)


def test_reserved_prefix_audit_canonicalizes_the_resolved_path() -> None:
    # Defense-in-depth: a non-canonical base ("x/../auth") whose RAW resolved path is
    # /api/x/../auth/keys falls under the reserved /api/auth ONLY after dot-resolution.
    # The audit canonicalizes before comparing, so "reserved always wins" holds even
    # for a non-canonical form the contract's segment validation would not itself
    # produce. A raw startswith comparison would let it through.
    binding = MountBinding(
        owner_ref="acme/one",
        item_name="web",
        base="x/../auth",
        declared_routes=(RouteDecl(path="/keys", methods=["GET"], public=True),),
    )
    with pytest.raises(MountMapError):
        _reject_reserved_public_routes({"acme_one.router": binding}, _RESERVED)


def test_reserved_prefix_audit_leaves_a_canonical_offside_route_alone() -> None:
    # A public route whose canonical resolved path is NOT under the reserved prefix is
    # untouched — the canonicalization narrows, it does not over-reject. base "things/.."
    # resolves /api/things/../keys -> /api/keys, which is not under /api/auth.
    binding = MountBinding(
        owner_ref="acme/one",
        item_name="web",
        base="things/..",
        declared_routes=(RouteDecl(path="/keys", methods=["GET"], public=True),),
    )
    _reject_reserved_public_routes({"acme_one.router": binding}, _RESERVED)


def test_authed_route_under_reserved_prefix_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    # A NON-public route under the reserved prefix is legitimate (the auth surface).
    spec = _router_spec(base="auth", path="/users", public=False)
    _packaged({"acme_one.router": spec}, monkeypatch)
    mapping = build_mount_map(["acme_one.router"], _RESERVED, dict)
    assert mapping["acme_one.router"].base == "auth"


def test_find_route_matches_exact_path_and_method_set() -> None:
    binding = MountBinding(
        owner_ref="acme/one",
        item_name="web",
        base="acme/one",
        declared_routes=(RouteDecl(path="/ping", methods=["GET"], public=True),),
    )
    assert binding.find_route("/ping", frozenset({"GET"})) is not None
    assert binding.find_route("/ping", frozenset({"POST"})) is None
    assert binding.find_route("/pong", frozenset({"GET"})) is None


def test_bind_module_carries_the_binding_on_the_contextvar() -> None:
    binding = MountBinding("acme/one", "web", "acme/one", (RouteDecl(path="/ping", methods=["GET"], public=True),))
    assert current_mount_binding() is None
    with bind_module(binding):
        assert current_mount_binding() is binding
        note_registered("/ping", frozenset({"GET"}))
    assert current_mount_binding() is None


def test_declared_but_unregistered_route_raises_on_clean_import() -> None:
    binding = MountBinding(
        "acme/one",
        "web",
        "acme/one",
        (
            RouteDecl(path="/ping", methods=["GET"], public=True),
            RouteDecl(path="/pong", methods=["POST"], public=True),
        ),
    )
    with pytest.raises(MountRegistrationError, match="never registered"), bind_module(binding):
        note_registered("/ping", frozenset({"GET"}))


def test_bind_module_none_is_a_passthrough() -> None:
    with bind_module(None):
        assert current_mount_binding() is None
